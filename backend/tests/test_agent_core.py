"""Agent 核心 ReAct 循环测试 —— 用 FakeLLMAdapter 打桩，零网络"""

import pytest

import agent.tools  # noqa: F401
from agent.core import Agent
from agent.session import SessionManager
from agent.tools.registry import ToolRegistry

# conftest 提供：sample_dataset / tc (make_tool_call) / FakeLLMAdapter 类
from conftest import FakeLLMAdapter


def _new_agent(tmp_path, responses, max_iterations=10):
    sm = SessionManager(str(tmp_path / "sessions"))
    session = sm.create("ds-test")
    agent = Agent(
        llm=FakeLLMAdapter(responses),
        session_manager=sm,
        max_iterations=max_iterations,
    )
    return agent, sm, session


# ============================================================
# run() 非流式
# ============================================================

async def test_react_single_tool_then_final(tmp_path, sample_dataset, tc):
    agent, sm, session = _new_agent(
        tmp_path, [[tc("get_summary", {})], "分析完成"]
    )
    reply = await agent.run(session.session_id, "有什么概览", sample_dataset)
    assert reply == "分析完成"
    assert len(agent.llm.calls) == 2  # 两轮 LLM 调用

    # 持久化：user / assistant(tool_calls) / tool / assistant(final)
    roles = [m["role"] for m in sm.get(session.session_id).messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    final = sm.get(session.session_id).messages[-1]
    assert final["content"] == "分析完成" and final.get("tool_calls") is None


async def test_react_multi_tool_calls_one_iteration(tmp_path, sample_dataset, tc):
    agent, sm, session = _new_agent(
        tmp_path,
        [[tc("get_summary", {}, "c1"), tc("get_time_range", {}, "c2")], "done"],
    )
    reply = await agent.run(session.session_id, "综合分析", sample_dataset)
    assert reply == "done"
    # 两条 tool 消息（同一轮两个工具）
    tool_msgs = [m for m in sm.get(session.session_id).messages if m["role"] == "tool"]
    assert len(tool_msgs) == 2


async def test_react_empty_reply_retries(tmp_path, sample_dataset):
    agent, _, session = _new_agent(tmp_path, ["", "real answer"])
    reply = await agent.run(session.session_id, "hi", sample_dataset)
    assert reply == "real answer"
    assert len(agent.llm.calls) == 2  # 空回复后重试


async def test_react_max_iterations_fallback(tmp_path, sample_dataset, tc):
    agent, _, session = _new_agent(
        tmp_path,
        [[tc("get_summary", {})], [tc("get_summary", {})], [tc("get_summary", {})]],
        max_iterations=2,
    )
    reply = await agent.run(session.session_id, "loop", sample_dataset)
    assert "超出当前处理轮次限制" in reply


async def test_react_missing_session_raises_valueerror(tmp_path, sample_dataset):
    agent, _, _ = _new_agent(tmp_path, ["x"])
    with pytest.raises(ValueError, match="Session not found"):
        await agent.run("no-such-session", "hi", sample_dataset)


async def test_react_tool_args_bad_json_falls_back_to_empty_dict(tmp_path, sample_dataset, tc):
    # arguments 非法 JSON → Agent 退化为 {}，get_summary(dataset) 仍能成功
    bad = {
        "id": "c1", "type": "function",
        "function": {"name": "get_summary", "arguments": "not-json"},
    }
    agent, _, session = _new_agent(tmp_path, [[bad], "done"])
    reply = await agent.run(session.session_id, "q", sample_dataset)
    assert reply == "done"  # 没有因非法 JSON 崩溃


async def test_react_unknown_tool_handled_by_registry(tmp_path, sample_dataset, tc):
    agent, sm, session = _new_agent(
        tmp_path, [[tc("does_not_exist", {})], "final"]
    )
    reply = await agent.run(session.session_id, "q", sample_dataset)
    assert reply == "final"
    # 未知工具的执行结果作为 tool 消息存入，且内含 error
    tool_msg = next(m for m in sm.get(session.session_id).messages if m["role"] == "tool")
    assert "Unknown tool" in tool_msg["content"]


async def test_react_records_messages_passed_to_llm(tmp_path, sample_dataset, tc):
    agent, _, session = _new_agent(tmp_path, [[tc("get_summary", {})], "done"])
    await agent.run(session.session_id, "用户问题", sample_dataset)
    first_call = agent.llm.calls[0]
    assert first_call["messages"][0]["role"] == "system"
    assert first_call["messages"][-1] == {"role": "user", "content": "用户问题"}
    assert first_call["tools"] == ToolRegistry.get_schemas()


# ============================================================
# run_stream() 流式
# ============================================================

async def _collect(agent, session_id, msg, dataset):
    return [e async for e in agent.run_stream(session_id, msg, dataset)]


async def test_stream_event_order_tool_then_done(tmp_path, sample_dataset, tc):
    agent, _, session = _new_agent(
        tmp_path, [[tc("get_summary", {})], "final answer"]
    )
    events = await _collect(agent, session.session_id, "q", sample_dataset)
    # tool_progress(start) → tool_progress(done) → delta(s) → done
    tp_start = next(i for i, e in enumerate(events) if e["type"] == "tool_progress" and e["status"] == "start")
    tp_done = next(i for i, e in enumerate(events) if e["type"] == "tool_progress" and e["status"] == "done")
    first_delta = next(i for i, e in enumerate(events) if e["type"] == "delta")
    assert tp_start < tp_done < first_delta
    assert events[-1]["type"] == "done"
    assert events[tp_start]["tool"] == "get_summary"


async def test_stream_tool_progress_around_each_tool(tmp_path, sample_dataset, tc):
    agent, _, session = _new_agent(
        tmp_path,
        [[tc("get_summary", {}, "c1"), tc("get_time_range", {}, "c2")], "done"],
    )
    events = await _collect(agent, session.session_id, "q", sample_dataset)
    progress = [e for e in events if e["type"] == "tool_progress"]
    # 两个工具 → 4 个 tool_progress（start/done × 2）
    assert len(progress) == 4
    statuses = [(p["tool"], p["status"]) for p in progress]
    assert ("get_summary", "start") in statuses
    assert ("get_summary", "done") in statuses
    assert ("get_time_range", "start") in statuses
    assert ("get_time_range", "done") in statuses


async def test_stream_final_assistant_persisted_once(tmp_path, sample_dataset, tc):
    agent, sm, session = _new_agent(
        tmp_path, [[tc("get_summary", {})], "final answer"]
    )
    await _collect(agent, session.session_id, "q", sample_dataset)
    final_assistants = [
        m for m in sm.get(session.session_id).messages
        if m["role"] == "assistant" and not m.get("tool_calls") and m.get("content")
    ]
    assert len(final_assistants) == 1
    assert final_assistants[0]["content"] == "final answer"


async def test_stream_tool_assistant_persisted_with_tool_calls_once(tmp_path, sample_dataset, tc):
    agent, sm, session = _new_agent(
        tmp_path, [[tc("get_summary", {})], "final"]
    )
    await _collect(agent, session.session_id, "q", sample_dataset)
    tool_call_msgs = [
        m for m in sm.get(session.session_id).messages
        if m["role"] == "assistant" and m.get("tool_calls")
    ]
    assert len(tool_call_msgs) == 1  # 只持久化一次


async def test_stream_max_iter_yields_fallback_then_done(tmp_path, sample_dataset, tc):
    agent, _, session = _new_agent(
        tmp_path, [[tc("get_summary", {})]], max_iterations=1
    )
    events = await _collect(agent, session.session_id, "q", sample_dataset)
    # 达 max_iterations → 兜底文案作为 delta 输出，最后 done
    # （兜底文案为中文无半角空格，run_stream.split(" ") 得单元素，故只 1 个 delta）
    deltas = [e for e in events if e["type"] == "delta"]
    assert deltas
    assert "超出当前处理轮次限制" in deltas[0]["text"]
    assert events[-1]["type"] == "done"


async def test_stream_empty_reply_retries(tmp_path, sample_dataset):
    agent, _, session = _new_agent(tmp_path, ["", "answer"])
    events = await _collect(agent, session.session_id, "q", sample_dataset)
    texts = [e["text"] for e in events if e["type"] == "delta"]
    assert "answer" in texts
    assert events[-1]["type"] == "done"
