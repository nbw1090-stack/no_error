"""从**本地已克隆**的 openUBMC docs 仓编译 LLM Wiki（绕过 service 里对 gitcode 的慢克隆）。
复用 wiki.compile 的提炼逻辑 + wiki.store 落盘，等价于 sync_stream 但 repo 直接给本地路径。

用法：venv/bin/python -m eval.compile_wiki_local /abs/path/to/docs_repo
"""
import asyncio, logging, sys
from datetime import datetime, timezone
from config import AppConfig
from agent.llm.factory import create_llm
from wiki import compile as wc
from wiki import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("compile_local")

_CONCURRENCY = 4


async def main(repo_dir: str):
    cfg = AppConfig.from_env()
    store.init_wiki_dir()
    llm = create_llm(cfg.llm)
    assert llm is not None, "无 LLM"

    items = wc.plan_sources(repo_dir)
    log.info("规划精选源文档: %d 篇", len(items))
    assert items, f"未在 {repo_dir}/docs/zh/development 找到精选源文档"

    sem = asyncio.Semaphore(_CONCURRENCY)
    async def one(it):
        async with sem:
            try:
                p = await wc.compile_page(llm, it)
                log.info("  compiled %s", p["slug"])
                return p
            except Exception as e:  # noqa: BLE001
                log.warning("  FAILED %s: %s", it["slug"], e)
                return None
    pages = [p for p in await asyncio.gather(*[one(it) for it in items]) if p]
    assert pages, "全部页编译失败"

    pages = wc.interlink(pages)
    for p in pages:
        store.write_page(p["slug"], p["body"])
    arch = wc.find_architecture_text(repo_dir)
    overview = await wc.build_index_overview(llm, pages, arch)
    store.write_index(wc.render_index(overview, pages))
    store.write_manifest({
        "git_url": "local:" + repo_dir,
        "branch": "", "commit": "", "model": getattr(llm, "model", ""),
        "compiled_at": datetime.now(timezone.utc).isoformat(),
        "source_count": len(items),
        "pages": [{"slug": p["slug"], "title": p["title"], "description": p["description"],
                   "section": p["section"], "sources": p["sources"], "source_hash": p["source_hash"]}
                  for p in sorted(pages, key=lambda x: (x["section"], x["title"]))],
    })
    log.info("DONE indexed=%s pages=%d", store.is_indexed(), len(store.list_pages()))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
