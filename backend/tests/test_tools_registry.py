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
    "get_time_range",
    "get_errors_by_component",
    "get_error_digest",
    "get_context_around",
}


def test_registered_at_import():
    # 导入 agent.tools 即注册 8 个日志工具
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
    assert data["total_lines"] == 6
    assert data["errors"] == 2


async def test_execute_drops_unknown_kwargs(sample_dataset, monkeypatch):
    # LLM 臆造未知参数（如把 max_lines 写成 lines）不应触发 TypeError，
    # 而是被过滤掉、工具照常执行。
    seen = {}

    async def tool(dataset, real_param: int = 7):
        seen["real_param"] = real_param
        return {"ok": True}

    monkeypatch.setitem(
        ToolRegistry._tools,
        "_test_filter",
        ToolDefinition(
            name="_test_filter", description="x", parameters={}, func=tool,
            required=[],
        ),
    )
    result = await ToolRegistry.execute(
        "_test_filter",
        {"real_param": 5, "hallucinated": "drop me"},
        sample_dataset,
    )
    assert json.loads(result) == {"ok": True}
    assert seen["real_param"] == 5  # 已知参数保留，未知参数被丢弃


async def test_execute_var_keyword_tool_keeps_all_kwargs(sample_dataset, monkeypatch):
    # 工具声明了 **kwargs → 任意参数原样放行，不丢弃
    seen = {}

    async def tool(dataset, **kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setitem(
        ToolRegistry._tools,
        "_test_varkw",
        ToolDefinition(
            name="_test_varkw", description="x", parameters={}, func=tool,
            required=[],
        ),
    )
    await ToolRegistry.execute(
        "_test_varkw", {"anything": 1, "else_": 2}, sample_dataset
    )
    assert seen == {"anything": 1, "else_": 2}
