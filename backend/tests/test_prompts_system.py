"""build_system_prompt 单元测试"""

from agent.prompts.system import build_system_prompt


def test_contains_role_and_guidance(sample_summary):
    prompt = build_system_prompt(sample_summary)
    assert "BMC" in prompt  # 角色段
    assert "When the user asks" in prompt  # 工具指导段


def test_data_context_rendered_with_values(sample_summary):
    prompt = build_system_prompt({**sample_summary, "totalLines": 42, "errorCount": 7})
    assert "Total log entries: 42" in prompt
    assert "Error entries (ERROR): 7" in prompt


def test_components_joined_top15():
    summary = {
        "totalLines": 0, "errorCount": 0, "warningCount": 0,
        "noticeCount": 0, "launchCount": 0,
        "components": [f"comp{i}" for i in range(20)],
        "timeRange": {"start": "s", "end": "e"},
    }
    prompt = build_system_prompt(summary)
    assert "comp0" in prompt and "comp14" in prompt  # 前 15
    assert "comp15" not in prompt  # 第 16 个被截断


def test_time_unknown_when_missing():
    prompt = build_system_prompt(
        {"totalLines": 0, "errorCount": 0, "warningCount": 0,
         "noticeCount": 0, "launchCount": 0, "components": []}
    )
    # 缺 timeRange 时应显示 unknown
    assert "unknown" in prompt


def test_no_template_syntax_leftover(sample_summary):
    prompt = build_system_prompt(sample_summary)
    # 所有提供的变量都应被替换，不应残留 ${...
    assert "${" not in prompt
