"""ConversationContext.build_messages 单元测试"""

from agent.context import (
    ConversationContext,
    ELIDED_TOOL_PLACEHOLDER,
    estimate_tokens,
)


def test_system_prepended_always():
    ctx = ConversationContext(system_prompt="SYS")
    msgs = ctx.build_messages(history=[])
    assert msgs[0] == {"role": "system", "content": "SYS"}


def test_user_message_appended_when_truthy():
    ctx = ConversationContext(system_prompt="SYS")
    msgs = ctx.build_messages(history=[], user_message="hi")
    assert msgs[-1] == {"role": "user", "content": "hi"}


def test_user_message_omitted_when_none():
    ctx = ConversationContext(system_prompt="SYS")
    msgs = ctx.build_messages(history=[{"role": "user", "content": "old"}])
    assert msgs[-1] == {"role": "user", "content": "old"}
    assert all(m.get("content") != "hi" for m in msgs)


def test_empty_user_message_string_not_appended():
    # 区分 "" 与真值：空串是 falsy，不应追加
    ctx = ConversationContext(system_prompt="SYS")
    msgs = ctx.build_messages(history=[], user_message="")
    assert msgs == [{"role": "system", "content": "SYS"}]


def test_history_trimmed_to_max_history():
    ctx = ConversationContext(system_prompt="SYS", max_history=10)
    history = [{"role": "user", "content": str(i)} for i in range(50)]
    msgs = ctx.build_messages(history=history)
    # system + 10 条历史（最后 10 条），不含 user_message
    body = msgs[1:]
    assert len(body) == 10
    assert body[-1]["content"] == "49"  # 保留最近的


def test_trim_drops_leading_orphan_tool_messages():
    # 回归：条数裁剪从 (assistant tool_calls → tool) 配对中间切开时，裁剪后历史
    # 不得以孤儿 tool 消息开头，否则 DeepSeek/OpenAI 报 400。
    ctx = ConversationContext(system_prompt="SYS", max_history=3)
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c0"}]},
        _tool_msg("c0", "r0"),
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        _tool_msg("c1", "r1"),
    ]
    msgs = ctx.build_messages(history=history)
    roles = [m["role"] for m in msgs]
    # 朴素切片 [-3:] = [tool c0, assistant c1, tool c1]，孤儿 tool c0 被丢弃
    assert roles == ["system", "assistant", "tool"]
    assert msgs[1]["tool_calls"][0]["id"] == "c1"


def test_history_not_trimmed_under_limit():
    ctx = ConversationContext(system_prompt="SYS", max_history=10)
    history = [{"role": "user", "content": str(i)} for i in range(5)]
    msgs = ctx.build_messages(history=history)
    assert len(msgs) == 1 + 5  # 全部保留


# ============================================================
# 陈旧工具结果省略（token 预算）
# ============================================================

def _tool_msg(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _big(n: int) -> str:
    return "x" * n


def test_under_budget_passes_tool_results_unchanged():
    # 不超预算 → 前缀稳定，工具结果完整保留（吃满前缀缓存）
    ctx = ConversationContext("SYS", token_budget=100_000, recent_tools_keep=3)
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
        _tool_msg("c1", _big(300)),
    ]
    msgs = ctx.build_messages(history=history)
    assert msgs[-1]["content"] == _big(300)


def test_over_budget_elides_oldest_tools_keeps_recent():
    ctx = ConversationContext("SYS", token_budget=500, recent_tools_keep=2)
    history = []
    for i in range(5):
        history.append({"role": "assistant", "content": f"step{i}"})
        history.append(_tool_msg(f"c{i}", _big(3000)))  # 每条 ~1000 tokens

    msgs = ctx.build_messages(history=history)
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 5  # 不删除消息，只替换内容
    # 最近两条完整
    assert tool_msgs[-1]["content"] == _big(3000)
    assert tool_msgs[-2]["content"] == _big(3000)
    # 更早的省略为占位符，且保留 tool_call_id（结构合法）
    assert tool_msgs[0]["content"] == ELIDED_TOOL_PLACEHOLDER
    assert tool_msgs[0]["tool_call_id"] == "c0"


def test_elision_preserves_message_order_and_pairing():
    ctx = ConversationContext("SYS", token_budget=10, recent_tools_keep=1)
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c0"}]},
        _tool_msg("c0", _big(3000)),
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        _tool_msg("c1", _big(3000)),
    ]
    msgs = ctx.build_messages(history=history)
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool"]


def test_elision_does_not_mutate_input_history():
    ctx = ConversationContext("SYS", token_budget=10, recent_tools_keep=0)
    original = _big(3000)
    history = [_tool_msg("c0", original)]
    ctx.build_messages(history=history)
    # 原始 history 不被改写（原文仍在 session 存档可追溯）
    assert history[0]["content"] == original


def test_budget_zero_disables_elision():
    ctx = ConversationContext("SYS", token_budget=0, recent_tools_keep=1)
    history = [_tool_msg("c0", _big(9000)), _tool_msg("c1", _big(9000))]
    msgs = ctx.build_messages(history=history)
    assert all(
        m["content"] == _big(9000) for m in msgs if m["role"] == "tool"
    )


def test_estimate_tokens_handles_types():
    assert estimate_tokens("abc" * 3) > 0
    assert estimate_tokens({"a": "b" * 30}) > 0
    assert estimate_tokens(None) == 0
