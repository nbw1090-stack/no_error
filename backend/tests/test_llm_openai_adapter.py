"""OpenAIAdapter 单元测试 —— 打桩 SDK 客户端，零网络

关键验证：chat_stream 中 tool_calls 分片按 index 累积拼接（最易出错的正确性点）。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config import LLMConfig
from agent.llm.openai_adapter import OpenAIAdapter


# ============================================================
# Fake 构造工具（模拟 OpenAI SDK 响应对象，仅做属性访问）
# ============================================================

def _sdk_tc(id_, name, arguments):
    """SDK 风格的 tool_call（chat 非流式响应里的形状）。"""
    return SimpleNamespace(
        id=id_, type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _tc_delta(index, id=None, type=None, name=None, arguments=None):
    """流式 tool_call 分片。"""
    return SimpleNamespace(
        index=index, id=id, type=type,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _resp(content=None, tool_calls=None, finish_reason="stop", usage=None):
    """非流式响应。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, tool_calls=tool_calls),
            finish_reason=finish_reason,
        )],
        usage=usage,
    )


def _chunk(content=None, tool_calls=None, finish_reason=None, choices=None, usage=None):
    """流式 chunk。choices 默认用 delta 构造；显式传 choices（如 []）用于 usage chunk。"""
    if choices is None:
        delta = SimpleNamespace(content=content, tool_calls=tool_calls)
        choices = [SimpleNamespace(delta=delta, finish_reason=finish_reason)]
    return SimpleNamespace(choices=choices, usage=usage)


def _usage(p=5, c=3, t=8):
    return SimpleNamespace(prompt_tokens=p, completion_tokens=c, total_tokens=t)


class FakeStream:
    """async iterable 的流式 chunk 序列。"""

    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        self._i = 0
        return self

    async def __anext__(self):
        if self._i >= len(self.chunks):
            raise StopAsyncIteration
        c = self.chunks[self._i]
        self._i += 1
        return c


def _make_adapter(create_return):
    """构造真实 OpenAIAdapter，并把 SDK 客户端替换为返回 create_return 的 fake。"""
    adapter = OpenAIAdapter(
        LLMConfig(provider="openai", model="test-model", api_key="x",
                  temperature=0.0, max_tokens=128)
    )
    create_mock = AsyncMock(return_value=create_return)
    adapter.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create_mock))
    )
    return adapter, create_mock


MESSAGES = [{"role": "user", "content": "hi"}]


# ============================================================
# chat() 非流式
# ============================================================

async def test_chat_no_tools_normalizes_content():
    adapter, create_mock = _make_adapter(_resp(content="hello"))
    result = await adapter.chat(MESSAGES)
    assert result == {"role": "assistant", "content": "hello", "tool_calls": None}
    kwargs = create_mock.call_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["messages"] == MESSAGES
    assert kwargs["temperature"] == 0.0
    assert kwargs["max_tokens"] == 128
    # 无 tools 时不应带 tools / tool_choice
    assert "tools" not in kwargs
    assert "tool_choice" not in kwargs


async def test_chat_with_tools_adds_tool_choice_auto():
    adapter, create_mock = _make_adapter(_resp(content="ok"))
    tools = [{"type": "function", "function": {"name": "x"}}]
    await adapter.chat(MESSAGES, tools=tools)
    kwargs = create_mock.call_args.kwargs
    assert kwargs["tools"] == tools
    assert kwargs["tool_choice"] == "auto"


async def test_chat_normalizes_tool_calls():
    sdk_tcs = [
        _sdk_tc("call_1", "search_logs", '{"keyword":"a"}'),
        _sdk_tc("call_2", "get_summary", "{}"),
    ]
    adapter, _ = _make_adapter(_resp(content=None, tool_calls=sdk_tcs, finish_reason="tool_calls"))
    result = await adapter.chat(MESSAGES, tools=[{}])
    assert result["tool_calls"] == [
        {"id": "call_1", "type": "function",
         "function": {"name": "search_logs", "arguments": '{"keyword":"a"}'}},
        {"id": "call_2", "type": "function",
         "function": {"name": "get_summary", "arguments": "{}"}},
    ]


async def test_chat_no_usage_does_not_crash():
    adapter, _ = _make_adapter(_resp(content="hi", usage=None))
    result = await adapter.chat(MESSAGES)
    assert result["content"] == "hi"  # usage=None 不应抛异常


async def test_chat_api_error_propagates():
    adapter, create_mock = _make_adapter(None)
    create_mock.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        await adapter.chat(MESSAGES)


# ============================================================
# chat_stream() 流式 —— index 累积是核心
# ============================================================

async def _collect(adapter, *args, **kwargs):
    return [e async for e in adapter.chat_stream(*args, **kwargs)]


async def test_stream_content_deltas_then_done():
    adapter, _ = _make_adapter(FakeStream([
        _chunk(content="Hel"),
        _chunk(content="lo"),
        _chunk(finish_reason="stop"),
    ]))
    events = await _collect(adapter, MESSAGES)
    deltas = [e for e in events if e["type"] == "content_delta"]
    assert [d["text"] for d in deltas] == ["Hel", "lo"]
    done = events[-1]
    assert done["type"] == "done"
    assert done["content"] == "Hello"
    assert done["tool_calls"] is None
    assert done["finish_reason"] == "stop"


async def test_stream_tool_call_split_across_two_chunks_same_index():
    # 同一 index 的 arguments 分两个 chunk 到达 → 必须拼接
    adapter, _ = _make_adapter(FakeStream([
        _chunk(tool_calls=[_tc_delta(0, id="c1", type="function", name="search_logs",
                                      arguments='{"keyword":"a')]),
        _chunk(tool_calls=[_tc_delta(0, arguments='bc"}')]),  # 续接 name/id 为 None
        _chunk(finish_reason="tool_calls"),
    ]))
    events = await _collect(adapter, MESSAGES)
    done = events[-1]
    assert done["tool_calls"] == [
        {"id": "c1", "type": "function",
         "function": {"name": "search_logs", "arguments": '{"keyword":"abc"}'}},
    ]
    assert done["finish_reason"] == "tool_calls"


async def test_stream_two_tool_calls_different_indices():
    adapter, _ = _make_adapter(FakeStream([
        _chunk(tool_calls=[_tc_delta(0, id="c0", type="function", name="search_logs",
                                      arguments='{"keyword":"x"}')]),
        _chunk(tool_calls=[_tc_delta(1, id="c1", type="function", name="get_summary",
                                      arguments="{}")]),
        _chunk(finish_reason="tool_calls"),
    ]))
    done = (await _collect(adapter, MESSAGES))[-1]
    # 按 index 排序，两个都在
    assert [tc["function"]["name"] for tc in done["tool_calls"]] == ["search_logs", "get_summary"]
    assert [tc["id"] for tc in done["tool_calls"]] == ["c0", "c1"]


async def test_stream_trailing_usage_chunk_no_choices():
    # 尾随的 usage chunk（choices 为空）应被安全处理，不崩
    adapter, _ = _make_adapter(FakeStream([
        _chunk(content="hi"),
        _chunk(finish_reason="stop"),
        _chunk(choices=[], usage=_usage()),  # usage chunk
    ]))
    events = await _collect(adapter, MESSAGES)
    assert events[-1]["content"] == "hi"  # 没有崩，内容完整


async def test_stream_finish_reason_defaults_stop_when_none():
    adapter, _ = _make_adapter(FakeStream([_chunk(content="hi")]))  # 无 finish_reason
    done = (await _collect(adapter, MESSAGES))[-1]
    assert done["finish_reason"] == "stop"


async def test_stream_content_and_tool_calls_mixed():
    adapter, _ = _make_adapter(FakeStream([
        _chunk(content="thinking...", tool_calls=[
            _tc_delta(0, id="c1", type="function", name="get_summary", arguments="{}"),
        ]),
        _chunk(finish_reason="tool_calls"),
    ]))
    events = await _collect(adapter, MESSAGES)
    assert any(e["type"] == "content_delta" and e["text"] == "thinking..." for e in events)
    done = events[-1]
    assert done["content"] == "thinking..."
    assert done["tool_calls"][0]["function"]["name"] == "get_summary"


async def test_stream_create_kwargs_include_stream_options():
    adapter, create_mock = _make_adapter(FakeStream([_chunk(finish_reason="stop")]))
    await _collect(adapter, MESSAGES, tools=[{}])
    kwargs = create_mock.call_args.kwargs
    assert kwargs["stream"] is True
    assert kwargs["stream_options"] == {"include_usage": True}
    assert "tools" in kwargs and kwargs["tool_choice"] == "auto"
