"""
LLM Wiki 测试（全离线，不触网、不真调 LLM）

覆盖：
- store：slugify / 读写 page·index·manifest / is_indexed / get_status / search / delete
- compile：plan_sources（精选范围）/ _extract_title_desc / compile_page（假 LLM）/
  interlink / render_index
- service.sync_stream：monkeypatch 掉 git clone（本地造文档树）+ 假 LLM，跑完整编译流水线
- wiki_tools：未编译→提示；已编译→get_wiki_index / read_wiki_page / search_wiki
- routes：/status（鉴权 + 空）/ /sync（SSE 编译）/ /index / /page / /search

LLM 用本地 DistillLLM 假实现（提炼成结构化页 / 写架构总览），git clone 用 monkeypatch
替换为在 dest 里造一棵最小 docs/zh/development 树。绝不碰真实 backend/data/wiki。
"""

import re

import pytest

from wiki import store
from wiki import compile as wc
from wiki import service
from wiki import routes as wiki_routes
from agent.dataset import LogDataset
from agent.tools import wiki_tools


# ============================================================
# 假 LLM：把源文档「提炼」成结构化页；索引调用则写架构总览
# ============================================================
class DistillLLM:
    model = "fake-distill"

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools=None):
        self.calls += 1
        sys = messages[0]["content"]
        user = messages[-1]["content"]
        if "架构总览" in sys:  # build_index_overview
            return {"content": "openUBMC 采用微组件架构，组件通过 mdb 协作。"}
        m = re.search(r"标题（参考）：(.+)", user)
        t = m.group(1).strip() if m else "页"
        return {
            "content": (
                f"# {t}\n> {t} 的提炼简介\n\n"
                f"## 概述\n{t} 涉及 mdb 接口与组件模型。\n\n"
                f"## 关键设计 / 接口\n- 要点A\n- 要点B\n"
            )
        }


def _write_fake_docs(dest: str) -> None:
    """在 dest 下造一棵最小 docs/zh/development 树（含精选与非精选目录）。"""
    import os

    base = os.path.join(dest, "docs", "zh", "development")
    # 每篇正文需 ≥200 字节（compile._MIN_SOURCE_BYTES），故重复铺一段。
    pad = "openUBMC 采用微组件架构，组件之间通过 mdb 接口协作开发，统一模型描述。" * 4
    files = {
        "design_reference/architecture.md": f"# openUBMC架构简介\n{pad}\n",
        "design_reference/key_feature/model_rules.md": f"# 模型规范定义\n组件模型规范，描述对象与属性。{pad}\n",
        "api/app_api/sensor.md": f"# 功能简介\n传感器 sensor 通过 D-Bus 暴露门限与离散配置。{pad}\n",
        "quick_start/intro.md": f"# 快速上手\n如何起一个 openUBMC 组件。{pad}\n",
        "glossary.md": f"# 术语表\nmdb、D-Bus、Redfish 等术语解释。{pad}\n",
        # 非精选：specifications 应被跳过
        "specifications/auto_dict.md": f"# 自动字典\n自动生成的低价值内容。{pad}\n",
    }
    for rel, content in files.items():
        p = os.path.join(base, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)


# ============================================================
# fixtures
# ============================================================
@pytest.fixture
def wiki_tmp(tmp_path):
    """把全局 wiki 目录重定向到 tmp_path 并初始化。"""
    d = str(tmp_path / "wiki")
    store.set_wiki_dir(d)
    store.init_wiki_dir()
    yield d
    store.set_wiki_dir(str(tmp_path / "nonexistent_wiki"))


@pytest.fixture
def fake_clone(monkeypatch):
    """把 git clone 换成本地造文档树，避免触网。"""
    def _clone(git_url, branch, dest, timeout=600):
        _write_fake_docs(dest)
        return "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    monkeypatch.setattr(service, "_clone_docs", _clone)
    return _clone


async def _run_sync(llm, **kw):
    """跑 service.sync_stream，收集事件。"""
    events = []
    async for ev in service.sync_stream(llm, **kw):
        events.append(ev)
    return events


# ============================================================
# store
# ============================================================
def test_slugify():
    # 非字母数字（含下划线）折叠为 '-'
    assert (
        store.slugify("docs/zh/development/design_reference/architecture.md")
        == "design-reference-architecture"
    )
    # ascii 化后为空（整段纯中文）→ 回退短哈希
    s = store.slugify("docs/zh/development/中文目录/中文文件.md")
    assert s.startswith("doc-")


def test_store_write_read_status(wiki_tmp):
    assert store.is_indexed() is False
    assert store.get_status()["indexed"] is False

    store.write_page("p1", "# 页一\n> 简介一\n正文一 mdb")
    store.write_index("# openUBMC 知识库\n总览…")
    store.write_manifest(
        {
            "git_url": "u", "branch": "", "commit": "c0ffee00", "model": "m",
            "compiled_at": "2026-06-29T00:00:00Z", "source_count": 1,
            "pages": [
                {"slug": "p1", "title": "页一", "description": "简介一",
                 "section": "design_reference", "sources": ["a.md"],
                 "source_hash": "h1"}
            ],
        }
    )
    assert store.is_indexed() is True
    st = store.get_status()
    assert st["indexed"] and st["meta"]["page_count"] == 1
    assert st["meta"]["model"] == "m"
    assert st["pages"][0]["slug"] == "p1"

    pg = store.read_page("p1")
    assert pg["title"] == "页一" and "正文一" in pg["content"]
    assert store.read_page("nope") is None


def test_store_search(wiki_tmp):
    store.write_page("a", "# 传感器\n传感器通过 D-Bus 暴露 mdb 接口")
    store.write_page("b", "# 电源\n电源管理与升级")
    store.write_manifest(
        {
            "git_url": "u", "branch": "", "commit": "x", "model": "m",
            "compiled_at": "t1", "source_count": 2,
            "pages": [
                {"slug": "a", "title": "传感器", "description": "传感器页",
                 "section": "api", "sources": [], "source_hash": "1"},
                {"slug": "b", "title": "电源", "description": "电源页",
                 "section": "api", "sources": [], "source_hash": "2"},
            ],
        }
    )
    res = store.search("mdb 接口", top_k=3)
    assert res and res[0]["slug"] == "a"
    # 2 字中文词也能召回
    assert store.search("传感", top_k=3)
    assert store.search("", top_k=3) == []


def test_store_search_cache_invalidated(wiki_tmp):
    store.write_page("a", "# 旧\n旧主题 架构")
    store.write_manifest({"compiled_at": "t1", "pages": [
        {"slug": "a", "title": "旧", "description": "", "section": "s",
         "sources": [], "source_hash": "1"}]})
    assert store.search("架构", top_k=3)
    store.delete_page("a")
    store.write_page("b", "# 新\n只讲电源")
    store.write_manifest({"compiled_at": "t2", "pages": [
        {"slug": "b", "title": "新", "description": "", "section": "s",
         "sources": [], "source_hash": "2"}]})
    assert store.search("架构", top_k=3) == []
    assert store.search("电源", top_k=3)


# ============================================================
# compile
# ============================================================
def test_plan_sources_curated_only(tmp_path):
    dest = str(tmp_path / "repo")
    _write_fake_docs(dest)
    items = wc.plan_sources(dest)
    sections = {i["section"] for i in items}
    # 精选目录在内，specifications 被排除
    assert "design_reference" in sections
    assert "api" in sections
    assert "specifications" not in sections
    rels = {i["rel_path"] for i in items}
    assert not any("specifications" in r for r in rels)
    # slug 唯一
    slugs = [i["slug"] for i in items]
    assert len(set(slugs)) == len(slugs)


def test_extract_title_desc():
    md = "# 我的标题\n> 一句话简介\n\n## 概述\n正文"
    t, d = wc._extract_title_desc(md, "回退")
    assert t == "我的标题" and d == "一句话简介"
    # 无 > 行时取首段
    t2, d2 = wc._extract_title_desc("# 标题\n\n这是首段内容", "回退")
    assert t2 == "标题" and "首段" in d2


async def test_compile_page_and_interlink(tmp_path):
    dest = str(tmp_path / "repo")
    _write_fake_docs(dest)
    items = wc.plan_sources(dest)
    llm = DistillLLM()
    pages = [await wc.compile_page(llm, it) for it in items]
    assert all(p["body"].startswith("#") for p in pages)
    assert all(p["slug"] and p["title"] for p in pages)
    # interlink 不报错且返回同样数量
    linked = wc.interlink(pages)
    assert len(linked) == len(pages)


def test_render_index_groups_sections():
    pages = [
        {"slug": "a", "title": "架构", "description": "架构页",
         "section": "design_reference"},
        {"slug": "b", "title": "接口", "description": "接口页", "section": "api"},
    ]
    md = wc.render_index("总览文字", pages)
    assert "# openUBMC 知识库" in md
    assert "总览文字" in md
    assert "(./pages/a.md)" in md and "(./pages/b.md)" in md
    assert "架构与设计" in md and "接口（API）" in md


# ============================================================
# service.sync_stream（完整编译流水线，离线）
# ============================================================
async def test_sync_stream_compiles_wiki(wiki_tmp, fake_clone):
    llm = DistillLLM()
    events = await _run_sync(llm)
    types = [e["type"] for e in events]
    assert "plan" in types and "plan_done" in types
    assert "summary" in types and types[-1] == "done"
    page_evs = [e for e in events if e["type"] == "page"]
    assert page_evs and all(e["status"] in ("compiled", "reused") for e in page_evs)
    summary = next(e for e in events if e["type"] == "summary")
    # 精选源应包含 5 篇（specifications 被排除）
    assert summary["pages"] == 5
    assert summary["compiled"] == 5 and summary["failed"] == 0
    # 落盘校验
    assert store.is_indexed()
    assert len(store.list_pages()) == 5
    assert "openUBMC" in (store.read_index() or "")


async def test_sync_stream_incremental_reuse(wiki_tmp, fake_clone):
    llm = DistillLLM()
    await _run_sync(llm)
    first_calls = llm.calls
    # 第二次同源（fake_clone 内容不变）：应全部复用，几乎不调 LLM 提炼页
    llm2 = DistillLLM()
    events = await _run_sync(llm2)
    summary = next(e for e in events if e["type"] == "summary")
    assert summary["reused"] == 5 and summary["compiled"] == 0
    # 第二次只剩索引总览 1 次 LLM 调用
    assert llm2.calls < first_calls


async def test_sync_stream_no_llm_errors(wiki_tmp, fake_clone):
    events = await _run_sync(None)
    assert events and events[0]["type"] == "error"
    assert not store.is_indexed()


# ============================================================
# wiki_tools
# ============================================================
async def test_tools_not_indexed(wiki_tmp):
    ds = LogDataset(entries=[], summary={})
    assert "error" in await wiki_tools.get_wiki_index(ds)
    assert "error" in await wiki_tools.read_wiki_page(ds, "x")
    out = await wiki_tools.search_wiki(ds, "架构")
    assert "error" in out and out["results"] == []


async def test_tools_after_compile(wiki_tmp, fake_clone):
    await _run_sync(DistillLLM())
    ds = LogDataset(entries=[], summary={})

    idx = await wiki_tools.get_wiki_index(ds)
    assert idx["total"] == 5 and idx["index"]
    slug = idx["pages"][0]["slug"]

    page = await wiki_tools.read_wiki_page(ds, slug)
    assert "content" in page and page["content"]

    miss = await wiki_tools.read_wiki_page(ds, "nope")
    assert "error" in miss

    sr = await wiki_tools.search_wiki(ds, "mdb 接口", top_k=3)
    assert sr["total"] >= 1
    assert {"slug", "title", "description", "snippet"} <= set(sr["results"][0])


# ============================================================
# routes
# ============================================================
@pytest.fixture
def inject_fake_llm():
    """临时给 wiki routes 注入假 LLM，测试后还原。"""
    prev = wiki_routes._LLM
    llm = DistillLLM()
    wiki_routes.set_llm(llm)
    yield llm
    wiki_routes.set_llm(prev)


def test_status_requires_auth(client):
    assert client.get("/api/wiki/status").status_code == 401


def test_sync_and_read_endpoints(authed, inject_fake_llm, monkeypatch):
    client, headers = authed["client"], authed["headers"]
    # 造文档树替代真 clone
    monkeypatch.setattr(
        service, "_clone_docs",
        lambda git_url, branch, dest, timeout=600: (_write_fake_docs(dest) or "c0ffee00c0ffee00"),
    )

    # 空状态
    assert client.get("/api/wiki/status", headers=headers).json()["indexed"] is False

    # 编译（SSE）
    from tests.conftest import parse_sse_events
    resp = client.post("/api/wiki/sync", json={}, headers=headers)
    assert resp.status_code == 200
    events = parse_sse_events(resp)
    assert events[-1]["type"] == "done"
    assert any(e["type"] == "summary" and e["pages"] == 5 for e in events)

    # 状态 / 索引 / 单页 / 检索
    st = client.get("/api/wiki/status", headers=headers).json()
    assert st["indexed"] and st["meta"]["page_count"] == 5

    idx = client.get("/api/wiki/index", headers=headers).json()
    assert idx["pages"] and idx["index"]
    slug = idx["pages"][0]["slug"]

    pg = client.get("/api/wiki/page", params={"slug": slug}, headers=headers)
    assert pg.status_code == 200 and pg.json()["content"]
    assert client.get("/api/wiki/page", params={"slug": "nope"}, headers=headers).status_code == 404

    sr = client.get("/api/wiki/search", params={"q": "mdb 接口"}, headers=headers)
    assert sr.status_code == 200 and sr.json()["results"]
