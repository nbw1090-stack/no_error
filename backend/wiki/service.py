"""
LLM Wiki 同步编排（SSE）：克隆 openUBMC 文档 → LLM 提炼成 wiki 页 → 生成索引。

与「检索式」不同，这里走 Karpathy「LLM Wiki」模式：每篇精选源文档由 LLM 提炼成一页
结构化 markdown，再让 LLM 写一段架构总览拼成索引页。编译需要 LLM（无 LLM_API_KEY 时报错）。

特性：
- 增量可续编：已编译且源文档未变（source_hash 一致）的页直接复用，不重复调 LLM。
- 有界并发：用信号量限制同时进行的 LLM 调用，按完成顺序 yield 进度。
- 全局共享：编译结果落在 <WIKI_DIR>，所有用户的 agent 共用。

事件序列：plan → progress(cloning/planning) → page* → progress(indexing) → summary → done
失败：clone/plan/index 阶段失败 yield error；单页失败只记 failed，不影响其它页。
"""

import asyncio
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

from wiki import compile as wcompile
from wiki import store

DEFAULT_GIT_URL = "https://gitcode.com/openUBMC/docs.git"
DEFAULT_BRANCH = ""
# 只稀疏检出中文文档（编译只用 docs/zh）
_SPARSE_PATHS = ["docs/zh"]
# 同时进行的 LLM 编译并发上限
_MAX_CONCURRENCY = 4


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clone_docs(git_url: str, branch: str, dest: str, timeout: int = 600) -> str:
    """blobless + sparse 浅克隆，只检出 docs/zh，返回 HEAD commit sha。"""
    cmd = [
        "git", "clone", "--depth", "1",
        "--filter=blob:none", "--sparse", "--no-tags",
    ]
    if branch:
        cmd += ["--branch", branch]
    cmd += [git_url, dest]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr or proc.stdout or "git clone failed").strip()
            or "git clone failed"
        )
    sp = subprocess.run(
        ["git", "-C", dest, "sparse-checkout", "set", *_SPARSE_PATHS],
        capture_output=True, text=True, timeout=120,
    )
    if sp.returncode != 0:
        raise RuntimeError(
            (sp.stderr or "git sparse-checkout failed").strip()
            or "git sparse-checkout failed"
        )
    rev = subprocess.run(
        ["git", "-C", dest, "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=30,
    )
    return rev.stdout.strip() if rev.returncode == 0 else ""


def _existing_hashes() -> dict:
    """已编译页的 {slug: source_hash}，用于增量复用。"""
    manifest = store.read_manifest()
    if not manifest:
        return {}
    return {
        p["slug"]: p.get("source_hash", "")
        for p in manifest.get("pages", [])
        if p.get("slug")
    }


async def sync_stream(llm, git_url=None, branch=None, force=False):
    """
    LLM Wiki 全量/增量编译异步生成器：逐个 yield SSE 事件 dict。

    llm：LLM adapter（None 时直接报错——编译离不开 LLM）。
    force：True 时忽略已编译缓存，全部重编。
    """
    git_url = git_url or DEFAULT_GIT_URL
    branch = branch or DEFAULT_BRANCH

    if llm is None:
        yield {
            "type": "error",
            "stage": "init",
            "message": "未配置 LLM（LLM_API_KEY 为空），无法编译 wiki。",
        }
        return

    yield {"type": "plan", "git_url": git_url, "branch": branch or "(default)"}

    tmp_root = tempfile.mkdtemp(prefix="wiki_compile_")
    dest = os.path.join(tmp_root, "docs")
    try:
        # 1) 克隆
        yield {"type": "progress", "stage": "cloning"}
        try:
            commit = await asyncio.to_thread(_clone_docs, git_url, branch, dest)
        except Exception as e:
            yield {"type": "error", "stage": "cloning", "message": str(e)}
            return

        # 2) 规划精选源文档
        yield {"type": "progress", "stage": "planning"}
        items = await asyncio.to_thread(wcompile.plan_sources, dest)
        total = len(items)
        if total == 0:
            yield {
                "type": "error",
                "stage": "planning",
                "message": "未在 docs/zh/development 找到可编译的精选源文档",
            }
            return
        yield {"type": "plan_done", "total": total}

        existing = {} if force else _existing_hashes()
        store.init_wiki_dir()

        # 3) 有界并发编译/复用每页
        sem = asyncio.Semaphore(_MAX_CONCURRENCY)
        counters = {"compiled": 0, "reused": 0, "failed": 0}

        async def _one(item):
            slug = item["slug"]
            # 复用：源未变且页文件在
            if (
                not force
                and existing.get(slug) == item["source_hash"]
                and store.read_page_raw(slug) is not None
            ):
                meta = store.page_meta(slug) or {}
                return {
                    "status": "reused",
                    "page": {
                        "slug": slug,
                        "title": meta.get("title", item["title_hint"]),
                        "description": meta.get("description", ""),
                        "body": store.read_page_raw(slug) or "",
                        "section": item["section"],
                        "sources": meta.get("sources", [item["rel_path"]]),
                        "source_hash": item["source_hash"],
                    },
                }
            async with sem:
                try:
                    page = await wcompile.compile_page(llm, item)
                    return {"status": "compiled", "page": page}
                except Exception as e:
                    return {"status": "failed", "slug": slug,
                            "title": item["title_hint"], "error": str(e)}

        tasks = [asyncio.ensure_future(_one(it)) for it in items]
        pages: list[dict] = []
        done_n = 0
        for fut in asyncio.as_completed(tasks):
            res = await fut
            done_n += 1
            status = res["status"]
            counters[status] += 1
            if status == "failed":
                yield {
                    "type": "page", "status": "failed", "index": done_n,
                    "total": total, "slug": res["slug"], "title": res["title"],
                    "error": res["error"],
                }
            else:
                page = res["page"]
                pages.append(page)
                yield {
                    "type": "page", "status": status, "index": done_n,
                    "total": total, "slug": page["slug"], "title": page["title"],
                }

        if not pages:
            yield {
                "type": "error",
                "stage": "compiling",
                "message": "所有页编译失败（检查 LLM 配置/额度）",
            }
            return

        # 4) 互链 + 落盘各页
        yield {"type": "progress", "stage": "indexing"}
        pages = await asyncio.to_thread(wcompile.interlink, pages)
        for p in pages:
            await asyncio.to_thread(store.write_page, p["slug"], p["body"])

        # 5) 索引页（LLM 写架构总览 + 分组目录）
        arch_text = await asyncio.to_thread(wcompile.find_architecture_text, dest)
        overview = await wcompile.build_index_overview(llm, pages, arch_text)
        index_md = wcompile.render_index(overview, pages)
        await asyncio.to_thread(store.write_index, index_md)

        # 6) 清理本次未产出的旧页（源文档已删除）
        new_slugs = {p["slug"] for p in pages}
        for old_slug in existing:
            if old_slug not in new_slugs:
                await asyncio.to_thread(store.delete_page, old_slug)

        # 7) manifest
        compiled_at = _now_iso()
        manifest = {
            "git_url": git_url,
            "branch": branch,
            "commit": commit,
            "model": getattr(llm, "model", ""),
            "compiled_at": compiled_at,
            "source_count": total,
            "pages": [
                {
                    "slug": p["slug"],
                    "title": p["title"],
                    "description": p["description"],
                    "section": p["section"],
                    "sources": p["sources"],
                    "source_hash": p["source_hash"],
                }
                for p in sorted(pages, key=lambda x: (x["section"], x["title"]))
            ],
        }
        await asyncio.to_thread(store.write_manifest, manifest)

        yield {
            "type": "summary",
            "git_url": git_url,
            "branch": branch or "(default)",
            "commit": (commit or "")[:8],
            "model": manifest["model"],
            "pages": len(pages),
            "compiled": counters["compiled"],
            "reused": counters["reused"],
            "failed": counters["failed"],
            "compiled_at": compiled_at,
        }
        yield {"type": "done"}
    finally:
        await asyncio.to_thread(shutil.rmtree, tmp_root, True)
