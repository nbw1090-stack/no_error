"""
LLM 适配器抽象基类

所有 LLM 提供商适配器必须实现此接口。
内部消息格式遵循 OpenAI 标准（de facto 工具调用标准）。
"""

from abc import ABC, abstractmethod
from typing import AsyncGenerator


class BaseLLMAdapter(ABC):
    """
    LLM 适配器抽象基类。

    所有适配器将供应商的响应统一为以下格式：
    {
        "role": "assistant",
        "content": str | None,
        "tool_calls": [
            {
                "id": str,
                "type": "function",
                "function": {"name": str, "arguments": str}
            }
        ] | None,
        "usage": {                       # 可选；供应商返回 usage 时附带，否则省略
            "input": int,                # prompt / 输入 token
            "output": int,               # completion / 输出 token
            "total": int,                # 合计 token
        } | None
    }
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> dict:
        """
        发送消息到 LLM 并返回助手响应。

        Args:
            messages: 对话消息列表，每条为 {"role": ..., "content": ...}
            tools: OpenAI 格式的工具 schema 列表，或 None

        Returns:
            标准化的助手响应字典
        """
        ...

    @abstractmethod
    def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncGenerator[dict, None]:
        """
        流式发送消息到 LLM，逐个 yield 标准化事件。

        事件类型：
            {"type": "content_delta", "text": str}    — 文本增量，收到即转发
            {"type": "tool_call", "tool_call": {...}}  — 一个完整累积后的工具调用
            {"type": "done", "finish_reason": str,
             "content": str, "tool_calls": list|None,
             "usage": {"input": int, "output": int, "total": int} | None}
                                                            — 流结束，携带完整内容/工具调用
                                                              及本轮 token 消耗（可选）

        tool_calls 在流中是分片到达的，实现方须按 index 累积，
        finish_reason 后才整体放入 done.tool_calls。

        Yields:
            标准化的流式事件字典
        """
        ...
