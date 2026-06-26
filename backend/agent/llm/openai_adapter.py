"""
OpenAI 兼容适配器

支持 OpenAI、Ollama、vLLM、LM Studio、Groq 等
所有提供 OpenAI 兼容 API 的服务。
"""

import logging
from openai import AsyncOpenAI

from config import LLMConfig
from agent.llm.base import BaseLLMAdapter

logger = logging.getLogger(__name__)


class OpenAIAdapter(BaseLLMAdapter):
    """OpenAI 兼容的 LLM 适配器"""

    def __init__(self, config: LLMConfig):
        # api_base 为空时使用 OpenAI 默认地址
        base_url = config.api_base or None
        self.client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=base_url,
        )
        self.model = config.model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> dict:
        """发送消息到 OpenAI 兼容 API"""
        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        logger.debug(
            "LLM request: model=%s, messages=%d, tools=%d",
            self.model,
            len(messages),
            len(tools) if tools else 0,
        )

        response = await self.client.chat.completions.create(**kwargs)
        choice = response.choices[0]

        # 标准化 tool_calls 格式
        tool_calls = None
        if choice.message.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in choice.message.tool_calls
            ]

        return {
            "role": "assistant",
            "content": choice.message.content,
            "tool_calls": tool_calls,
        }
