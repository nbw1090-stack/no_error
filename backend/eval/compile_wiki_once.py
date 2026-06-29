"""一次性把 openUBMC 文档编译成 LLM Wiki（驱动 wiki.service.sync_stream）。
用于 wiki A/B 评测前置：cd backend && venv/bin/python -m eval.compile_wiki_once
"""
import asyncio, logging
from config import AppConfig
from agent.llm.factory import create_llm
from wiki import store as wiki_store
from wiki import service as wiki_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

async def main():
    cfg = AppConfig.from_env()
    wiki_store.init_wiki_dir()
    llm = create_llm(cfg.llm)
    assert llm is not None, "无 LLM"
    async for ev in wiki_service.sync_stream(llm, force=False):
        t = ev.get("type")
        if t == "page":
            print(f"  page[{ev['index']}/{ev['total']}] {ev['status']:8} {ev['slug']}")
        elif t in ("plan_done","summary","error","plan","progress"):
            print(t, {k:v for k,v in ev.items() if k!='type'})
    print("indexed:", wiki_store.is_indexed(), "pages:", len(wiki_store.list_pages()))

asyncio.run(main())
