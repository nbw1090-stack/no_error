"""
追踪上下文管理器

提供 TraceContext 和 SpanContext，用于创建和管理 Langfuse trace/span。
"""

import logging
import json
import time
from contextlib import contextmanager

from observability.client import ObservabilityClient, _NoopSpan

logger = logging.getLogger(__name__)


class SpanContext:
    """
    Span 上下文管理器。

    封装 Langfuse span 的生命周期管理，支持嵌套 span 和 generation。

    使用方式：
        with SpanContext(trace, name="tool-call", input={...}) as span:
            result = do_something()
            span.update(output=result)
    """

    def __init__(self, parent, name: str, **kwargs):
        """
        Args:
            parent: 父级 trace 或 span（Langfuse 对象或 _NoopSpan）
            name: span 名称
            **kwargs: 传递给 parent.span() 的额外参数（如 input, metadata）
        """
        self._parent = parent
        self._name = name
        self._kwargs = kwargs
        self._span = None
        self._start_time = None

    def __enter__(self):
        self._start_time = time.time()
        try:
            self._span = self._parent.span(name=self._name, **self._kwargs)
        except Exception as e:
            logger.warning("Failed to create span '%s': %s", self._name, e)
            self._span = _NoopSpan()
        return self._span

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = (time.time() - self._start_time) * 1000 if self._start_time else 0
        try:
            if exc_type is not None:
                # 发生异常时记录错误信息
                self._span.update(
                    status_message=str(exc_val),
                    level="ERROR",
                )
            self._span.update(metadata={"duration_ms": round(duration_ms, 2)})
            self._span.end()
        except Exception as e:
            logger.warning("Failed to end span '%s': %s", self._name, e)

    def generation(self, name: str, **kwargs):
        """
        在当前 span 下创建一个 generation（用于 LLM 调用追踪）。

        Returns:
            generation 对象（或 _NoopSpan）
        """
        try:
            return self._span.generation(name=name, **kwargs)
        except Exception as e:
            logger.warning("Failed to create generation '%s': %s", name, e)
            return _NoopSpan()

    def update(self, **kwargs):
        """更新 span 属性"""
        try:
            self._span.update(**kwargs)
        except Exception:
            pass


class TraceContext:
    """
    Trace 上下文管理器。

    封装 Langfuse trace 的完整生命周期。

    使用方式：
        tracer = get_client()
        with TraceContext(tracer, name="chat", session_id="...", user_id="...") as trace:
            span = trace.span(name="react-iteration")
            gen = span.generation(name="llm-call", model="gpt-4o", ...)
            gen.end()
            span.end()
    """

    def __init__(self, client: ObservabilityClient, name: str, **kwargs):
        """
        Args:
            client: ObservabilityClient 实例
            name: trace 名称
            **kwargs: 传递给 client.trace() 的参数（如 session_id, user_id, input, metadata）
        """
        self._client = client
        self._name = name
        self._kwargs = kwargs
        self._trace = None
        self._start_time = None

    def __enter__(self):
        self._start_time = time.time()
        self._trace = self._client.trace(name=self._name, **self._kwargs)
        return self._trace

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = (time.time() - self._start_time) * 1000 if self._start_time else 0
        try:
            if exc_type is not None:
                self._trace.update(
                    status_message=str(exc_val),
                    level="ERROR",
                )
            self._trace.update(metadata={"total_duration_ms": round(duration_ms, 2)})
            self._trace.end()
        except Exception as e:
            logger.warning("Failed to end trace '%s': %s", self._name, e)

    def span(self, name: str, **kwargs):
        """
        在当前 trace 下创建一个 span。

        Returns:
            span 对象（或 _NoopSpan）
        """
        try:
            return self._trace.span(name=name, **kwargs)
        except Exception as e:
            logger.warning("Failed to create span '%s': %s", name, e)
            return _NoopSpan()

    def update(self, **kwargs):
        """更新 trace 属性（如 output）"""
        try:
            self._trace.update(**kwargs)
        except Exception:
            pass
