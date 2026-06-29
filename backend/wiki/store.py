"""
LLM Wiki 文件存储（磁盘 markdown，**全局共享**，不按用户隔离）

采用 Karpathy「LLM Wiki」模式：不是把原始文档切片做向量/关键词检索，而是让 LLM 把
openUBMC 原始文档**提炼成一套结构化、互相链接的 markdown 知识库**，agent 直接读整页。

磁盘布局（<WIKI_DIR> 默认 backend/data/wiki/）：
- index.md          —— LLM 维护的总览 + 目录（按 section 分组，列出每页 slug/标题/一句话）
- pages/<slug>.md   —— 每篇提炼页（页内用 [[slug]] 互链）
- manifest.json     —— 元信息：来源 git/commit、编译模型/时间、各页 {slug,title,description,
                       sources,source_hash,section}

检索兜底（search）：把已编译的 wiki 页加载进内存，用 LIKE 子串 + 中文 2-gram 打分
（与页导航互补，找不到合适页时用）。纯标准库，无第三方依赖。
"""

import json
import os
import re
import shutil
from datetime import datetime, timezone

# 模块级可变路径（导入时按 config.data_dir 推导默认值）
WIKI_DIR = ""


def _default_wiki_dir() -> str:
    from config import AppConfig

    return os.path.join(AppConfig.from_env().data_dir, "wiki")


WIKI_DIR = _default_wiki_dir()

# 进程内检索缓存：按 manifest.compiled_at 失效
_SEARCH_CACHE: dict = {"key": None, "rows": None}


def set_wiki_dir(path: str) -> None:
    """重定向 wiki 目录（测试用）。同时清空检索缓存。"""
    global WIKI_DIR
    WIKI_DIR = path
    _SEARCH_CACHE["key"] = None
    _SEARCH_CACHE["rows"] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- 路径 ----
def _pages_dir() -> str:
    return os.path.join(WIKI_DIR, "pages")


def _manifest_path() -> str:
    return os.path.join(WIKI_DIR, "manifest.json")


def _index_path() -> str:
    return os.path.join(WIKI_DIR, "index.md")


def _page_path(slug: str) -> str:
    return os.path.join(_pages_dir(), f"{slug}.md")


def init_wiki_dir() -> None:
    """幂等创建 wiki 目录骨架。"""
    os.makedirs(_pages_dir(), exist_ok=True)


# ============================================================
# slug
# ============================================================
def slugify(rel_path: str) -> str:
    """
    源文档相对路径 → 文件名安全的 slug。

    取 development/ 之后的部分，去扩展名，非 [a-z0-9] 段折叠为 '-'；若 ascii 化后为空
    （纯中文文件名）则回退为短哈希，保证唯一可落盘。
    """
    rel = rel_path
    marker = "development/"
    if marker in rel:
        rel = rel.split(marker, 1)[1]
    rel = re.sub(r"\.md$", "", rel)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", rel).strip("-").lower()
    if not slug:
        import hashlib

        slug = "doc-" + hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:10]
    return slug


# ============================================================
# 读写：manifest / index / page
# ============================================================
def read_manifest() -> dict | None:
    p = _manifest_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def write_manifest(manifest: dict) -> None:
    init_wiki_dir()
    tmp = _manifest_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _manifest_path())
    _SEARCH_CACHE["key"] = None
    _SEARCH_CACHE["rows"] = None


def read_index() -> str | None:
    p = _index_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def write_index(markdown: str) -> None:
    init_wiki_dir()
    tmp = _index_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(markdown)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _index_path())


def write_page(slug: str, markdown: str) -> None:
    init_wiki_dir()
    tmp = _page_path(slug) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(markdown)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _page_path(slug))


def read_page_raw(slug: str) -> str | None:
    p = _page_path(slug)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def delete_page(slug: str) -> None:
    try:
        os.remove(_page_path(slug))
    except OSError:
        pass


def page_meta(slug: str) -> dict | None:
    """从 manifest 取某页的元信息。"""
    manifest = read_manifest()
    if not manifest:
        return None
    for pg in manifest.get("pages", []):
        if pg.get("slug") == slug:
            return pg
    return None


def read_page(slug: str) -> dict | None:
    """读整页（正文 + manifest 元信息）。未找到返回 None。"""
    body = read_page_raw(slug)
    if body is None:
        return None
    meta = page_meta(slug) or {}
    return {
        "slug": slug,
        "title": meta.get("title", slug),
        "description": meta.get("description", ""),
        "sources": meta.get("sources", []),
        "content": body,
    }


# ============================================================
# 状态
# ============================================================
def list_pages() -> list[dict]:
    manifest = read_manifest()
    return manifest.get("pages", []) if manifest else []


def is_indexed() -> bool:
    """是否已编译出可用的 wiki（manifest 有页 + index.md 存在）。"""
    manifest = read_manifest()
    return bool(
        manifest
        and manifest.get("pages")
        and os.path.exists(_index_path())
    )


def get_status() -> dict:
    """wiki 状态（供 /status 端点与前端面板）。"""
    manifest = read_manifest()
    if not manifest or not manifest.get("pages"):
        return {"indexed": False, "meta": None}
    pages = manifest.get("pages", [])
    return {
        "indexed": os.path.exists(_index_path()),
        "meta": {
            "git_url": manifest.get("git_url", ""),
            "branch": manifest.get("branch", ""),
            "commit_sha": manifest.get("commit", ""),
            "model": manifest.get("model", ""),
            "compiled_at": manifest.get("compiled_at", ""),
            "page_count": len(pages),
            "source_count": manifest.get("source_count", 0),
        },
        "pages": [
            {
                "slug": p.get("slug"),
                "title": p.get("title"),
                "description": p.get("description", ""),
                "section": p.get("section", ""),
            }
            for p in pages
        ],
    }


# ============================================================
# 检索兜底（LIKE 子串 + 中文 2-gram 打分，over 已编译页）
# ============================================================
_CJK_RE = re.compile(r"[一-鿿㐀-䶿]+")
_ASCII_RE = re.compile(r"[A-Za-z0-9_]{2,}")


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for m in _ASCII_RE.finditer(query):
        terms.append(m.group(0).lower())
    for m in _CJK_RE.finditer(query):
        run = m.group(0)
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[i : i + 2] for i in range(len(run) - 1))
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _load_rows() -> list[dict]:
    """加载全部已编译页到内存（按 compiled_at 缓存）。"""
    manifest = read_manifest()
    key = manifest.get("compiled_at") if manifest else None
    key = (WIKI_DIR, key)
    if _SEARCH_CACHE["key"] == key and _SEARCH_CACHE["rows"] is not None:
        return _SEARCH_CACHE["rows"]
    rows: list[dict] = []
    for pg in (manifest.get("pages", []) if manifest else []):
        slug = pg.get("slug")
        body = read_page_raw(slug) or ""
        rows.append(
            {
                "slug": slug,
                "title": pg.get("title", slug),
                "description": pg.get("description", ""),
                "_title_l": (pg.get("title", "") or "").lower(),
                "_desc_l": (pg.get("description", "") or "").lower(),
                "_body_l": body.lower(),
                "content": body,
            }
        )
    _SEARCH_CACHE["key"] = key
    _SEARCH_CACHE["rows"] = rows
    return rows


def _make_snippet(content: str, terms: list[str], width: int = 220) -> str:
    low = content.lower()
    pos = -1
    for t in terms:
        i = low.find(t)
        if i != -1 and (pos == -1 or i < pos):
            pos = i
    if pos == -1:
        snippet = content[:width]
    else:
        start = max(0, pos - width // 3)
        snippet = content[start : start + width]
        if start > 0:
            snippet = "…" + snippet
    snippet = " ".join(snippet.split())
    if len(content) > len(snippet):
        snippet += "…"
    return snippet


def search(query: str, top_k: int = 5) -> list[dict]:
    """关键词检索已编译的 wiki 页（页导航找不到合适页时的兜底）。"""
    terms = _query_terms(query or "")
    if not terms:
        return []
    rows = _load_rows()
    scored = []
    for r in rows:
        score = 0
        matched = 0
        for t in terms:
            ct = r["_title_l"].count(t)
            cd = r["_desc_l"].count(t)
            cb = r["_body_l"].count(t)
            if ct or cd or cb:
                matched += 1
            score += ct * 6 + cd * 4 + cb
        if score <= 0:
            continue
        score += matched * 4
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            "slug": r["slug"],
            "title": r["title"],
            "description": r["description"],
            "snippet": _make_snippet(r["content"], terms),
            "score": score,
        }
        for score, r in scored[: max(1, top_k)]
    ]


# ============================================================
# 维护
# ============================================================
def clear() -> None:
    """清空整个 wiki 目录（慎用）。"""
    if os.path.isdir(WIKI_DIR):
        shutil.rmtree(WIKI_DIR, ignore_errors=True)
    _SEARCH_CACHE["key"] = None
    _SEARCH_CACHE["rows"] = None
