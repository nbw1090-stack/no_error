"""
LLM Wiki 检索工具（group="wiki"）—— 页导航 + 检索兜底

采用 Karpathy「LLM Wiki」模式：wiki 是 LLM 把 openUBMC 文档**提炼**成的一套结构化、
互链 markdown 页。agent 的主路径是「读索引页 → 读整页」，而非对原文做切片检索。

- get_wiki_index：读知识库首页（架构总览 + 分组目录，含各页 slug/标题/简介）。
- read_wiki_page：按 slug 读某页提炼内容（含页内 [[slug]] 互链）。
- search_wiki：兜底，当从索引判断不出该读哪页时，用关键词在已编译页里检索。

wiki 全局共享（不按用户隔离），故这些工具不依赖 dataset/user_id；首个位置参数 dataset
仅为与其它工具签名一致，内部不使用。
"""

from agent.tools.registry import ToolRegistry
from agent.dataset import LogDataset
from wiki import store as wiki_store


_NOT_INDEXED_MSG = (
    "openUBMC 知识库尚未编译。请在「Wiki 知识库」面板点击编译，"
    "或调用 POST /api/wiki/sync 让 LLM 从 openUBMC 文档提炼出 wiki 后再查阅。"
)


@ToolRegistry.register(
    name="get_wiki_index",
    description=(
        "读取 openUBMC 知识库首页：一段架构总览 + 按主题分组的目录（列出每页的 slug、"
        "标题、一句话简介）。**回答任何涉及 BMC/openUBMC 架构、组件模型、接口、设计的"
        "问题时，先调用它**了解有哪些页、该读哪页，再用 read_wiki_page 读整页。无参数。"
    ),
    parameters={"type": "object", "properties": {}},
    group="wiki",
)
async def get_wiki_index(dataset: LogDataset) -> dict:
    """返回索引页 markdown（架构总览 + 分组目录）+ 页清单（slug/标题/简介）。"""
    if not wiki_store.is_indexed():
        return {"error": _NOT_INDEXED_MSG}
    index_md = wiki_store.read_index() or ""
    pages = [
        {
            "slug": p.get("slug"),
            "title": p.get("title"),
            "description": p.get("description", ""),
            "section": p.get("section", ""),
        }
        for p in wiki_store.list_pages()
    ]
    return {"index": index_md, "pages": pages, "total": len(pages)}


@ToolRegistry.register(
    name="read_wiki_page",
    description=(
        "按 slug 读取 openUBMC 知识库某一页的完整提炼内容（结构化 markdown，含与其它页的"
        "互链）。slug 来自 get_wiki_index 的目录或 search_wiki 的结果。需要某主题的设计/"
        "接口细节时调用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": "页面 slug（get_wiki_index / search_wiki 返回的 slug 字段）",
            }
        },
        "required": ["slug"],
    },
    group="wiki",
)
async def read_wiki_page(dataset: LogDataset, slug: str) -> dict:
    """读整页。未找到返回提示。"""
    if not wiki_store.is_indexed():
        return {"error": _NOT_INDEXED_MSG}
    page = wiki_store.read_page(slug)
    if page is None:
        return {
            "error": f"未找到页面：{slug}。请先用 get_wiki_index 查看可用页的 slug。"
        }
    return page


@ToolRegistry.register(
    name="search_wiki",
    description=(
        "在已编译的 openUBMC 知识库页里做关键词检索（**兜底**用：当从 get_wiki_index 的"
        "目录判断不出该读哪页时再用）。返回若干最相关页的 slug/标题/简介/摘要，"
        "再用 read_wiki_page 读全文。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索关键词，可中英文混合，如『组件模型』『mdb 接口』",
            },
            "top_k": {
                "type": "integer",
                "description": "返回页数，默认 5（范围 1-20）",
            },
        },
        "required": ["query"],
    },
    group="wiki",
)
async def search_wiki(dataset: LogDataset, query: str, top_k: int = 5) -> dict:
    """关键词检索已编译页（页导航的兜底）。"""
    if not wiki_store.is_indexed():
        return {"error": _NOT_INDEXED_MSG, "results": []}
    try:
        k = max(1, min(int(top_k), 20))
    except (TypeError, ValueError):
        k = 5
    results = wiki_store.search(query, top_k=k)
    return {
        "query": query,
        "total": len(results),
        "results": [
            {
                "slug": r["slug"],
                "title": r["title"],
                "description": r["description"],
                "snippet": r["snippet"],
            }
            for r in results
        ],
    }
