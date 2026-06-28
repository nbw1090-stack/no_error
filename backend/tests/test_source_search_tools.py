"""源码检索工具测试 —— search_symbols / list_source_files / get_file_source。

全程零网络（clone 打桩）。流程同 test_source_tools.py：
stub clone 写假 Lua 源码 → /api/ast/analyze 落盘 + 留快照 →
构造 LogDataset(user_id) → ToolRegistry.execute(...) → 断言结果。
"""

import json
import os

import pytest

# tree-sitter 是可选依赖；缺失时整组跳过（analyzer 无法产出符号）
pytest.importorskip("tree_sitter")

from agent.dataset import LogDataset  # noqa: E402
from agent.tools.registry import ToolRegistry  # noqa: E402
from tests.conftest import parse_sse_events  # noqa: E402


# 假源码：init_card(1-3) / shutdown_card(5-7) / read_sensor_value(9-11)
_SRC = (
    "local function init_card()\n"
    '  return "card"\n'
    "end\n"
    "\n"
    "local function shutdown_card()\n"
    "  return nil\n"
    "end\n"
    "\n"
    "local function read_sensor_value()\n"
    "  return 42\n"
    "end\n"
)


@pytest.fixture
def fake_clone(monkeypatch):
    """替换 clone_component：在目标目录写一份含三个函数的 main.lua。"""
    sha = "bbbbbbb000000000000000000000000000000bbbb"

    def _fake_clone(git_url, branch, dest_dir, timeout=300):
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, "main.lua"), "w", encoding="utf-8") as fh:
            fh.write(_SRC)
        return sha

    def _fake_remote(git_url, branch, timeout=30):
        return sha

    import ast_analysis.service as svc

    monkeypatch.setattr(svc.downloader, "clone_component", _fake_clone)
    monkeypatch.setattr(svc.downloader, "remote_commit", _fake_remote)


@pytest.fixture
def fake_clone_dup(monkeypatch):
    """clone 写两个同名 init.lua（不同目录），用于验证 basename 歧义/精确优先。"""
    sha = "ccccccc000000000000000000000000000000cccc"

    def _fake_clone(git_url, branch, dest_dir, timeout=300):
        # include/ 字母序在 src/ 之前——旧实现会让它覆盖精确路径
        for rel in ("include/init.lua", "src/lib/init.lua"):
            p = os.path.join(dest_dir, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(_SRC)
        return sha

    def _fake_remote(git_url, branch, timeout=30):
        return sha

    import ast_analysis.service as svc

    monkeypatch.setattr(svc.downloader, "clone_component", _fake_clone)
    monkeypatch.setattr(svc.downloader, "remote_commit", _fake_remote)


def _analyze(authed, names):
    """发起一次 /api/ast/analyze，返回事件列表（同步 TestClient）。"""
    resp = authed["client"].post(
        "/api/ast/analyze",
        json={"component_names": names},
        headers=authed["headers"],
    )
    assert resp.status_code == 200, resp.text
    return parse_sse_events(resp)


async def _exec_tool(name, args, user_id):
    """以给定 user_id 执行工具，返回解析后的 dict。"""
    dataset = LogDataset(entries=[], summary={}, user_id=user_id)
    raw = await ToolRegistry.execute(name, args, dataset)
    return json.loads(raw)


# ============================================================
# search_symbols
# ============================================================
async def test_search_symbols_substring_match(authed, fake_clone):
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        "search_symbols", {"component": "sensor", "name": "card"},
        authed["user_id"],
    )
    # init_card + shutdown_card 都含 "card"
    names = {r["function"] for r in result["results"]}
    assert names == {"init_card", "shutdown_card"}
    assert result["matches_count"] == 2
    assert "return" in result["results"][0]["source"]


async def test_search_symbols_case_insensitive(authed, fake_clone):
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        "search_symbols", {"component": "sensor", "name": "INIT_CARD"},
        authed["user_id"],
    )
    assert result["matches_count"] == 1
    assert result["results"][0]["function"] == "init_card"


async def test_search_symbols_no_match(authed, fake_clone):
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        "search_symbols", {"component": "sensor", "name": "nonexistent"},
        authed["user_id"],
    )
    assert result["error"] == "未找到匹配的符号"
    assert "hint" in result


async def test_search_symbols_unindexed_component(authed, fake_clone):
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        "search_symbols", {"component": "hwproxy", "name": "card"},
        authed["user_id"],
    )
    assert result["error"] == "该组件尚未构建源码索引"


# ============================================================
# list_source_files
# ============================================================
async def test_list_source_files_returns_structure(authed, fake_clone):
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        "list_source_files", {"component": "sensor"}, authed["user_id"]
    )
    assert result["files_count"] == 1
    f = result["files"][0]
    assert f["rel_path"] == "main.lua"
    assert f["language"] == "lua"
    sym_names = {s["name"] for s in f["symbols"]}
    assert sym_names == {"init_card", "shutdown_card", "read_sensor_value"}


async def test_list_source_files_unindexed_component(authed, fake_clone):
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        "list_source_files", {"component": "hwproxy"}, authed["user_id"]
    )
    assert result["error"] == "该组件尚未构建源码索引"


# ============================================================
# get_file_source
# ============================================================
async def test_get_file_source_full(authed, fake_clone):
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        "get_file_source", {"component": "sensor", "file": "main.lua"},
        authed["user_id"],
    )
    assert result["rel_path"] == "main.lua"
    assert result["total_lines"] == 11
    assert result["truncated"] is False
    # 带行号渲染
    assert "init_card" in result["source"]
    assert result["source"].splitlines()[0].lstrip().startswith("1")


async def test_get_file_source_truncation(authed, fake_clone):
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        "get_file_source",
        {"component": "sensor", "file": "main.lua", "max_lines": 3},
        authed["user_id"],
    )
    assert result["truncated"] is True
    assert result["shown_lines"] == 3
    assert result["total_lines"] == 11
    assert "hint" in result


async def test_get_file_source_basename_match(authed, fake_clone):
    _analyze(authed, ["sensor"])
    # 传带前缀的路径，应退化为 basename 匹配仍命中
    result = await _exec_tool(
        "get_file_source",
        {"component": "sensor", "file": "src/lua/main.lua"},
        authed["user_id"],
    )
    assert result["rel_path"] == "main.lua"


async def test_get_file_source_offset_pagination(authed, fake_clone):
    """offset 翻页：从第 9 行起读，只返回尾部 3 行且行号真实。"""
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        "get_file_source",
        {"component": "sensor", "file": "main.lua", "offset": 9, "max_lines": 5},
        authed["user_id"],
    )
    assert result["start_line"] == 9
    assert result["end_line"] == 11
    assert result["shown_lines"] == 3
    assert result["truncated"] is False
    # 行号真实（从 9 起），且只含尾部函数
    assert result["source"].splitlines()[0].lstrip().startswith("9")
    assert "read_sensor_value" in result["source"]
    assert "init_card" not in result["source"]


async def test_get_file_source_truncation_hints_next_offset(authed, fake_clone):
    """读前 3 行应提示用 offset=4 续读。"""
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        "get_file_source",
        {"component": "sensor", "file": "main.lua", "max_lines": 3},
        authed["user_id"],
    )
    assert result["truncated"] is True
    assert (result["start_line"], result["end_line"]) == (1, 3)
    assert "offset=4" in result["hint"]


async def test_get_file_source_offset_past_eof(authed, fake_clone):
    """offset 超出文件 → 空窗口 + 提示，不报 truncated。"""
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        "get_file_source",
        {"component": "sensor", "file": "main.lua", "offset": 999},
        authed["user_id"],
    )
    assert result["shown_lines"] == 0
    assert result["truncated"] is False
    assert "超出文件范围" in result["hint"]


async def test_get_file_source_exact_path_beats_basename(authed, fake_clone_dup):
    """精确 rel_path 必须命中自身，不被字母序靠前的同名文件覆盖（本次根因修复）。"""
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        "get_file_source",
        {"component": "sensor", "file": "src/lib/init.lua"},
        authed["user_id"],
    )
    assert result["rel_path"] == "src/lib/init.lua"


async def test_get_file_source_ambiguous_basename_returns_candidates(
    authed, fake_clone_dup
):
    """只给 basename 且多个同名文件 → 返回候选列表，而非闷头返回错文件。"""
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        "get_file_source",
        {"component": "sensor", "file": "init.lua"},
        authed["user_id"],
    )
    assert result["error"].startswith("文件名不唯一")
    assert set(result["candidates"]) == {"include/init.lua", "src/lib/init.lua"}


async def test_get_file_source_not_found(authed, fake_clone):
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        "get_file_source", {"component": "sensor", "file": "missing.lua"},
        authed["user_id"],
    )
    assert result["error"] == "未找到匹配的源码文件"


async def test_get_file_source_unindexed_component(authed, fake_clone):
    _analyze(authed, ["sensor"])
    result = await _exec_tool(
        "get_file_source", {"component": "hwproxy", "file": "main.lua"},
        authed["user_id"],
    )
    assert result["error"] == "该组件尚未构建源码索引"


# ============================================================
# 未登录降级（对所有源码工具一致）
# ============================================================
async def test_source_tools_require_login(fake_clone, tmp_data):
    for tool, args in [
        ("search_symbols", {"component": "sensor", "name": "card"}),
        ("list_source_files", {"component": "sensor"}),
        ("get_file_source", {"component": "sensor", "file": "main.lua"}),
    ]:
        result = await _exec_tool(tool, args, None)
        assert result["error"] == "源码索引需登录后可用", tool


# ============================================================
# db 查询函数直接验证（不经工具层）
# ============================================================
async def test_db_find_symbols_by_name_case_insensitive(authed, fake_clone):
    from ast_analysis import db

    _analyze(authed, ["sensor"])
    hits = db.find_symbols_by_name(authed["user_id"], "sensor", "VALUE")
    assert len(hits) == 1
    assert hits[0]["name"] == "read_sensor_value"
    assert hits[0]["kind"]  # tree-sitter 给出的符号类型非空


async def test_db_list_component_files_structure(authed, fake_clone):
    from ast_analysis import db

    _analyze(authed, ["sensor"])
    files = db.list_component_files(authed["user_id"], "sensor")
    assert len(files) == 1
    assert files[0]["symbol_count"] == 3
    # symbols 已从 JSON 解析为 list[dict]
    assert isinstance(files[0]["symbols"], list)
    assert all("start_line" in s for s in files[0]["symbols"])
