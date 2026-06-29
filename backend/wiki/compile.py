"""
LLM Wiki 编译流水线：把 openUBMC 原始文档**提炼**成结构化、互相链接的 wiki 页。

这是 Karpathy「LLM Wiki」模式的核心：不是把原文切片检索，而是让 LLM 读原始文档、
压缩提炼成一页结构化 markdown（概述 / 关键设计 / 接口 / 相关主题），并由索引页串联。

- plan_sources：从克隆出的文档树里挑「精选架构核心」子集（design_reference / api /
  quick_start / glossary），每个源文档对应一篇 wiki 页（跳过过小文件）。
- compile_page：对单篇源文档调用一次 LLM，产出提炼后的 markdown 页（含 title/description）。
- interlink：编译完成后的轻量后处理，把页正文里出现的其它页标题改成页内链接 [[slug]]。
- build_index：让 LLM 基于架构总文档 + 各页标题/简介写一段「架构总览」，再拼出分组目录。

所有 LLM 调用经传入的 adapter（await llm.chat(messages)）完成；无 LLM 时由调用方报错。
"""

import hashlib
import os
import re

# 精选架构核心：只编译这些 development/ 下的子树（相对仓库根的 development/ 之后部分）
_CURATED_SECTIONS = ("design_reference", "api", "quick_start")
# 额外单文件（development/ 之后的相对路径）
_CURATED_FILES = ("glossary.md", "introduction.md")
# 跳过过小 / 噪声文件
_MIN_SOURCE_BYTES = 200
# 单篇喂给 LLM 的原文上限（控制 token；超出截断并提示）
_MAX_SOURCE_CHARS = 16000

_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)


# ============================================================
# 规划：选出要编译的源文档
# ============================================================
def _rel_under_development(rel_path: str) -> str:
    marker = "development/"
    return rel_path.split(marker, 1)[1] if marker in rel_path else rel_path


def _is_curated(rel_path: str) -> bool:
    sub = _rel_under_development(rel_path)
    if sub in _CURATED_FILES:
        return True
    top = sub.split("/", 1)[0]
    return top in _CURATED_SECTIONS


def _title_of(text: str, fallback: str) -> str:
    body = _FRONTMATTER_RE.sub("", text)
    m = _H1_RE.search(body)
    return m.group(1).strip() if m else fallback


def plan_sources(repo_dir: str) -> list[dict]:
    """
    遍历克隆树，选出「精选架构核心」源文档，返回待编译项列表。

    每项：{rel_path, section, title_hint, slug, source_text, source_hash}。
    （slug 由 store.slugify 生成，保证与落盘文件名一致。）
    """
    from wiki import store

    items: list[dict] = []
    seen_slugs: set[str] = set()
    base = os.path.join(repo_dir, "docs", "zh", "development")
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "images"]
        for fn in files:
            if not fn.endswith(".md"):
                continue
            abs_path = os.path.join(root, fn)
            rel_path = os.path.relpath(abs_path, repo_dir).replace("\\", "/")
            if not _is_curated(rel_path):
                continue
            try:
                with open(abs_path, encoding="utf-8") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            if len(text.encode("utf-8")) < _MIN_SOURCE_BYTES:
                continue
            slug = store.slugify(rel_path)
            # slug 冲突（极少）→ 追加短哈希
            if slug in seen_slugs:
                slug = slug + "-" + hashlib.sha1(rel_path.encode()).hexdigest()[:6]
            seen_slugs.add(slug)
            sub = _rel_under_development(rel_path)
            section = sub.split("/", 1)[0] if "/" in sub else "overview"
            items.append(
                {
                    "rel_path": rel_path,
                    "section": section,
                    "title_hint": _title_of(text, os.path.splitext(fn)[0]),
                    "slug": slug,
                    "source_text": text,
                    "source_hash": hashlib.sha1(text.encode("utf-8")).hexdigest(),
                }
            )
    items.sort(key=lambda x: (x["section"], x["rel_path"]))
    return items


# ============================================================
# 编译单页
# ============================================================
_PAGE_SYSTEM = (
    "你是 openUBMC（一个 BMC 固件系统）的资料编纂者。你的任务是把给定的一篇原始文档"
    "**提炼**成知识库里的一页：用简体中文，结构化、精炼、面向工程师，保留架构/设计/接口"
    "的关键事实，去掉冗长铺垫、变更记录、版权声明等噪声。\n\n"
    "严格按以下 markdown 结构输出（不要用代码围栏包裹整页）：\n"
    "# <页面标题>\n"
    "> <一句话描述这页讲什么>\n\n"
    "## 概述\n<2-5 句，这块在 BMC 架构里的定位与职责>\n\n"
    "## 关键设计 / 接口\n<要点列表：核心概念、数据/对象模型、关键接口名"
    "（如 mdb / D-Bus / Redfish 的接口与对象）、约束>\n\n"
    "## 相关主题\n<列出与本页相关的其它主题名词，便于交叉索引>\n\n"
    "要求：只输出这一页 markdown 本身；不要编造原文没有的接口名或字段。"
)


def _extract_title_desc(markdown: str, fallback_title: str) -> tuple[str, str]:
    """从编译产物里抽取 title（# 行）与 description（> 行 / 首段）。"""
    title = fallback_title
    m = _H1_RE.search(markdown)
    if m:
        title = m.group(1).strip()
    desc = ""
    dm = re.search(r"^>\s+(.+?)\s*$", markdown, re.MULTILINE)
    if dm:
        desc = dm.group(1).strip()
    else:
        for line in markdown.splitlines():
            s = line.strip()
            if s and not s.startswith("#") and not s.startswith(">"):
                desc = s[:100]
                break
    return title, desc


async def compile_page(llm, item: dict) -> dict:
    """
    对单篇源文档调用 LLM，产出提炼页。

    返回 {slug, title, description, body, section, sources, source_hash}。
    LLM 异常或空输出 → 抛异常，由调用方记为该页失败（不影响其它页）。
    """
    src = item["source_text"]
    if len(src) > _MAX_SOURCE_CHARS:
        src = src[:_MAX_SOURCE_CHARS] + "\n\n（原文过长，已截断）"
    user = (
        f"原始文档路径：{item['rel_path']}\n"
        f"原始文档标题（参考）：{item['title_hint']}\n\n"
        f"=== 原始文档开始 ===\n{src}\n=== 原始文档结束 ==="
    )
    resp = await llm.chat(
        [
            {"role": "system", "content": _PAGE_SYSTEM},
            {"role": "user", "content": user},
        ]
    )
    body = (resp.get("content") or "").strip()
    if not body:
        raise RuntimeError("LLM 返回空内容")
    title, desc = _extract_title_desc(body, item["title_hint"])
    return {
        "slug": item["slug"],
        "title": title,
        "description": desc,
        "body": body,
        "section": item["section"],
        "sources": [item["rel_path"]],
        "source_hash": item["source_hash"],
    }


# ============================================================
# 互链后处理：页正文里出现其它页标题 → [[slug]]
# ============================================================
def interlink(pages: list[dict]) -> list[dict]:
    """
    把每页正文里**首次**出现的其它页标题替换成 markdown 链接 [标题](./<slug>.md)。

    只链接长度≥5 的标题、且不链接自身，降低误链；返回更新后的 pages（body 被改写）。
    """
    # 长标题优先，避免短标题抢先匹配
    title_slug = sorted(
        ((p["title"], p["slug"]) for p in pages if len(p["title"]) >= 5),
        key=lambda x: -len(x[0]),
    )
    for p in pages:
        body = p["body"]
        for title, slug in title_slug:
            if slug == p["slug"]:
                continue
            # 跳过已是链接的、标题行（# 开头）的匹配：只在正文纯文本里替换首次出现
            idx = body.find(title)
            if idx == -1:
                continue
            # 避免替换到标题行/已有链接：简单判断前一个字符不是 [ 或 (
            prev = body[idx - 1] if idx > 0 else ""
            if prev in "[(/":
                continue
            link = f"[{title}](./{slug}.md)"
            body = body[:idx] + link + body[idx + len(title) :]
        p["body"] = body
    return pages


# ============================================================
# 索引页
# ============================================================
_INDEX_SYSTEM = (
    "你是 openUBMC 的资料编纂者。下面给你 openUBMC 的架构总文档片段，以及知识库各页的"
    "标题与简介。请用简体中文写一段精炼的「openUBMC 架构总览」（3-6 段）：讲清整体架构、"
    "微组件/模型、组件间协作（mdb/D-Bus）、对外接口（Redfish）等主线，让读者据此知道该去"
    "看哪些页。只输出这段总览正文，不要列目录（目录会自动生成）。"
)


async def build_index_overview(llm, pages: list[dict], architecture_text: str) -> str:
    """让 LLM 写「架构总览」段落；失败/无 LLM 时返回简短缺省语。"""
    arch = (architecture_text or "")[:8000]
    listing = "\n".join(f"- {p['title']}：{p['description']}" for p in pages)
    user = (
        f"=== 架构总文档（节选）===\n{arch}\n\n"
        f"=== 知识库页面清单 ===\n{listing}"
    )
    try:
        resp = await llm.chat(
            [
                {"role": "system", "content": _INDEX_SYSTEM},
                {"role": "user", "content": user},
            ]
        )
        ov = (resp.get("content") or "").strip()
        return ov or "openUBMC 架构知识库（自动编译）。"
    except Exception:
        return "openUBMC 架构知识库（自动编译）。"


_SECTION_LABEL = {
    "design_reference": "架构与设计",
    "api": "接口（API）",
    "quick_start": "快速上手",
    "overview": "总览",
}


def render_index(overview: str, pages: list[dict]) -> str:
    """拼出 index.md：架构总览 + 按 section 分组的目录。"""
    lines = ["# openUBMC 知识库", "", overview.strip(), "", "## 目录", ""]
    # 分组
    by_section: dict[str, list[dict]] = {}
    for p in pages:
        by_section.setdefault(p.get("section", "overview"), []).append(p)
    for section in sorted(by_section.keys()):
        label = _SECTION_LABEL.get(section, section)
        lines.append(f"### {label}")
        lines.append("")
        for p in sorted(by_section[section], key=lambda x: x["title"]):
            desc = f" — {p['description']}" if p.get("description") else ""
            lines.append(f"- [{p['title']}](./pages/{p['slug']}.md){desc}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def find_architecture_text(repo_dir: str) -> str:
    """取架构总文档原文（design_reference/architecture.md），供索引总览参考。"""
    p = os.path.join(
        repo_dir, "docs", "zh", "development", "design_reference", "architecture.md"
    )
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""
    return ""
