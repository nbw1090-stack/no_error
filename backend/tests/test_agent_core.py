"""Agent 核心 ReAct 循环测试 —— 用 FakeLLMAdapter 打桩，零网络"""

import json

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
    # 该测试会话 user_id=0（无已索引源码组件）→ 只暴露日志工具
    # （源码工具已按 group 解耦，仅在有已索引组件时才暴露）
    assert first_call["tools"] == ToolRegistry.get_schemas(groups={"log"})


async def test_react_last_iteration_forces_answer_without_tools(
    tmp_path, sample_dataset, tc
):
    # 最后一轮应收走工具 + 注入收尾指令，逼模型用已有信息直接作答
    agent, _, session = _new_agent(
        tmp_path, [[tc("get_summary", {})], "最终答案"], max_iterations=2
    )
    reply = await agent.run(session.session_id, "q", sample_dataset)
    assert reply == "最终答案"
    last_call = agent.llm.calls[-1]
    assert last_call["tools"] == []  # 最后一轮不暴露工具
    assert any(
        m["role"] == "system" and "最后一步" in m["content"]
        for m in last_call["messages"]
    )


async def test_react_repeated_unproductive_call_warns(tmp_path, sample_dataset, tc):
    # 同一工具+同参数连续返回 error（无效）→ 第二次结果追加换策略提示
    agent, sm, session = _new_agent(
        tmp_path,
        [
            [tc("does_not_exist", {}, "c1")],
            [tc("does_not_exist", {}, "c2")],
            "done",
        ],
    )
    reply = await agent.run(session.session_id, "q", sample_dataset)
    assert reply == "done"
    tool_msgs = [
        m for m in sm.get(session.session_id).messages if m["role"] == "tool"
    ]
    assert len(tool_msgs) == 2
    assert "调用策略提示" not in tool_msgs[0]["content"]  # 第一次不提示
    assert "调用策略提示" in tool_msgs[1]["content"]  # 第二次提示换策略
    assert "does_not_exist" in tool_msgs[1]["content"]


async def test_react_distinct_calls_not_warned(tmp_path, sample_dataset, tc):
    # 同工具但参数不同（不同符号名）→ 不算重复，不应提示
    agent, sm, session = _new_agent(
        tmp_path,
        [
            [tc("does_not_exist", {"name": "a"}, "c1")],
            [tc("does_not_exist", {"name": "b"}, "c2")],
            "done",
        ],
    )
    await agent.run(session.session_id, "q", sample_dataset)
    tool_msgs = [
        m for m in sm.get(session.session_id).messages if m["role"] == "tool"
    ]
    assert all("调用策略提示" not in m["content"] for m in tool_msgs)


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


async def test_stream_last_iteration_forces_answer_without_tools(
    tmp_path, sample_dataset, tc
):
    # 流式版与 run() 一致：最后一轮收走工具 + 注入收尾指令
    agent, _, session = _new_agent(
        tmp_path, [[tc("get_summary", {})], "收尾回答"], max_iterations=2
    )
    events = await _collect(agent, session.session_id, "q", sample_dataset)
    texts = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "收尾回答" in texts
    last_call = agent.llm.stream_calls[-1]
    assert last_call["tools"] == []
    assert any(
        m["role"] == "system" and "最后一步" in m["content"]
        for m in last_call["messages"]
    )


async def test_stream_empty_reply_retries(tmp_path, sample_dataset):
    agent, _, session = _new_agent(tmp_path, ["", "answer"])
    events = await _collect(agent, session.session_id, "q", sample_dataset)
    texts = [e["text"] for e in events if e["type"] == "delta"]
    assert "answer" in texts
    assert events[-1]["type"] == "done"


# ============================================================
# 无日志会话解耦：dataset=None 但 session 归属用户有已索引组件
# → 源码工具被暴露并执行（日志工具不暴露）
# ============================================================

async def test_no_dataset_session_runs_source_tool_when_indexed(
    tmp_path, tc, monkeypatch
):
    """无日志会话但归属用户有已索引组件 → 暴露并执行源码工具。"""
    from ast_analysis import db as ast_db

    # 隔离 ast.db 到 tmp_path，给 user_id=42 建一个组件索引（直接写 DB，绕过 clone）
    ast_db_path = str(tmp_path / "ast.db")
    monkeypatch.setattr(ast_db, "AST_DB_PATH", ast_db_path)
    ast_db.init_ast_db()
    ast_db.replace_component(
        42,
        "sensor",
        "git://x",
        "main",
        "deadbeef",
        "2026-01-01T00:00:00Z",
        [
            {
                "rel_path": "src/main.lua",
                "language": "lua",
                "symbol_count": 1,
                "symbols": [
                    {
                        "name": "init",
                        "kind": "function_definition",
                        "start_line": 1,
                        "end_line": 3,
                    }
                ],
                "node_types": {},
            }
        ],
    )

    sm = SessionManager(str(tmp_path / "sessions"))
    session = sm.create("ds-none", user_id=42)  # 无日志会话，归属 user_id=42
    agent = Agent(
        llm=FakeLLMAdapter(
            [[tc("summarize_component", {"component": "sensor"})], "done"]
        ),
        session_manager=sm,
    )

    reply = await agent.run(session.session_id, "sensor 是做什么的", dataset=None)
    assert reply == "done"

    # 源码工具被暴露；日志工具未暴露（无日志）
    first_call = agent.llm.calls[0]
    exposed = {t["function"]["name"] for t in first_call["tools"]}
    assert "summarize_component" in exposed
    assert "search_logs" not in exposed

    # 源码工具确实被执行（session 有 tool 消息，且返回正常概览而非 error）
    tool_msgs = [
        m for m in sm.get(session.session_id).messages if m["role"] == "tool"
    ]
    assert tool_msgs
    payload = json.loads(tool_msgs[0]["content"])
    assert payload.get("component") == "sensor"
    assert payload.get("totals", {}).get("file_count") == 1
    assert "error" not in payload


# ============================================================
# token usage 聚合（整轮 input/output/total）
# ============================================================

async def test_run_aggregates_usage_across_iterations(tmp_path, sample_dataset, tc):
    """run() 把工具轮 + 文本轮的 usage 累加写回传入的 usage 累加器。"""
    sm = SessionManager(str(tmp_path / "sessions"))
    session = sm.create("ds-test")
    agent = Agent(
        llm=FakeLLMAdapter(
            [[tc("get_summary", {})], "done"],
            usages=[
                {"input": 100, "output": 10, "total": 110},
                {"input": 200, "output": 20, "total": 220},
            ],
        ),
        session_manager=sm,
    )
    usage = {}
    await agent.run(session.session_id, "q", sample_dataset, usage=usage)
    assert usage == {"input": 300, "output": 30, "total": 330}


async def test_run_usage_zero_when_adapter_silent(tmp_path, sample_dataset):
    """适配器不带 usage 时累加器保持全 0（不报错）。"""
    sm = SessionManager(str(tmp_path / "sessions"))
    session = sm.create("ds-test")
    agent = Agent(llm=FakeLLMAdapter(["hi"]), session_manager=sm)
    usage = {}
    await agent.run(session.session_id, "q", sample_dataset, usage=usage)
    assert usage == {"input": 0, "output": 0, "total": 0}


async def test_run_stream_emits_usage_event_before_done(tmp_path, sample_dataset, tc):
    """run_stream() 在 done 前发出 usage 事件，数值为各轮 usage 之和。"""
    sm = SessionManager(str(tmp_path / "sessions"))
    session = sm.create("ds-test")
    agent = Agent(
        llm=FakeLLMAdapter(
            [[tc("get_summary", {})], "final answer"],
            usages=[
                {"input": 50, "output": 5, "total": 55},
                {"input": 70, "output": 7, "total": 77},
            ],
        ),
        session_manager=sm,
    )
    events = [
        e async for e in agent.run_stream(session.session_id, "q", sample_dataset)
    ]
    usage_events = [e for e in events if e["type"] == "usage"]
    assert len(usage_events) == 1
    assert usage_events[0] == {
        "type": "usage",
        "input": 120,
        "output": 12,
        "total": 132,
    }
    # usage 事件必须紧贴在最后一个 done 之前
    assert events[-1]["type"] == "done"
    assert events[-2]["type"] == "usage"


async def test_run_usage_not_persisted_into_session(tmp_path, sample_dataset, tc):
    """usage 是调用级元数据，不应进入会话历史（避免回放给 LLM 时混入多余字段）。"""
    sm = SessionManager(str(tmp_path / "sessions"))
    session = sm.create("ds-test")
    agent = Agent(
        llm=FakeLLMAdapter(
            [[tc("get_summary", {})], "done"],
            usages=[{"input": 1, "output": 1, "total": 2}] * 2,
        ),
        session_manager=sm,
    )
    usage = {}
    await agent.run(session.session_id, "q", sample_dataset, usage=usage)
    for m in sm.get(session.session_id).messages:
        assert "usage" not in m
