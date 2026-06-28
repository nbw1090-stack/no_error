"""build_system_prompt 测试 —— 数据上下文 / 源码检索段注入 / 无数据集 QA 模式。"""

from agent.prompts.system import build_system_prompt


_SUMMARY = {
    "totalLines": 10,
    "errorCount": 2,
    "warningCount": 1,
    "noticeCount": 1,
    "launchCount": 1,
    "components": ["pcie_device", "sensor"],
    "timeRange": {"start": "2025-01-01 00:00:00", "end": "2025-01-01 01:00:00"},
}


def test_with_source_components_injects_guidance():
    prompt = build_system_prompt(_SUMMARY, source_components=["sensor", "pcie_device"])
    # 源码检索段被注入，并列出了已索引组件
    assert "INDEXED SOURCE CODE" in prompt
    assert "sensor" in prompt
    assert "pcie_device" in prompt
    # 数据上下文段照常存在
    assert "Total log entries: 10" in prompt


def test_without_source_components_omits_guidance():
    prompt = build_system_prompt(_SUMMARY)
    assert "INDEXED SOURCE CODE" not in prompt
    assert "Total log entries: 10" in prompt


def test_empty_source_components_omits_guidance():
    # 空列表也应跳过源码段（不打扰 LLM）
    prompt = build_system_prompt(_SUMMARY, source_components=[])
    assert "INDEXED SOURCE CODE" not in prompt


def test_no_dataset_falls_back_to_qa_mode():
    prompt = build_system_prompt(None)
    # QA 模式提示词，不含数据上下文
    assert "Q&A mode" in prompt
    assert "Total log entries" not in prompt
    assert "INDEXED SOURCE CODE" not in prompt


def test_no_dataset_with_source_components_injects_source_guidance():
    # 无数据集但有已索引源码 → 源码问答模式：注入源码引导，不走纯 QA
    prompt = build_system_prompt(None, source_components=["sensor"])
    assert "INDEXED SOURCE CODE" in prompt             # 源码引导已注入
    assert "Q&A mode" not in prompt                     # 不再走纯 QA 角色
    assert "source analysis assistant" in prompt        # QA_SOURCE_ROLE 角色


def test_no_dataset_with_source_components_counts_rendered():
    # 无日志模式下 dict 形态同样富格式渲染
    prompt = build_system_prompt(
        None,
        source_components=[
            {"name": "sensor", "file_count": 145, "symbol_count": 3610},
        ],
    )
    assert "sensor (145 files, 3610 symbols)" in prompt
    assert "INDEXED SOURCE CODE" in prompt


def test_source_components_with_counts_rendered():
    """dict 形态的 source_components 渲染为 'name (N files, M symbols)'。"""
    prompt = build_system_prompt(
        _SUMMARY,
        source_components=[
            {"name": "sensor", "file_count": 145, "symbol_count": 3610},
        ],
    )
    assert "INDEXED SOURCE CODE" in prompt
    assert "sensor (145 files, 3610 symbols)" in prompt
    # HARD RULE 与 summarize_component 引导已注入
    assert "HARD RULE" in prompt
    assert "summarize_component" in prompt


def test_source_components_backward_compat_strings():
    """旧 str 列表形态仍能正常渲染（向后兼容）。"""
    prompt = build_system_prompt(
        _SUMMARY, source_components=["sensor", "pcie_device"]
    )
    assert "INDEXED SOURCE CODE" in prompt
    assert "sensor" in prompt
    assert "pcie_device" in prompt


def test_tool_guidance_documents_aggregation():
    """数据分析模式注入聚合分组返回结构的引导。"""
    prompt = build_system_prompt(_SUMMARY)
    assert "GROUPED BY MESSAGE" in prompt  # 分组聚合说明
    assert "collapse into ONE group" in prompt  # 重复错误合并为一组
    assert "sample_ids" in prompt
    assert "interval" in prompt
    assert "dedup=false" in prompt
    assert "get_context_around" in prompt  # 下钻引导
