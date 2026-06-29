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


# ============================================================
# gather_code_context 工具测试
#
# 需要跨文件调用点，故用多文件快照：main.lua（目标 init_card + 兄弟 teardown）
# 与 caller.lua（调用 init_card）。
# ============================================================

# main.lua：init_card(1-3) / 空行(4) / teardown(5-7)
_MAIN_LUA = (
    "local function init_card()\n"   # line 1
    "  return 1\n"                    # line 2
    "end\n"                           # line 3
    "\n"                              # line 4
    "local function teardown()\n"     # line 5
    "  return 2\n"                    # line 6
    "end\n"                           # line 7
)

# caller.lua：runner 内调用 init_card（line 2）
_CALLER_LUA = (
    "local function runner()\n"   # line 1
    "  init_card()\n"             # line 2 —— 跨文件调用点
    "end\n"                       # line 3
)


@pytest.fixture
def multi_fake_clone(monkeypatch):
    """多文件 clone 打桩：写 main.lua（目标+兄弟）与 caller.lua（调用方）。"""
    state = {"sha": "bbbbbbb000000000000000000000000000000bbbb"}

    def _fake_clone(git_url, branch, dest_dir, timeout=300):
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, "main.lua"), "w", encoding="utf-8") as fh:
            fh.write(_MAIN_LUA)
        with open(os.path.join(dest_dir, "caller.lua"), "w", encoding="utf-8") as fh:
            fh.write(_CALLER_LUA)
        return state["sha"]

    def _fake_remote(git_url, branch, timeout=30):
        return state["sha"]

    import ast_analysis.service as svc

    monkeypatch.setattr(svc.downloader, "clone_component", _fake_clone)
    monkeypatch.setattr(svc.downloader, "remote_commit", _fake_remote)
    return state


async def _exec_gather(args, user_id):
    """以给定 user_id 执行 gather_code_context，返回解析后的 dict。"""
    dataset = LogDataset(entries=[], summary={}, user_id=user_id)
    raw = await ToolRegistry.execute("gather_code_context", args, dataset)
    return json.loads(raw)


async def test_gather_name_anchor(authed, multi_fake_clone):
    """name 锚点：取目标函数体 + 兄弟符号 + 跨文件调用点。"""
    _analyze(authed, ["sensor"])
    result = await _exec_gather(
        {"component": "sensor", "name": "init_card"}, authed["user_id"]
    )
    assert result["anchor"]["type"] == "name"
    assert result["target"]["function"] == "init_card"
    assert result["target"]["rel_path"] == "main.lua"
    assert result["target"]["start_line"] == 1
    assert result["target"]["end_line"] == 3
    assert "init_card" in result["target"]["source"]

    # 兄弟符号：同文件的 teardown；目标自身已排除
    sib_names = {s["name"] for s in result["sibling_symbols"]}
    assert "teardown" in sib_names
    assert "init_card" not in sib_names

    # 跨文件调用点：caller.lua 命中
    usage_paths = {u["rel_path"] for u in result["related_usages"]}
    assert "caller.lua" in usage_paths


async def test_gather_file_line_anchor(authed, multi_fake_clone):
    """file+line 锚点：按日志行号反查目标函数。"""
    _analyze(authed, ["sensor"])
    result = await _exec_gather(
        {"component": "sensor", "file": "main.lua", "line": 2},
        authed["user_id"],
    )
    assert result["anchor"]["type"] == "file_line"
    assert result["anchor"]["line"] == 2
    assert result["target"]["function"] == "init_card"
    assert result["target"]["start_line"] == 1
    assert result["target"]["end_line"] == 3


async def test_gather_context_lines(authed, multi_fake_clone):
    """context_lines 控制函数体前后取的行数。"""
    _analyze(authed, ["sensor"])
    result = await _exec_gather(
        {"component": "sensor", "name": "init_card", "context_lines": 1},
        authed["user_id"],
    )
    # init_card 1-3 + ctx 1 → 1-4
    assert result["target"]["context_start_line"] == 1
    assert result["target"]["context_end_line"] == 4


async def test_gather_max_usages_cap(authed, multi_fake_clone):
    """max_usages 限制返回的调用点数量。"""
    _analyze(authed, ["sensor"])
    result = await _exec_gather(
        {"component": "sensor", "name": "init_card", "max_usages": 1},
        authed["user_id"],
    )
    assert len(result["related_usages"]) <= 1


async def test_gather_unindexed_component(authed, multi_fake_clone):
    """组件已播种但未索引 → 友好提示去构建索引。"""
    _analyze(authed, ["sensor"])
    result = await _exec_gather(
        {"component": "hwproxy", "name": "init_card"}, authed["user_id"]
    )
    assert result["error"] == "该组件尚未构建源码索引"


async def test_gather_missing_user_id(multi_fake_clone, tmp_data):
    """user_id=None（未登录）→ 友好提示，不触达 DB。"""
    result = await _exec_gather(
        {"component": "sensor", "name": "init_card"}, None
    )
    assert result["error"] == "源码索引需登录后可用"


async def test_gather_no_anchor(authed, multi_fake_clone):
    """既无 name 也无 file+line → 缺少锚点错误。"""
    _analyze(authed, ["sensor"])
    result = await _exec_gather({"component": "sensor"}, authed["user_id"])
    assert "error" in result
    assert "锚点" in result["error"]


async def test_gather_symbol_not_found(authed, multi_fake_clone):
    """组件已索引但符号不存在 → 未找到匹配的符号。"""
    _analyze(authed, ["sensor"])
    result = await _exec_gather(
        {"component": "sensor", "name": "nope_nope"}, authed["user_id"]
    )
    assert result["error"] == "未找到匹配的符号"


# ============================================================
# trace_call_chain 工具测试
#
# 消费侧链:get_device_name → (mdb.try_get_object 'bmc.dev.PCIeDevice') + read_port。
# read_port 在同组件可解析(下一跳);try_get_object 不可解析但命中接口(保留)。
# provider 侧 pcie_device 用 register('bmc.dev.PCIeDevice') 提供接口。
# ============================================================

# card_mgmt.lua（sensor，消费侧）
_CHAIN_CONSUMER = (
    "local function get_device_name()\n"                              # 1
    "  local obj = mdb.try_get_object('bmc.dev.PCIeDevice', path)\n"  # 2
    "  return read_port(obj)\n"                                       # 3
    "end\n"                                                           # 4
    "\n"                                                              # 5
    "local function read_port(obj)\n"                                 # 6
    "  return obj.port\n"                                             # 7
    "end\n"                                                           # 8
)

# service.lua（pcie_device，提供侧）
_CHAIN_PROVIDER = (
    "local function setup()\n"                          # 1
    "  register('bmc.dev.PCIeDevice', handler)\n"       # 2 —— provide 信号 register
    "end\n"                                             # 3
)


@pytest.fixture
def chain_fake_clone(monkeypatch):
    """按组件名(dest_dir basename)写不同内容:sensor=消费侧链,pcie_device=提供侧。"""
    state = {"sha": "ccccccc000000000000000000000000000000cccc"}

    def _fake_clone(git_url, branch, dest_dir, timeout=300):
        comp = os.path.basename(dest_dir.rstrip("/"))
        os.makedirs(dest_dir, exist_ok=True)
        if comp == "pcie_device":
            with open(os.path.join(dest_dir, "service.lua"), "w", encoding="utf-8") as fh:
                fh.write(_CHAIN_PROVIDER)
        else:
            with open(os.path.join(dest_dir, "card_mgmt.lua"), "w", encoding="utf-8") as fh:
                fh.write(_CHAIN_CONSUMER)
        return state["sha"]

    def _fake_remote(git_url, branch, timeout=30):
        return state["sha"]

    import ast_analysis.service as svc

    monkeypatch.setattr(svc.downloader, "clone_component", _fake_clone)
    monkeypatch.setattr(svc.downloader, "remote_commit", _fake_remote)
    return state


async def _exec_trace(args, user_id):
    dataset = LogDataset(entries=[], summary={}, user_id=user_id)
    raw = await ToolRegistry.execute("trace_call_chain", args, dataset)
    return raw, json.loads(raw)


async def test_trace_skeleton_no_body(authed, chain_fake_clone):
    """骨架含调用链(名字+file:line+调用行)且不含被调函数体。"""
    _analyze(authed, ["sensor"])
    raw, result = await _exec_trace(
        {"component": "sensor", "file": "card_mgmt.lua", "line": 2},
        authed["user_id"],
    )
    assert result["anchor"]["function"] == "get_device_name"

    # 接口被识别
    assert "bmc.dev.PCIeDevice" in result["interfaces_seen"]

    # 收集所有 call 项
    calls = [c for hop in result["hops"] for c in hop["calls"]]
    callees = {c["callee"] for c in calls}
    assert "try_get_object" in callees   # 接口命中,保留(resolved=null)
    assert "read_port" in callees         # 同组件可解析(下一跳锚点)

    # try_get_object 带接口、resolved 为 null
    tgo = next(c for c in calls if c["callee"] == "try_get_object")
    assert tgo.get("interface") == "bmc.dev.PCIeDevice"
    assert tgo["resolved"] is None

    # read_port 解析到定义(第 6 行)
    rp = next(c for c in calls if c["callee"] == "read_port")
    assert rp["resolved"]["rel_path"] == "card_mgmt.lua"
    assert rp["resolved"]["start_line"] == 6

    # 关键:不含被调函数体——read_port 的体 `return obj.port` 不应出现在骨架里
    assert "obj.port" not in raw


async def test_trace_depth_cap(authed, chain_fake_clone):
    """max_depth=1 时只展开锚点本身,不下钻 read_port。"""
    _analyze(authed, ["sensor"])
    _, result = await _exec_trace(
        {"component": "sensor", "name": "get_device_name", "max_depth": 1},
        authed["user_id"],
    )
    # 仅锚点这一跳被展开
    expanded = {hop["from"]["function"] for hop in result["hops"]}
    assert expanded == {"get_device_name"}


async def test_trace_breadth_cap(authed, chain_fake_clone):
    """max_breadth=1 时每跳最多 1 个 call,且标记 truncated。"""
    _analyze(authed, ["sensor"])
    _, result = await _exec_trace(
        {"component": "sensor", "name": "get_device_name", "max_breadth": 1},
        authed["user_id"],
    )
    anchor_hop = next(
        h for h in result["hops"] if h["from"]["function"] == "get_device_name"
    )
    assert len(anchor_hop["calls"]) <= 1
    assert anchor_hop["truncated_calls"] is True


async def test_trace_missing_anchor(authed, chain_fake_clone):
    """既无 name 也无 file+line → 缺少锚点错误。"""
    _analyze(authed, ["sensor"])
    _, result = await _exec_trace({"component": "sensor"}, authed["user_id"])
    assert "error" in result and "锚点" in result["error"]


async def test_trace_missing_user_id(chain_fake_clone, tmp_data):
    """user_id=None → 友好提示。"""
    _, result = await _exec_trace(
        {"component": "sensor", "name": "get_device_name"}, None
    )
    assert result["error"] == "源码索引需登录后可用"


# ============================================================
# resolve_interface 工具测试
# ============================================================

async def _exec_resolve(args, user_id):
    dataset = LogDataset(entries=[], summary={}, user_id=user_id)
    raw = await ToolRegistry.execute("resolve_interface", args, dataset)
    return json.loads(raw)


async def test_resolve_interface_provider_and_consumer(authed, chain_fake_clone):
    """两组件都索引:pcie_device 提供 / sensor 消费。"""
    _analyze(authed, ["sensor", "pcie_device"])
    result = await _exec_resolve(
        {"name": "bmc.dev.PCIeDevice"}, authed["user_id"]
    )
    assert result["provider_indexed"] is True
    prov_comps = {p["component"] for p in result["providers"]}
    cons_comps = {c["component"] for c in result["consumers"]}
    assert "pcie_device" in prov_comps
    assert "sensor" in cons_comps
    # provider 命中行的 signal 应识别为 provide
    assert any(p["signal"] == "provide" for p in result["providers"])


async def test_resolve_interface_wiki_fallback(authed, chain_fake_clone, monkeypatch):
    """只索引消费侧:无已索引 provider → provider_indexed=False + wiki 兜底。"""
    _analyze(authed, ["sensor"])  # 不分析 pcie_device

    import wiki.store as wiki_store

    monkeypatch.setattr(wiki_store, "is_indexed", lambda: True)
    monkeypatch.setattr(
        wiki_store,
        "search",
        lambda q, top_k=5: [
            {
                "slug": "mdb-interfaces",
                "title": "mdb 接口",
                "description": "",
                "snippet": "bmc.dev.PCIeDevice 由 pcie_device 提供",
                "score": 9,
            }
        ],
    )

    result = await _exec_resolve(
        {"name": "bmc.dev.PCIeDevice"}, authed["user_id"]
    )
    assert result["provider_indexed"] is False
    assert result["wiki_hint"]["slug"] == "mdb-interfaces"


async def test_resolve_interface_missing_user_id(chain_fake_clone, tmp_data):
    """user_id=None → 友好提示。"""
    result = await _exec_resolve({"name": "bmc.dev.PCIeDevice"}, None)
    assert result["error"] == "源码索引需登录后可用"


# ============================================================
# summarize_component 工具测试
#
# 多目录快照:src/lualib/main.lua(入口符号)+ gen/big.lua(噪声),
# 验证 src/ 优先过滤与入口启发式选择。
# ============================================================

# src/lualib/main.lua:init_card(命中 init_*)/ teardown(不命中)/ helper(不命中)
_SUMMARY_MAIN = (
    "local function init_card()\n"   # line 1
    "  return 1\n"                    # line 2
    "end\n"                           # line 3
    "\n"                              # line 4
    "local function teardown()\n"     # line 5
    "  return 2\n"                    # line 6
    "end\n"                           # line 7
    "\n"                              # line 8
    "local function helper()\n"       # line 9
    "  return 3\n"                    # line 10
    "end\n"                           # line 11
)
# gen/big.lua:generated(噪声,被 src/ 过滤排除出入口候选)
_SUMMARY_GEN = "local function generated()\n  return 0\nend\n"


@pytest.fixture
def summary_fake_clone(monkeypatch):
    """写 src/lualib/main.lua(入口符号)+ gen/big.lua(噪声),验证 src/ 过滤。"""
    sha = "ccccccc000000000000000000000000000000cccc"

    def _fake_clone(git_url, branch, dest_dir, timeout=300):
        os.makedirs(os.path.join(dest_dir, "src", "lualib"), exist_ok=True)
        os.makedirs(os.path.join(dest_dir, "gen"), exist_ok=True)
        with open(
            os.path.join(dest_dir, "src", "lualib", "main.lua"), "w", encoding="utf-8"
        ) as fh:
            fh.write(_SUMMARY_MAIN)
        with open(
            os.path.join(dest_dir, "gen", "big.lua"), "w", encoding="utf-8"
        ) as fh:
            fh.write(_SUMMARY_GEN)
        return sha

    def _fake_remote(git_url, branch, timeout=30):
        return sha

    import ast_analysis.service as svc

    monkeypatch.setattr(svc.downloader, "clone_component", _fake_clone)
    monkeypatch.setattr(svc.downloader, "remote_commit", _fake_remote)
    return sha


async def _exec_summarize(args, user_id):
    """以给定 user_id 执行 summarize_component,返回解析后的 dict。"""
    dataset = LogDataset(entries=[], summary={}, user_id=user_id)
    raw = await ToolRegistry.execute("summarize_component", args, dataset)
    return json.loads(raw)


async def test_summarize_bounded_overview(authed, summary_fake_clone):
    """概览:总数 + 目录分组 + 入口符号(仅 src/)+ 大文件样本。"""
    _analyze(authed, ["sensor"])
    result = await _exec_summarize({"component": "sensor"}, authed["user_id"])
    assert result["component"] == "sensor"
    assert result["totals"]["file_count"] == 2          # main.lua + big.lua
    assert result["totals"]["symbol_count"] == 4         # 3 (main) + 1 (gen)
    # 目录分组含 src 与 gen
    assert result["by_top_directory"].get("src") == 1
    assert result["by_top_directory"].get("gen") == 1
    # 入口符号只来自 src/,且只命中 init_card
    entry_names = {e["name"] for e in result["entry_symbols"]}
    assert "init_card" in entry_names
    assert "teardown" not in entry_names
    assert "helper" not in entry_names
    assert "generated" not in entry_names                # gen/ 被排除
    # 大文件样本来自 src/
    assert all(
        f["rel_path"].startswith("src/")
        for f in result["largest_source_files"]
    )


async def test_summarize_max_entry_symbols_cap(authed, summary_fake_clone):
    """max_entry_symbols 限制入口符号数量。"""
    _analyze(authed, ["sensor"])
    result = await _exec_summarize(
        {"component": "sensor", "max_entry_symbols": 0}, authed["user_id"]
    )  # max(1, 0) = 1
    assert len(result["entry_symbols"]) <= 1


async def test_summarize_unindexed_component(authed, summary_fake_clone):
    """组件已播种但未索引 → 友好提示去构建索引。"""
    _analyze(authed, ["sensor"])
    result = await _exec_summarize({"component": "hwproxy"}, authed["user_id"])
    assert result["error"] == "该组件尚未构建源码索引"


async def test_summarize_missing_user_id(summary_fake_clone, tmp_data):
    """user_id=None(未登录)→ 友好提示,不触达 DB。"""
    result = await _exec_summarize({"component": "sensor"}, None)
    assert result["error"] == "源码索引需登录后可用"


# ============================================================
# 工具分组(group)验证 —— log 工具与 source 工具可分别过滤(供 core 按需暴露)
# ============================================================

def test_tool_grouping_separates_log_and_source():
    """源码工具标 group=source,日志工具默认 log,wiki 工具 group=wiki,三组互不重叠。"""
    source_schemas = ToolRegistry.get_schemas(groups={"source"})
    source_names = {s["function"]["name"] for s in source_schemas}
    log_names = set(ToolRegistry.get_names(groups={"log"}))
    wiki_names = set(ToolRegistry.get_names(groups={"wiki"}))
    all_names = set(ToolRegistry.get_names())

    # 8 个源码工具（含多跳导航 trace_call_chain / 跨组件 resolve_interface）
    assert source_names == {
        "get_function_source",
        "search_symbols",
        "list_source_files",
        "get_file_source",
        "gather_code_context",
        "summarize_component",
        "trace_call_chain",
        "resolve_interface",
    }
    # 3 个 wiki 工具（页导航 + 检索兜底）
    assert wiki_names == {"get_wiki_index", "read_wiki_page", "search_wiki"}
    # 三组两两不重叠,并集 = 全部;groups=None 返回全部
    assert source_names.isdisjoint(log_names)
    assert wiki_names.isdisjoint(source_names | log_names)
    assert source_names | log_names | wiki_names == all_names
    assert len(ToolRegistry.get_schemas(groups=None)) == len(all_names)
