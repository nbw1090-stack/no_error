"""
OpenAI 兼容适配器

支持 OpenAI、Ollama、vLLM、LM Studio、Groq 等
所有提供 OpenAI 兼容 API 的服务。
"""

import logging
import time
from typing import AsyncGenerator

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

            # 构建 usage（含前缀缓存命中/未命中）
            usage_details = _extract_usage(response.usage)

            # 更新 generation 的输出。Langfuse usage_details 只放 input/output/total
            # （避免把缓存字段计入成本）；缓存命中指标放 metadata，便于直接查看命中率。
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
                usage_details=_langfuse_usage(usage_details),
                metadata={
                    "finish_reason": choice.finish_reason,
                    "duration_ms": round(duration_ms, 2),
                    **_cache_metadata(usage_details),
                },
            )

        return {
            "role": "assistant",
            "content": choice.message.content,
            "tool_calls": tool_calls,
            "usage": usage_details,
        }

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncGenerator[dict, None]:
        """
        流式发送消息，逐个 yield 标准化事件（content_delta/tool_call/done）。

        tool_calls 按 index 累积分片，finish_reason 后才整体放入 done.tool_calls，
        避免 arguments 半截 JSON 导致下游 json.loads 失败。
        """
        from observability import get_client

        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        logger.debug(
            "LLM stream request: model=%s, messages=%d, tools=%d",
            self.model,
            len(messages),
            len(tools) if tools else 0,
        )

        tracer = get_client()
        start_time = time.time()

        with tracer.observation(
            name="llm-chat-stream",
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
                "stream": True,
            },
        ) as generation:
            content_parts: list[str] = []
            # tool_calls 分片累积：{index: {id, type, function:{name, arguments}}}
            tool_calls_acc: dict[int, dict] = {}
            finish_reason: str | None = None
            usage_details: dict | None = None

            try:
                stream = await self.client.chat.completions.create(**kwargs)
                async for chunk in stream:
                    # usage 的位置因供应商而异：OpenAI 用独立的空-choices chunk，
                    # 而 DeepSeek 等把 usage 放在携带 finish_reason 的末 chunk（choices 非空）。
                    # 因此对每个 chunk 都检查 usage，避免漏掉。
                    if chunk.usage:
                        usage_details = _extract_usage(chunk.usage)

                    # 空-choices chunk（OpenAI 风格的纯 usage chunk）：已取 usage，跳过后续
                    if not chunk.choices:
                        continue

                    delta = chunk.choices[0].delta

                    # 文本增量：立即转发（真流式）
                    if delta.content:
                        content_parts.append(delta.content)
                        yield {"type": "content_delta", "text": delta.content}

                    # 工具调用分片：按 index 累积
                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            slot = tool_calls_acc.setdefault(
                                idx,
                                {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                },
                            )
                            if tc_delta.id:
                                slot["id"] = tc_delta.id
                            if tc_delta.type:
                                slot["type"] = tc_delta.type
                            fn = tc_delta.function
                            if fn and fn.name:
                                slot["function"]["name"] += fn.name
                            if fn and fn.arguments:
                                slot["function"]["arguments"] += fn.arguments

                    if chunk.choices[0].finish_reason:
                        finish_reason = chunk.choices[0].finish_reason
            except Exception as e:
                generation.update(status_message=str(e), level="ERROR")
                raise

            # 按索引排序得到完整 tool_calls 列表
            tool_calls = None
            if tool_calls_acc:
                tool_calls = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]

            full_content = "".join(content_parts)
            duration_ms = (time.time() - start_time) * 1000

            generation.update(
                output={
                    "content": full_content,
                    "tool_calls": [
                        {
                            "name": tc["function"]["name"],
                            "arguments": tc["function"]["arguments"],
                        }
                        for tc in (tool_calls or [])
                    ],
                },
                usage_details=_langfuse_usage(usage_details),
                metadata={
                    "finish_reason": finish_reason,
                    "duration_ms": round(duration_ms, 2),
                    **_cache_metadata(usage_details),
                },
            )

            yield {
                "type": "done",
                "finish_reason": finish_reason or "stop",
                "content": full_content,
                "tool_calls": tool_calls,
                "usage": usage_details,
            }


def _extract_usage(usage) -> dict | None:
    """
    把供应商 usage 对象规约成内部 dict：input/output/total + cache_hit/cache_miss。

    前缀缓存命中字段各家不同，这里兼容两种主流形态：
    - DeepSeek：usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens
      （二者之和即 prompt_tokens）。
    - OpenAI：usage.prompt_tokens_details.cached_tokens（命中），未命中 = 输入 - 命中。
    取不到缓存字段时 cache_hit=0、cache_miss=input（视作全部未命中），不影响 input/total。
    """
    if not usage:
        return None
    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0
    total_tokens = getattr(usage, "total_tokens", 0) or 0

    # DeepSeek 直接给出命中/未命中
    cache_hit = getattr(usage, "prompt_cache_hit_tokens", None)
    cache_miss = getattr(usage, "prompt_cache_miss_tokens", None)

    if cache_hit is None:
        # OpenAI 风格：prompt_tokens_details.cached_tokens
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", None) if details else None
        cache_hit = cached or 0
        cache_miss = max(0, input_tokens - cache_hit)
    else:
        cache_hit = cache_hit or 0
        cache_miss = cache_miss if cache_miss is not None else max(
            0, input_tokens - cache_hit
        )

    return {
        "input": input_tokens,
        "output": output_tokens,
        "total": total_tokens,
        "cache_hit": cache_hit,
        "cache_miss": cache_miss,
    }


def _langfuse_usage(usage_details: dict | None) -> dict | None:
    """只保留 input/output/total 给 Langfuse 计费，剔除缓存附加字段。"""
    if not usage_details:
        return None
    return {
        "input": usage_details.get("input", 0),
        "output": usage_details.get("output", 0),
        "total": usage_details.get("total", 0),
    }


def _cache_metadata(usage_details: dict | None) -> dict:
    """从 usage_details 提炼缓存命中指标（含命中率），供 Langfuse generation 展示。"""
    if not usage_details:
        return {}
    hit = usage_details.get("cache_hit", 0) or 0
    miss = usage_details.get("cache_miss", 0) or 0
    denom = hit + miss
    return {
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "cache_hit_rate": round(hit / denom, 4) if denom else 0.0,
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
