"""ConversationContext.build_messages 单元测试"""

from agent.context import ConversationContext


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


def test_history_not_trimmed_under_limit():
    ctx = ConversationContext(system_prompt="SYS", max_history=10)
    history = [{"role": "user", "content": str(i)} for i in range(5)]
    msgs = ctx.build_messages(history=history)
    assert len(msgs) == 1 + 5  # 全部保留
