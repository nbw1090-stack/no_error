"""
LLM 适配器抽象基类

所有 LLM 提供商适配器必须实现此接口。
内部消息格式遵循 OpenAI 标准（de facto 工具调用标准）。
"""

from abc import ABC, abstractmethod


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
        ] | None
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
