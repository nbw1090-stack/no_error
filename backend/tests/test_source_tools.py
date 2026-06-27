"""get_function_source 工具测试 —— 全程零网络（git 打桩）。

流程：stub clone 写一份含两个函数的假 Lua 源码 → /api/ast/analyze 落盘 + 留快照 →
构造 LogDataset(user_id) → ToolRegistry.execute("get_function_source") → 断言切出的函数体。
"""

import json
import os

import pytest

# tree-sitter 是可选依赖；缺失时整组跳过（analyzer 无法产出符号）
ts = pytest.importorskip("tree_sitter")

from agent.dataset import LogDataset  # noqa: E402
from agent.tools.registry import ToolRegistry  # noqa: E402
from tests.conftest import parse_sse_events  # noqa: E402


# 假源码：alpha(1-3) / 空行(4) / beta(5-7)
_LUA_SOURCE = (
    "local function alpha()\n"
    "  return 1\n"
    "end\n"
    "\n"
    "local function beta()\n"
    "  return 2\n"
    "end\n"
)


@pytest.fixture
def fake_clone(monkeypatch):
    """替换 clone_component：在目标目录写一份含两个函数的 main.lua。"""
    state = {"sha": "aaaaaaa000000000000000000000000000000aaaa"}

    def _fake_clone(git_url, branch, dest_dir, timeout=300):
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, "main.lua"), "w", encoding="utf-8") as fh:
            fh.write(_LUA_SOURCE)
        return state["sha"]

    def _fake_remote(git_url, branch, timeout=30):
        return state["sha"]

    import ast_analysis.service as svc

    monkeypatch.setattr(svc.downloader, "clone_component", _fake_clone)
    monkeypatch.setattr(svc.downloader, "remote_commit", _fake_remote)
    return state


def _analyze(authed, names):
    """发起一次 /api/ast/analyze，返回事件列表（同步 TestClient）。"""
    resp = authed["client"].post(
        "/api/ast/analyze",
        json={"component_names": names},
        headers=authed["headers"],
    )
    assert resp.status_code == 200, resp.text
    return parse_sse_events(resp)


async def _exec_tool(args, user_id):
    """以给定 user_id 执行 get_function_source，返回解析后的 dict。"""
    dataset = LogDataset(entries=[], summary={}, user_id=user_id)
    raw = await ToolRegistry.execute("get_function_source", args, dataset)
    return json.loads(raw)


async def test_get_function_source_returns_body(authed, fake_clone):
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        {"component": "sensor", "file": "main.lua", "line": 2},
        authed["user_id"],
    )
    assert result["function"] == "alpha"
    assert result["start_line"] == 1
    assert result["end_line"] == 3
    assert "alpha" in result["source"]
    assert "return 1" in result["source"]


async def test_get_function_source_second_function(authed, fake_clone):
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        {"component": "sensor", "file": "main.lua", "line": 6},
        authed["user_id"],
    )
    assert result["function"] == "beta"
    assert result["start_line"] == 5
    assert result["end_line"] == 7


async def test_get_function_source_context_lines(authed, fake_clone):
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        {"component": "sensor", "file": "main.lua", "line": 2, "context_lines": 1},
        authed["user_id"],
    )
    # alpha=1-3，context 1 → 切到第 1-4 行（含 beta 前的空行）
    assert result["context_start_line"] == 1
    assert result["context_end_line"] == 4


async def test_get_function_source_line_outside_function(authed, fake_clone):
    _analyze(authed, ["sensor"])
    # 第 4 行是函数间空行，不在任何函数区间
    result = await _exec_tool(
        {"component": "sensor", "file": "main.lua", "line": 4},
        authed["user_id"],
    )
    assert "error" in result


async def test_get_function_source_unindexed_component(authed, fake_clone):
    # 只分析了 sensor，查 hwproxy（种子组件但未索引）
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        {"component": "hwproxy", "file": "main.lua", "line": 2},
        authed["user_id"],
    )
    assert result["error"] == "该组件尚未构建源码索引"


async def test_get_function_source_missing_user_id(fake_clone, tmp_data):
    # user_id=None（未登录）→ 友好提示，不触达 DB
    result = await _exec_tool(
        {"component": "sensor", "file": "main.lua", "line": 2}, None
    )
    assert result["error"] == "源码索引需登录后可用"


async def test_source_snapshot_retained_after_analyze(authed, fake_clone, tmp_data):
    """验证 clone 后源码快照被保留（不再被 rmtree），且 .git 被清理。"""
    _analyze(authed, ["sensor"])
    snap = os.path.join(tmp_data["source_dir"], str(authed["user_id"]), "sensor")
    assert os.path.isfile(os.path.join(snap, "main.lua"))
    assert not os.path.isdir(os.path.join(snap, ".git"))
