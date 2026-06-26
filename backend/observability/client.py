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
        **kwargs,
    ):
        """
        创建一个 observation（自动嵌套到当前上下文中）。

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
                **kwargs,
            ) as obs:
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
