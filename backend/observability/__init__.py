"""
Langfuse 可观测性模块（v4 SDK）

使用方式：
    from observability import get_client

    tracer = get_client()
    with tracer.observation(name="trace-name", input={...}) as trace:
        # trace 是顶层 trace
        with tracer.observation(name="child-span") as span:
            # span 自动嵌套为 trace 的子节点
            with tracer.observation(
                name="llm-call",
                as_type="generation",
                model="gpt-4o",
                usage_details={"input": 100, "output": 50},
            ) as gen:
                pass  # generation 自动嵌套为 span 的子节点
"""

from observability.client import ObservabilityClient, get_client, init_observability

__all__ = [
    "ObservabilityClient",
    "get_client",
    "init_observability",
]
