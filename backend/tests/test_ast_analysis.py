"""
AST 分析模块测试

两部分：
1) analyzer 单元测试（tree-sitter 缺失时整组 importorskip 跳过）：
   解析内联的 Lua / C 片段，断言能识别语言、提取符号、不崩。
2) 端点 + 增量算法测试（git 完全打桩，零网络）：
   - clone_component 用 monkeypatch 替换为「写一份临时源码树 + 返回假 sha」
   - remote_commit 用 monkeypatch 返回受控 sha，验证 added/unchanged/updated/removed 分支
   - 通过 /api/ast/analyze（SSE）+ /api/ast/result 驱动整条增量流程
"""

import os

import pytest

# tree-sitter 是可选依赖；缺失时跳过 analyzer 单元测试
ts = pytest.importorskip("tree_sitter")

from ast_analysis import analyzer  # noqa: E402


# ============================================================
# 1) analyzer 单元测试
# ============================================================
def test_analyze_lua_snippet(tmp_path):
    f = tmp_path / "mod.lua"
    f.write_text("local function foo()\n  return 1\nend\n", encoding="utf-8")
    result = analyzer.analyze_file(str(f), "lua")
    assert result["language"] == "lua"
    assert result["symbol_count"] >= 1
    kinds = {s["kind"] for s in result["symbols"]}
    assert "function_declaration" in kinds
    # 名字应当被提取出来
    names = {s["name"] for s in result["symbols"]}
    assert "foo" in names
    # node_types 非空
    assert result["node_types"]


def test_analyze_c_snippet(tmp_path):
    f = tmp_path / "add.c"
    f.write_text("int add(int a, int b){return a+b;}\n", encoding="utf-8")
    result = analyzer.analyze_file(str(f), "c")
    assert result["language"] == "c"
    assert result["symbol_count"] >= 1
    kinds = {s["kind"] for s in result["symbols"]}
    assert "function_definition" in kinds
    names = {s["name"] for s in result["symbols"]}
    assert "add" in names


def test_analyze_directory_skips_hidden_and_git(tmp_path):
    """目录遍历应跳过 .git、隐藏目录、非源码文件。"""
    # 一个正常的 lua 文件
    (tmp_path / "ok.lua").write_text(
        "local function bar()\nend\n", encoding="utf-8"
    )
    # .git 目录下的 lua 应被忽略
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    (gitdir / "ignored.lua").write_text(
        "local function ghost()\nend\n", encoding="utf-8"
    )
    # 非源码文件应被忽略
    (tmp_path / "readme.md").write_text("hi", encoding="utf-8")

    result = analyzer.analyze_directory(str(tmp_path), "testcomp")
    paths = [f["rel_path"] for f in result["files"]]
    assert "ok.lua" in paths
    assert all(".git" not in p for p in paths)
    assert all(not p.endswith(".md") for p in paths)
    assert result["stats"]["files"] == 1


# ============================================================
# 2) 端点 + 增量算法（git 打桩）
# ============================================================
from tests.conftest import parse_sse_events  # noqa: E402


@pytest.fixture
def stub_clone(monkeypatch, tmp_path):
    """
    把 ast_analysis.downloader.clone_component 替换为：
      - 在目标目录写一份样例 .lua + .c 源码
      - 返回受控 sha（可通过 set_sha 更新）

    service.py 经 `ast_analysis.downloader.clone_component` 引用，
    因此 patch 该路径即可。
    """
    state = {"sha": "aaaaaaa000000000000000000000000000000aaaa"}

    def _fake_clone(git_url, branch, dest_dir, timeout=300):
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, "main.lua"), "w", encoding="utf-8") as fh:
            fh.write("local function init()\n  return 0\nend\n")
        with open(os.path.join(dest_dir, "core.c"), "w", encoding="utf-8") as fh:
            fh.write("int start(void){return 0;}\n")
        return state["sha"]

    def _fake_remote(git_url, branch, timeout=30):
        return state["sha"]

    import ast_analysis.service as svc

    monkeypatch.setattr(svc.downloader, "clone_component", _fake_clone)
    monkeypatch.setattr(svc.downloader, "remote_commit", _fake_remote)
    return state


def _analyze(authed, names):
    """发起一次 /api/ast/analyze，返回事件列表。"""
    resp = authed["client"].post(
        "/api/ast/analyze",
        json={"component_names": names},
        headers=authed["headers"],
    )
    assert resp.status_code == 200, resp.text
    return parse_sse_events(resp)


def _result(authed):
    return authed["client"].get(
        "/api/ast/result", headers=authed["headers"]
    )


def _component_events(events, action=None):
    return [
        e for e in events
        if e.get("type") == "component_result"
        and (action is None or e.get("action") == action)
    ]


def test_analyze_added_then_persisted(authed, stub_clone):
    events = _analyze(authed, ["sensor"])
    # 应有 plan + 至少一个 progress + added 的 component_result + summary + done
    types = [e["type"] for e in events]
    assert "plan" in types
    assert "summary" in types
    assert types[-1] == "done"

    added = _component_events(events, "added")
    assert any(e["component"] == "sensor" for e in added)
    sensor_ev = next(e for e in added if e["component"] == "sensor")
    assert sensor_ev["error"] is None
    assert sensor_ev["commit"] == stub_clone["sha"][:8]
    assert sensor_ev["files"] >= 1
    assert sensor_ev["symbols"] >= 1

    # summary 计数
    summary = next(e for e in events if e["type"] == "summary")
    assert summary["stats"]["added"] == 1
    assert summary["stats"]["components_total"] == 1

    # GET /result 命中
    r = _result(authed)
    assert r.status_code == 200
    data = r.json()
    comps = {c["component"] for c in data["components"]}
    assert "sensor" in comps
    assert data["stats"]["components"] == 1
    assert data["stats"]["files"] >= 1

    # 分析成功后，注册表里该组件 enabled=True（已分析）
    assert _component_enabled(authed, "sensor") is True


def test_analyze_unchanged_when_commit_matches(authed, stub_clone):
    # 第一次：added
    _analyze(authed, ["sensor"])
    # 第二次：remote_commit 返回相同 sha → unchanged，且 result 仍在
    events = _analyze(authed, ["sensor"])
    unchanged = _component_events(events, "unchanged")
    assert any(e["component"] == "sensor" for e in unchanged)
    # 不应再有 added/updated
    assert not _component_events(events, "added")
    assert not _component_events(events, "updated")

    summary = next(e for e in events if e["type"] == "summary")
    assert summary["stats"]["unchanged"] == 1
    assert summary["stats"]["added"] == 0

    # 数据保留
    assert _result(authed).json()["stats"]["components"] == 1


def test_analyze_updated_when_commit_differs(authed, stub_clone):
    _analyze(authed, ["sensor"])
    # 改 sha 再跑 → updated
    stub_clone["sha"] = "bbbbbbb1111111111111111111111111111bbbb"
    events = _analyze(authed, ["sensor"])
    updated = _component_events(events, "updated")
    assert any(e["component"] == "sensor" for e in updated)
    sensor_ev = next(e for e in updated if e["component"] == "sensor")
    assert sensor_ev["commit"] == stub_clone["sha"][:8]
    summary = next(e for e in events if e["type"] == "summary")
    assert summary["stats"]["updated"] == 1


def test_analyze_remove_and_add_swaps_component(authed, stub_clone):
    # 先有 sensor
    _analyze(authed, ["sensor"])
    assert "sensor" in {
        c["component"] for c in _result(authed).json()["components"]
    }
    # 换成另一个注册表里的组件（hwproxy 是种子组件）
    events = _analyze(authed, ["hwproxy"])
    types = [e["type"] for e in events]
    assert "plan" in types
    plan = next(e for e in events if e["type"] == "plan")
    assert "sensor" in plan["removed"]
    assert "hwproxy" in plan["added"]

    added = _component_events(events, "added")
    assert any(e["component"] == "hwproxy" for e in added)

    # result 中 sensor 应已消失，hwproxy 出现
    data = _result(authed).json()
    comps = {c["component"] for c in data["components"]}
    assert "sensor" not in comps
    assert "hwproxy" in comps

    # sensor 被移除 → enabled=False；hwproxy 新增分析 → enabled=True
    assert _component_enabled(authed, "sensor") is False
    assert _component_enabled(authed, "hwproxy") is True


def test_analyze_invalid_component_reports_error(authed, stub_clone):
    events = _analyze(authed, ["definitely_not_a_component"])
    # 注册表里没有 → 整体 error 事件
    assert any(e["type"] == "error" for e in events)
    # 既不应 wipe（本就空），也不应有 done 之外的破坏性动作
    assert _result(authed).status_code == 404


def test_analyze_empty_list_returns_400(authed, stub_clone):
    resp = authed["client"].post(
        "/api/ast/analyze",
        json={"component_names": []},
        headers=authed["headers"],
    )
    assert resp.status_code == 400


def test_analyze_requires_auth(client):
    resp = client.post(
        "/api/ast/analyze", json={"component_names": ["sensor"]}
    )
    assert resp.status_code == 401


def test_result_requires_auth(client):
    resp = client.get("/api/ast/result")
    assert resp.status_code == 401


def test_result_empty_returns_404(authed):
    assert _result(authed).status_code == 404


# ============================================================
# 3) 删除单组件 AST 分析：DELETE /api/ast/components/{name}
# ============================================================
def _component_enabled(authed, name):
    """从注册表列表里取某组件当前的 enabled（= 是否已分析）。"""
    comps = authed["client"].get(
        "/api/components", headers=authed["headers"]
    ).json()["components"]
    return next((c["enabled"] for c in comps if c["name"] == name), None)


def test_delete_component_analysis_clears_and_disables(authed, stub_clone):
    client = authed["client"]
    # 种子组件初始 enabled=False（尚未分析）
    assert _component_enabled(authed, "sensor") is False
    # 先分析 sensor，使其有 AST 结果 → enabled 翻为 True
    _analyze(authed, ["sensor"])
    assert _result(authed).status_code == 200
    assert "sensor" in {
        c["component"] for c in _result(authed).json()["components"]
    }
    assert _component_enabled(authed, "sensor") is True

    # 删除 sensor 的 AST 分析
    resp = client.delete(
        "/api/ast/components/sensor", headers=authed["headers"]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] is True
    # 组件仍在注册表里，但被标记为不可用
    assert body["component"]["name"] == "sensor"
    assert body["component"]["enabled"] is False
    assert _component_enabled(authed, "sensor") is False

    # AST 结果中 sensor 已消失（本就是唯一组件 → 整体 404）
    assert _result(authed).status_code == 404


def test_delete_component_analysis_idempotent_when_no_result(authed):
    """该用户本就没有该组件的分析结果时，删除应幂等成功，仅标记不可用。"""
    client = authed["client"]
    resp = client.delete(
        "/api/ast/components/sensor", headers=authed["headers"]
    )
    assert resp.status_code == 200
    assert resp.json()["component"]["enabled"] is False


def test_delete_component_analysis_unknown_returns_404(authed):
    resp = authed["client"].delete(
        "/api/ast/components/does_not_exist", headers=authed["headers"]
    )
    assert resp.status_code == 404


def test_delete_component_analysis_requires_auth(client):
    resp = client.delete("/api/ast/components/sensor")
    assert resp.status_code == 401
