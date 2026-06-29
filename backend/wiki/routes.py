"""
LLM Wiki API 路由（前缀 /api/wiki，tags=["wiki"]）

wiki 是**全局共享**的 LLM 提炼知识库（openUBMC 文档），不按用户隔离，但所有端点仍需登录。

端点：
- POST /sync     LLM 编译/增量重编（SSE 流）：plan → progress* → page* → summary → done
- GET  /status   当前 wiki 状态（来源/commit/模型/页数/编译时间 + 页清单；未编译时 indexed=false）
- GET  /index    知识库索引页 markdown + 页清单
- GET  /page     按 slug 读某页
- GET  /search   调试用关键词检索（agent 走工具）
"""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from auth.dependency import get_current_user
from wiki import service, store

router = APIRouter(prefix="/api/wiki", tags=["wiki"])

# main 启动时通过 set_llm 注入 LLM adapter（可能为 None：未配置 LLM_API_KEY）
_LLM = None


def set_llm(llm) -> None:
    """注入 LLM adapter（main 启动时调用）。"""
    global _LLM
    _LLM = llm


class SyncRequest(BaseModel):
    """编译请求体（均可选）。force=True 时忽略缓存全部重编。"""
    git_url: str | None = None
    branch: str | None = None
    force: bool = False


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("/sync")
async def sync(
    req: SyncRequest | None = None,
    user: dict = Depends(get_current_user),
):
    """LLM 编译 wiki（SSE 流）。任一登录用户都可触发，结果是全局共享知识库。"""
    git_url = req.git_url if req else None
    branch = req.branch if req else None
    force = req.force if req else False

    async def event_generator():
        async for ev in service.sync_stream(_LLM, git_url, branch, force):
            yield _sse(ev)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/status")
async def status(user: dict = Depends(get_current_user)):
    """返回 wiki 状态（含页清单）；未编译时 indexed=false、meta=null。"""
    return await asyncio.to_thread(store.get_status)


@router.get("/index")
async def index(user: dict = Depends(get_current_user)):
    """返回索引页 markdown + 页清单。"""
    if not await asyncio.to_thread(store.is_indexed):
        raise HTTPException(status_code=404, detail="Wiki not compiled yet")
    return {
        "index": await asyncio.to_thread(store.read_index) or "",
        "pages": await asyncio.to_thread(store.list_pages),
    }


@router.get("/page")
async def page(
    slug: str = Query(..., min_length=1),
    user: dict = Depends(get_current_user),
):
    """按 slug 读某页。"""
    pg = await asyncio.to_thread(store.read_page, slug)
    if pg is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return pg


@router.get("/search")
async def search(
    q: str = Query(..., min_length=1),
    top_k: int = Query(5, ge=1, le=20),
    user: dict = Depends(get_current_user),
):
    """调试用检索端点（agent 实际走 search_wiki 工具）。"""
    results = await asyncio.to_thread(store.search, q, top_k)
    return {"query": q, "results": results}
