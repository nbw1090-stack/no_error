"""
OpenAI 兼容适配器

支持 OpenAI、Ollama、vLLM、LM Studio、Groq 等
所有提供 OpenAI 兼容 API 的服务。
"""

import logging
import time
from openai import AsyncOpenAI

from config import LLMConfig
from agent.llm.base import BaseLLMAdapter

logger = logging.getLogger(__name__)


class OpenAIAdapter(BaseLLMAdapter):
    """OpenAI 兼容的 LLM 适配器"""

    def __init__(self, config: LLMConfig):
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
        """
        发送消息到 OpenAI 兼容 API。

        Langfuse generation 会通过 OTEL 上下文自动嵌套到
        当前活跃的 observation 下。
        """
        from observability import get_client

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

        tracer = get_client()
        start_time = time.time()

        with tracer.observation(
            name="llm-chat",
            as_type="generation",
            model=self.model,
            input={
                "messages_count": len(messages),
                "tools_count": len(tools) if tools else 0,
                "messages": _safe_serialize_messages(messages),
                "tools": tools,
            },
            metadata={
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            },
        ) as generation:
            try:
                response = await self.client.chat.completions.create(**kwargs)
            except Exception as e:
                generation.update(
                    status_message=str(e),
                    level="ERROR",
                )
                raise

            choice = response.choices[0]
            duration_ms = (time.time() - start_time) * 1000

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

            # 构建 usage_details（v4 SDK 格式）
            usage_details = None
            if response.usage:
                usage_details = {
                    "input": response.usage.prompt_tokens or 0,
                    "output": response.usage.completion_tokens or 0,
                    "total": response.usage.total_tokens or 0,
                }

            # 更新 generation 的输出
            generation.update(
                output={
                    "content": choice.message.content,
                    "tool_calls": [
                        {
                            "name": tc["function"]["name"],
                            "arguments": tc["function"]["arguments"],
                        }
                        for tc in (tool_calls or [])
                    ],
                },
                usage_details=usage_details,
                metadata={
                    "finish_reason": choice.finish_reason,
                    "duration_ms": round(duration_ms, 2),
                },
            )

        return {
            "role": "assistant",
            "content": choice.message.content,
            "tool_calls": tool_calls,
        }


def _safe_serialize_messages(messages: list[dict]) -> list[dict]:
    """
    安全序列化消息列表，截断过长内容。
    """
    MAX_CONTENT_LENGTH = 2000
    serialized = []
    for msg in messages:
        item = {"role": msg.get("role", "unknown")}
        content = msg.get("content")
        if isinstance(content, str) and len(content) > MAX_CONTENT_LENGTH:
            item["content"] = content[:MAX_CONTENT_LENGTH] + "...[truncated]"
            item["_content_original_length"] = len(content)
        else:
            item["content"] = content
        if msg.get("tool_calls"):
            item["tool_calls"] = [
                {
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"],
                }
                for tc in msg["tool_calls"]
            ]
        if msg.get("tool_call_id"):
            item["tool_call_id"] = msg["tool_call_id"]
        serialized.append(item)
    return serialized
