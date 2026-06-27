"""ToolRegistry 单元测试 —— schema 导出、执行、错误处理"""

import json

import agent.tools  # noqa: F401  触发注册
from agent.tools.registry import ToolRegistry
from agent.tools.base import ToolDefinition


REGISTERED_TOOLS = {
    "search_logs",
    "filter_by_component",
    "filter_by_level",
    "get_summary",
    "get_component_stats",
    "get_time_range",
    "get_errors_by_component",
    "get_error_timeline",
    "get_context_around",
}


def test_registered_at_import():
    # 导入 agent.tools 即注册 9 个工具
    assert REGISTERED_TOOLS.issubset(set(ToolRegistry.get_names()))


def test_get_names_returns_registered():
    names = set(ToolRegistry.get_names())
    assert names >= REGISTERED_TOOLS


def test_get_schemas_openai_format():
    schemas = ToolRegistry.get_schemas()
    assert len(schemas) >= 9
    for s in schemas:
        assert s["type"] == "function"
        fn = s["function"]
        assert {"name", "description", "parameters"} <= set(fn.keys())


async def test_execute_unknown_tool_returns_error_json(sample_dataset):
    result = await ToolRegistry.execute("does_not_exist", {}, sample_dataset)
    assert json.loads(result) == {"error": "Unknown tool: does_not_exist"}


async def test_execute_tool_exception_returns_error_str(sample_dataset, monkeypatch):
    # 临时注入一个抛异常的工具（monkeypatch 在 teardown 恢复，不污染全局注册表）
    async def boom(dataset):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(
        ToolRegistry._tools,
        "_test_boom",
        ToolDefinition(name="_test_boom", description="x", parameters={}, func=boom, required=[]),
    )
    result = await ToolRegistry.execute("_test_boom", {}, sample_dataset)
    assert json.loads(result) == {"error": "kaboom"}


async def test_execute_success_returns_json(sample_dataset):
    result = await ToolRegistry.execute("get_summary", {}, sample_dataset)
    data = json.loads(result)
    assert data["totalLines"] == 6
    assert data["errorCount"] == 2
