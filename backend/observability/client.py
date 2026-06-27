"""
Langfuse 客户端管理（适配 Langfuse SDK v4.x）

v4 API 采用 OpenTelemetry 风格的上下文管理：
- start_as_current_observation() 创建 observation 并设为当前上下文
- 嵌套调用自动形成父子关系，无需手动传递 parent 对象
- as_type="span" → 普通 span，as_type="generation" → LLM 调用
"""

import logging
from dataclasses import dataclass
from contextlib import contextmanager
from typing import Literal

from config import LangfuseConfig

logger = logging.getLogger(__name__)

_client: "ObservabilityClient | None" = None


@dataclass
class ObservabilityClient:
    """Langfuse 可观测性客户端（v4 SDK）"""

    config: LangfuseConfig
    _langfuse: object | None = None

    @property
    def enabled(self) -> bool:
        return self.config.enabled and self._langfuse is not None

    @contextmanager
    def observation(
        self,
        *,
        name: str,
        as_type: Literal["span", "generation"] = "span",
        input: any = None,
        output: any = None,
        metadata: dict | None = None,
        model: str | None = None,
        usage_details: dict | None = None,
        level: str | None = None,
        status_message: str | None = None,
        # —— trace 级属性（user_id / session_id / trace metadata）——
        # Langfuse v4 的 start_as_current_observation 不接受这些参数，
        # 必须用 propagate_attributes 单独设置并传播给所有子 observation。
        # 在此处拦截，调用方只需像传 session_id 一样传 user_id 即可。
        user_id: str | None = None,
        session_id: str | None = None,
        trace_metadata: dict | None = None,
    ):
        """
        创建一个 observation（自动嵌套到当前上下文中）。

        user_id / session_id 是 trace 级维度：设置后，该 trace 及其全部子节点
        （react-iteration / tool / llm-generation）都会带上 user.id / session.id
        OTEL 属性——这是 Langfuse「按用户/会话聚合 token 成本」等分析的必要条件。
        当 enabled=False 时自动降级为空操作。
        """
        if not self.enabled:
            yield _NoopObservation()
            return

        try:
            with self._langfuse.start_as_current_observation(
                name=name,
                as_type=as_type,
                input=input,
                output=output,
                metadata=metadata,
                model=model,
                usage_details=usage_details,
                level=level,
                status_message=status_message,
            ) as obs:
                # 尽早传播 trace 级属性：必须在子节点（LLM 调用等）生成之前激活，
                # 否则早于此处的 observation 不会被纳入按 user/session 的聚合。
                if user_id or session_id or trace_metadata:
                    from langfuse import propagate_attributes

                    with propagate_attributes(
                        user_id=user_id,
                        session_id=session_id,
                        metadata=trace_metadata,
                    ):
                        yield obs
                else:
                    yield obs
        except Exception as e:
            logger.warning("Langfuse observation '%s' failed: %s", name, e)
            yield _NoopObservation()

    def flush(self):
        if self.enabled:
            try:
                self._langfuse.flush()
            except Exception as e:
                logger.warning("Langfuse flush failed: %s", e)

    def shutdown(self):
        if self.enabled:
            try:
                self._langfuse.shutdown()
            except Exception as e:
                logger.warning("Langfuse shutdown failed: %s", e)


class _NoopObservation:
    """空操作 observation，Langfuse 未启用时使用"""

    def update(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def init_observability(config: LangfuseConfig) -> ObservabilityClient:
    """初始化 Langfuse 客户端（单例）"""
    global _client

    client = ObservabilityClient(config=config)

    if not config.enabled:
        logger.info("Langfuse tracing is DISABLED")
        _client = client
        return client

    if not config.public_key or not config.secret_key:
        logger.warning(
            "Langfuse enabled but keys not configured. "
            "Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY."
        )
        _client = client
        return client

    try:
        from langfuse import Langfuse

        client._langfuse = Langfuse(
            public_key=config.public_key,
            secret_key=config.secret_key,
            host=config.host,
        )
        logger.info(
            "Langfuse initialized (v4): host=%s, enabled=True", config.host
        )
    except Exception as e:
        logger.error("Failed to initialize Langfuse: %s", e)

    _client = client
    return client


def get_client() -> ObservabilityClient:
    """获取全局 Langfuse 客户端"""
    global _client
    if _client is not None:
        return _client
    return ObservabilityClient(config=LangfuseConfig(enabled=False))
