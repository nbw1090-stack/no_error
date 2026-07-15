"""
claw-code 风格上下文压缩测试

覆盖（对应任务书的设计要求）：
1. summarize_messages 七段式模板：结构 / 中英待办关键词 / 文件提取 / 截断 / 确定性
2. 边界安全：分割点落在 tool 消息上时回退，压缩后首条保留消息绝不是孤儿 tool
3. 冻结 / 前缀稳定：触发之间前缀序列化后逐字节相同；触发后再次冻结
4. 二次压缩 merge：Scope 累加、并集去重、旧时间线丢弃、预算裁剪优先级
5. Session.compaction 持久化 round-trip + 旧文件向后兼容
6. Agent 集成：超预算后写入 compaction、发给 LLM 的消息含摘要且不含被压缩原文、
   elide 模式行为不受影响

背景：旧的「每轮多省一条」省略机制逐轮改写历史前缀，DeepSeek 前缀缓存命中率
只有 ~20%；压缩机制的冻结不变式是本次改造的核心目的，故有专门的逐字节断言。
"""

import json

import pytest

import agent.tools  # noqa: F401  触发工具注册（Agent 集成测试需要真实工具）
from agent.compaction import (
    COMPACTION_PREAMBLE,
    build_compaction_message,
    merge_compact_summaries,
    safe_split_point,
    summarize_messages,
)
from agent.context import ConversationContext, ELIDED_TOOL_PLACEHOLDER
from agent.core import Agent
from agent.session import SessionManager

from conftest import FakeLLMAdapter


# ============================================================
# 消息构造辅助
# ============================================================


def _user(text):
    return {"role": "user", "content": text}


def _assistant(text):
    return {"role": "assistant", "content": text}


def _assistant_tc(*calls, content=None):
    """assistant 消息，tool_calls 为 [(id, name, args_dict), ...]"""
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": cid,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
            for cid, name, args in calls
        ],
    }


def _tool(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _serialize(messages):
    """逐字节比较用的序列化（sort_keys 消除 dict 顺序噪音）。"""
    return json.dumps(messages, ensure_ascii=False, sort_keys=True)


def _section_items(summary, header):
    """取摘要中某个多行段（如 "- Pending work:"）下的子项列表。"""
    lines = summary.splitlines()
    idx = lines.index(header)
    items = []
    for line in lines[idx + 1:]:
        if not line.startswith("  - "):
            break
        items.append(line[4:])
    return items


# ============================================================
# 1. summarize_messages：七段式模板摘要
# ============================================================


def _sample_messages():
    return [
        _user("排查 PCIe 初始化失败"),
        _assistant_tc(
            ("c1", "search_logs", {"query": "pcie"}),
            content="先搜日志",
        ),
        _tool("c1", '{"matches_count": 2, "results": ["PCIe card init failed"]}'),
        _assistant("找到了 src/pcie_card.lua 的报错。下一步还需确认电源状态。"),
        _user("好，继续查电源"),
        _assistant_tc(("c2", "get_summary", {})),
        _tool("c2", '{"errorCount": 2}'),
        _assistant("电源正常，结论：PCIe 卡初始化时序问题。"),
    ]


def test_summarize_has_seven_section_structure():
    summary = summarize_messages(_sample_messages())
    assert summary.startswith("<summary>")
    assert summary.endswith("</summary>")
    assert "Conversation summary:" in summary
    assert "- Scope: " in summary
    assert "- Tools mentioned: " in summary
    assert "- Recent user requests:" in summary
    assert "- Pending work:" in summary
    assert "- Key files referenced: " in summary
    assert "- Current work: " in summary
    assert "- Key timeline:" in summary


def test_summarize_scope_counts_roles():
    summary = summarize_messages(_sample_messages())
    # 8 条：user=2, assistant=4, tool=2
    assert (
        "- Scope: 8 earlier messages compacted (user=2, assistant=4, tool=2)."
        in summary
    )


def test_summarize_tools_from_tool_calls_deduped_sorted():
    messages = [
        _assistant_tc(("c1", "search_logs", {"q": "a"})),
        _assistant_tc(("c2", "search_logs", {"q": "b"}), ("c3", "get_summary", {})),
    ]
    summary = summarize_messages(messages)
    assert "- Tools mentioned: get_summary, search_logs." in summary


def test_summarize_recent_user_requests_last3_truncated():
    long_req = "这是一个非常长的用户请求" * 30  # >160 字符
    messages = [
        _user("请求一"),
        _user("请求二"),
        _user("请求三"),
        _user(long_req),
        _user("请求五"),
    ]
    summary = summarize_messages(messages)
    # 只保留最后 3 条，且按时间顺序
    assert "请求一" not in summary.split("- Key timeline:")[0]
    assert "  - 请求三" in summary
    assert "  - 请求五" in summary
    # 长请求被截到 160 字符并以省略号结尾
    request_lines = [
        l for l in summary.splitlines() if l.startswith("  - 这是一个非常长")
    ]
    assert request_lines
    assert len(request_lines[0]) <= 4 + 160
    assert request_lines[0].endswith("…")


def test_summarize_pending_keywords_chinese_and_english():
    messages = [
        _user("TODO: check the power rail"),
        _assistant("已完成第一步，下一步还需比对 sensor 阈值。"),
        _assistant("这句只是普通叙述。"),
    ]
    summary = summarize_messages(messages)
    pending = _section_items(summary, "- Pending work:")
    assert any("TODO: check the power rail" in p for p in pending)
    assert any("下一步还需比对" in p for p in pending)
    assert all("只是普通叙述" not in p for p in pending)


def test_summarize_pending_ignores_tool_result_noise():
    # 工具 JSON 里的 "next"/"剩余" 是字段名/数据，不应误报为待办
    messages = [
        _user("查一下"),
        _tool("c1", '{"next_page": 2, "剩余": 10}'),
    ]
    summary = summarize_messages(messages)
    assert "- Pending work:" not in summary


def test_summarize_key_files_paths_and_line_refs():
    messages = [
        _user("看看 src/pcie_card.lua 和 pcie_card.lua:49 的实现"),
        _assistant("对照 util.c:12 ；README.md 与 foo.exe 不算文件证据"),
        _assistant_tc(("c1", "get_function_at_line", {"file_path": "include/hwproxy.hpp"})),
    ]
    summary = summarize_messages(messages)
    files_line = next(
        l for l in summary.splitlines() if l.startswith("- Key files referenced: ")
    )
    assert "src/pcie_card.lua" in files_line
    assert "pcie_card.lua" in files_line  # 行号引用 xxx.lua:49 → 提取为路径
    assert "util.c" in files_line
    assert "include/hwproxy.hpp" in files_line  # tool_calls 参数里的路径也提取
    assert "README.md" not in files_line  # 裸文件名（无路径无行号）不算
    assert "foo.exe" not in files_line  # 后缀不在白名单


def test_summarize_current_work_is_last_assistant_text_truncated():
    long_text = "当前正在分析" * 60  # >200 字符
    messages = [
        _assistant("早期分析"),
        _user("继续"),
        _assistant(long_text),
        _tool("c1", "tool output"),  # 最后一条非 assistant，不应影响
    ]
    summary = summarize_messages(messages)
    current_line = next(
        l for l in summary.splitlines() if l.startswith("- Current work: ")
    )
    assert current_line.startswith("- Current work: 当前正在分析")
    assert len(current_line) <= len("- Current work: ") + 200
    assert current_line.endswith("…")


def test_summarize_timeline_renders_tool_use_and_tool_result():
    messages = [
        _user("找 PCIe 报错"),
        _assistant_tc(("c1", "search_logs", {"query": "pcie"})),
        _tool("c1", "x" * 500),
        _assistant("结论如下"),
    ]
    summary = summarize_messages(messages)
    timeline = summary.split("- Key timeline:")[1]
    lines = [l for l in timeline.splitlines() if l.startswith("  - ")]
    assert len(lines) == 4  # 逐条一行
    assert lines[0] == "  - user: 找 PCIe 报错"
    assert lines[1].startswith("  - assistant: tool_use search_logs(")
    assert "pcie" in lines[1]
    assert lines[2].startswith("  - tool: tool_result: xxx")
    # 每行内容截 160 字符（加 4 字符缩进前缀）
    assert all(len(l) <= 4 + 160 for l in lines)
    assert lines[2].endswith("…")


def test_summarize_is_deterministic():
    messages = _sample_messages()
    assert summarize_messages(messages) == summarize_messages(messages)


def test_build_compaction_message_is_system_with_preamble():
    msg = build_compaction_message("<summary>\nx\n</summary>")
    assert msg["role"] == "system"
    assert msg["content"].startswith(COMPACTION_PREAMBLE)
    assert "<summary>" in msg["content"]


# ============================================================
# 2. 边界安全：分割点回退
# ============================================================


def test_safe_split_point_backs_off_over_consecutive_tools():
    messages = [
        _user("q"),
        _assistant_tc(("a", "t1", {}), ("b", "t2", {})),  # 一条 assistant 两个调用
        _tool("a", "ra"),
        _tool("b", "rb"),
        _user("q2"),
    ]
    # 期望分割点 3（tool b）→ 连续回退跨过两条 tool，落到 assistant(tool_calls)
    assert safe_split_point(messages, 3) == 1
    assert safe_split_point(messages, 2) == 1
    # 落在非 tool 上则不动
    assert safe_split_point(messages, 4) == 4
    assert safe_split_point(messages, 1) == 1


def test_safe_split_point_respects_lower_bound():
    messages = [_tool("a", "r1"), _tool("b", "r2"), _tool("c", "r3"), _user("q")]
    # 全是 tool，退到下界为止（调用方据此判定"无法压缩"）
    assert safe_split_point(messages, 2, lower=0) == 0
    assert safe_split_point(messages, 2, lower=1) == 1


def test_compacted_first_preserved_message_never_orphan_tool():
    ctx = ConversationContext(
        "SYS", token_budget=10, compaction_mode="compact",
        preserve_recent_messages=3,
    )
    big = "x" * 600
    history = [
        _user("q" * 900),  # 首条足够大：压掉它砍掉 >30% 尾部（过显著性护栏）
        _assistant_tc(("a", "t1", {}), ("b", "t2", {})),
        _tool("a", big),
        _tool("b", big),
        _assistant("阶段结论"),
        _user("q2"),
    ]
    messages, compaction = ctx.build_messages_compacted(history, None)
    assert compaction is not None
    # 分割点上界 6-3=3 落在 tool 上 → 回退到 assistant(tool_calls)=1
    assert compaction["upto"] == 1
    # 输出：system / 摘要 system / assistant(tool_calls) / tool / tool / ...
    assert messages[2]["role"] == "assistant"
    assert messages[2].get("tool_calls")
    # 任何 tool 消息之前都必须有它的 assistant(tool_calls)（无孤儿 tool）
    assert messages[3]["role"] == "tool"
    for i, m in enumerate(messages):
        if m.get("role") == "tool":
            prev = messages[i - 1]
            assert prev["role"] in ("assistant", "tool")


# ============================================================
# 3. 冻结 / 前缀稳定（本次改造的目的，逐字节断言）
# ============================================================


def test_prefix_frozen_between_builds_and_after_compaction():
    ctx = ConversationContext(
        "SYS", token_budget=300, compaction_mode="compact",
        preserve_recent_messages=2,
    )
    big = "x" * 1500  # ~500 token，单条即超预算（低水位 = 300×0.6 = 180）

    # --- 阶段一：未超预算，连续构建之间前缀逐字节相同 ---
    h = [_user("q1"), _assistant("a1")]
    msgs_1, comp = ctx.build_messages_compacted(h, None)
    assert comp is None
    h = h + [_user("q2")]
    msgs_2, comp = ctx.build_messages_compacted(h, None)
    assert comp is None
    assert _serialize(msgs_2[: len(msgs_1)]) == _serialize(msgs_1)

    # --- 阶段二：超预算 → 触发一次压缩（低水位跳变） ---
    # 大工具结果之后已有小尾巴，分割点推进到「尾部 ≤ 低水位」即停：
    # 把大结果扫进摘要、保留其后的小结与追问
    h = h + [
        _assistant_tc(("c1", "search_logs", {"q": "pcie"})),
        _tool("c1", big),
        _assistant("阶段小结"),
        _user("q3"),
    ]
    msgs_3, comp_1 = ctx.build_messages_compacted(h, None)
    assert comp_1 is not None
    assert comp_1["count"] == 1
    assert comp_1["upto"] == 5  # 最小推进：扫掉 big（idx4），保留 小结+q3
    assert msgs_3[1]["role"] == "system" and "<summary>" in msgs_3[1]["content"]

    # --- 阶段三：触发之后冻结 ---
    # 完全相同的输入必须产出逐字节相同的消息序列
    msgs_4, comp = ctx.build_messages_compacted(h, comp_1)
    assert comp is None
    assert _serialize(msgs_4) == _serialize(msgs_3)

    # 又一轮大工具结果落在最少保留窗口内：虽再度超预算，但压掉窗口前的小
    # 消息砍不掉 30% 尾部 → 显著性护栏生效，保持冻结、不改写前缀
    h = h + [_assistant_tc(("c2", "search_logs", {"q": "fan"})), _tool("c2", big)]
    msgs_5, comp = ctx.build_messages_compacted(h, comp_1)
    assert comp is None
    assert _serialize(msgs_5[: len(msgs_4)]) == _serialize(msgs_4)

    # --- 阶段四：大结果滑出保留窗口后二次压缩，随后又一次冻结 ---
    h = h + [_assistant("第二阶段小结"), _user("q4")]
    msgs_6, comp_2 = ctx.build_messages_compacted(h, comp_1)
    assert comp_2 is not None
    assert comp_2["count"] == 2
    assert comp_2["upto"] == 9  # 扫掉第二个 big（idx8），保留 小结2+q4
    msgs_7, comp = ctx.build_messages_compacted(h, comp_2)
    assert comp is None
    assert _serialize(msgs_7) == _serialize(msgs_6)


def test_under_budget_no_compaction_messages_identical_to_history():
    ctx = ConversationContext("SYS", compaction_mode="compact")
    history = [_user("q"), _assistant("a")]
    messages, comp = ctx.build_messages_compacted(history, None)
    assert comp is None
    assert messages == [{"role": "system", "content": "SYS"}] + history


def test_compaction_does_not_mutate_session_messages():
    ctx = ConversationContext(
        "SYS", token_budget=10, compaction_mode="compact",
        preserve_recent_messages=1,
    )
    history = [_user("q"), _assistant("a" * 600), _user("q2")]
    snapshot = json.dumps(history, ensure_ascii=False)
    _, comp = ctx.build_messages_compacted(history, None)
    assert comp is not None
    # session.messages 是 append-only 存档，压缩绝不改写原文
    assert json.dumps(history, ensure_ascii=False) == snapshot


def test_over_count_triggers_compaction_without_sliding_window():
    # compact 模式不做滑动窗口条数裁剪（会破坏前缀稳定），
    # 尾部条数超 max_history 时改为触发压缩来兜底
    ctx = ConversationContext(
        "SYS", max_history=10, token_budget=100_000,
        compaction_mode="compact", preserve_recent_messages=4,
    )
    history = [_user(f"m{i}") for i in range(20)]
    messages, comp = ctx.build_messages_compacted(history, None)
    assert comp is not None
    assert comp["upto"] == 16
    # system + 摘要 + 保留 4 条
    assert len(messages) == 2 + 4


# ============================================================
# 4. 二次压缩 merge
# ============================================================


def test_merge_none_existing_returns_new():
    s = summarize_messages([_user("q")])
    assert merge_compact_summaries(None, s) == s


def test_merge_scope_accumulates():
    s1 = summarize_messages([_user("q1"), _assistant("a1"), _tool("c1", "r")])
    s2 = summarize_messages([_user("q2"), _assistant("a2")])
    merged = merge_compact_summaries(s1, s2)
    assert (
        "- Scope: 5 earlier messages compacted (user=2, assistant=2, tool=1)."
        in merged
    )


def test_merge_tools_union_pending_files_dedup_old_timeline_dropped():
    old_msgs = [
        _user("OLD_TIMELINE_MARKER 查 src/a.lua"),
        _assistant_tc(("c1", "search_logs", {})),
        _assistant("下一步还需复核 src/a.lua"),
    ]
    new_msgs = [
        _user("NEW_TIMELINE_MARKER 查 src/b.c"),
        _assistant_tc(("c2", "get_summary", {})),
        _assistant("下一步还需复核 src/a.lua"),  # 与旧 pending 完全相同 → 去重
    ]
    merged = merge_compact_summaries(
        summarize_messages(old_msgs), summarize_messages(new_msgs)
    )
    # 工具并集
    assert "- Tools mentioned: get_summary, search_logs." in merged
    # 文件并集去重
    files_line = next(
        l for l in merged.splitlines() if l.startswith("- Key files referenced: ")
    )
    assert files_line.count("src/a.lua") == 1
    assert "src/b.c" in files_line
    # pending 去重（旧+新相同的一条在 Pending work 段内只出现一次；
    # Current work / 新时间线里出现同文属正常，不在断言范围）
    pending = _section_items(merged, "- Pending work:")
    assert pending.count("下一步还需复核 src/a.lua") == 1
    # 时间线只保留新的，旧时间线丢弃
    timeline = merged.split("- Key timeline:")[1]
    assert "NEW_TIMELINE_MARKER" in timeline
    assert "OLD_TIMELINE_MARKER" not in timeline


def test_merge_is_composable_three_times():
    # 合并结果仍是同一七段结构 → 可被再次解析合并（Scope 持续累加）
    s1 = summarize_messages([_user("q1")])
    s2 = summarize_messages([_user("q2"), _assistant("a2")])
    s3 = summarize_messages([_tool("c", "r")])
    merged = merge_compact_summaries(merge_compact_summaries(s1, s2), s3)
    assert (
        "- Scope: 4 earlier messages compacted (user=2, assistant=1, tool=1)."
        in merged
    )


def test_merge_budget_trim_prefers_pending_over_timeline():
    # 构造大摘要：大量时间线 + 一条待办，收紧预算后待办必须活下来
    msgs = [_user(f"消息内容 {i} " + "填充" * 40) for i in range(30)]
    msgs.append(_assistant("下一步还需检查 sensor 电源轨。"))
    summary = summarize_messages(msgs)
    assert len(summary) > 800
    trimmed = merge_compact_summaries(None, summary, max_chars=800)
    assert len(trimmed) <= 800
    # 优先级：pending 保留、timeline 被裁、有省略提示、结构闭合
    assert "- Pending work:" in trimmed
    assert "下一步还需检查 sensor 电源轨。" in trimmed
    assert "行已按预算省略" in trimmed
    assert trimmed.splitlines()[-1] == "</summary>"
    original_timeline_lines = summary.split("- Key timeline:")[1].count("\n  - ")
    trimmed_timeline_lines = trimmed.count("\n  - 消息内容")
    assert trimmed_timeline_lines < original_timeline_lines


def test_merge_trim_keeps_scope_and_tools():
    msgs = [_assistant_tc(("c1", "search_logs", {}))] + [
        _user("x" * 150) for _ in range(50)
    ]
    trimmed = merge_compact_summaries(None, summarize_messages(msgs), max_chars=400)
    assert len(trimmed) <= 400
    assert "- Scope: " in trimmed  # 骨架行（优先级 0）永远保留
    assert "- Tools mentioned: search_logs." in trimmed


# ============================================================
# 5. Session.compaction 持久化
# ============================================================


def test_session_compaction_roundtrip(tmp_path):
    sm = SessionManager(str(tmp_path / "sessions"))
    session = sm.create("ds-1")
    assert session.compaction is None
    payload = {"upto": 5, "summary": "<summary>\nx\n</summary>", "count": 2}
    sm.update_compaction(session.session_id, payload)
    reloaded = sm.get(session.session_id)
    assert reloaded.compaction == payload
    # messages 不受影响（append-only 存档）
    assert reloaded.messages == session.messages


def test_session_old_file_without_compaction_field_loads_as_none(tmp_path):
    # 向后兼容：旧版本 session JSON 没有 compaction 字段
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    old = {
        "session_id": "legacy01",
        "dataset_id": "ds-1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "messages": [{"role": "user", "content": "hi"}],
    }
    (sessions_dir / "legacy01.json").write_text(json.dumps(old))
    sm = SessionManager(str(sessions_dir))
    session = sm.get("legacy01")
    assert session is not None
    assert session.compaction is None
    # 旧文件也能被 update_compaction 正常升级
    sm.update_compaction("legacy01", {"upto": 1, "summary": "s", "count": 1})
    assert sm.get("legacy01").compaction["upto"] == 1


def test_update_compaction_missing_session_raises(tmp_path):
    sm = SessionManager(str(tmp_path / "sessions"))
    with pytest.raises(ValueError, match="Session not found"):
        sm.update_compaction("nope", {"upto": 1, "summary": "s", "count": 1})


# ============================================================
# 6. Agent 集成
# ============================================================


def _new_agent(tmp_path, responses, **kwargs):
    sm = SessionManager(str(tmp_path / "sessions"))
    session = sm.create("ds-test")
    agent = Agent(
        llm=FakeLLMAdapter(responses),
        session_manager=sm,
        **kwargs,
    )
    return agent, sm, session


async def test_run_compacts_session_and_sends_summary(tmp_path, sample_dataset, tc):
    # 两轮真实工具（结果几百字符）+ 极小预算 → 必然触发压缩
    agent, sm, session = _new_agent(
        tmp_path,
        [
            [tc("get_summary", {}, "c1")],
            [tc("get_time_range", {}, "c2")],
            "分析完成",
        ],
        context_token_budget=50,
        compaction_preserve_recent=2,
    )
    reply = await agent.run(session.session_id, "有什么概览", sample_dataset)
    assert reply == "分析完成"

    # --- compaction 已持久化到 session ---
    stored = sm.get(session.session_id)
    assert stored.compaction is not None
    assert stored.compaction["count"] >= 1
    assert 0 < stored.compaction["upto"] < len(stored.messages)
    assert "<summary>" in stored.compaction["summary"]
    assert "get_summary" in stored.compaction["summary"]  # Tools mentioned

    # --- 原始消息存档完好（append-only，原文可追溯） ---
    roles = [m["role"] for m in stored.messages]
    assert roles == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
    first_tool_original = stored.messages[2]["content"]
    assert "total_lines" in first_tool_original  # get_summary 的真实结果未被改写

    # --- 发给 LLM 的最后一次调用：含摘要消息、不含被压缩的原文 ---
    last_messages = agent.llm.calls[-1]["messages"]
    assert last_messages[0]["role"] == "system"  # 主 system prompt 在首位
    assert last_messages[1]["role"] == "system"
    assert "<summary>" in last_messages[1]["content"]
    assert COMPACTION_PREAMBLE.split("。")[0] in last_messages[1]["content"]
    upto = stored.compaction["upto"]
    for compacted in stored.messages[:upto]:
        if compacted["role"] == "tool":
            # 被压缩区的工具结果原文不再出现在发给 LLM 的消息里
            assert all(
                m.get("content") != compacted["content"] for m in last_messages
            )
    # 摘要之后的首条保留消息不是孤儿 tool
    assert last_messages[2]["role"] != "tool"


async def test_run_stream_compacts_session(tmp_path, sample_dataset, tc):
    agent, sm, session = _new_agent(
        tmp_path,
        [
            [tc("get_summary", {}, "c1")],
            [tc("get_time_range", {}, "c2")],
            "done",
        ],
        context_token_budget=50,
        compaction_preserve_recent=2,
    )
    events = [
        e async for e in agent.run_stream(session.session_id, "q", sample_dataset)
    ]
    assert events[-1]["type"] == "done"
    stored = sm.get(session.session_id)
    assert stored.compaction is not None
    last_messages = agent.llm.stream_calls[-1]["messages"]
    assert "<summary>" in last_messages[1]["content"]


async def test_run_no_compaction_under_budget(tmp_path, sample_dataset, tc):
    # 默认预算（24000）下小对话不触发压缩：行为与旧机制一致、前缀零改写
    agent, sm, session = _new_agent(
        tmp_path, [[tc("get_summary", {}, "c1")], "done"]
    )
    await agent.run(session.session_id, "q", sample_dataset)
    assert sm.get(session.session_id).compaction is None
    # 无压缩时发给 LLM 的历史就是 session.messages 原样
    assert all(
        "<summary>" not in (m.get("content") or "")
        for call in agent.llm.calls
        for m in call["messages"]
    )


async def test_elide_mode_unaffected_by_compaction(tmp_path, sample_dataset, tc):
    # 回退开关：elide 模式必须保持旧行为——占位符省略、不写 compaction
    agent, sm, session = _new_agent(
        tmp_path,
        [
            [tc("get_summary", {}, "c1")],
            [tc("get_time_range", {}, "c2")],
            "done",
        ],
        context_token_budget=50,
        recent_tools_keep=0,
        context_compaction="elide",
    )
    reply = await agent.run(session.session_id, "q", sample_dataset)
    assert reply == "done"
    stored = sm.get(session.session_id)
    assert stored.compaction is None  # elide 模式绝不写压缩状态
    # 超预算 → 旧机制的占位符出现在发给 LLM 的消息里
    last_messages = agent.llm.calls[-1]["messages"]
    assert any(
        m.get("role") == "tool" and m.get("content") == ELIDED_TOOL_PLACEHOLDER
        for m in last_messages
    )
    assert all("<summary>" not in (m.get("content") or "") for m in last_messages)
