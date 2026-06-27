"""
AST 分析 API 路由（前缀 /api/ast，tags=["ast"]）

端点：
- POST   /analyze            增量分析（SSE 流）：plan → progress* → component_result* → summary → done
- DELETE /components/{name}   删除某组件的 AST 分析结果，并把注册表中该组件置为不可用
- GET    /result             当前用户的聚合分析结果（无则 404）

认证：依赖 auth.dependency.get_current_user（Bearer token）。
"""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import db as component_db
from ast_analysis import db, service
from auth.dependency import get_current_user

router = APIRouter(prefix="/api/ast", tags=["ast"])


class AnalyzeRequest(BaseModel):
    """增量分析请求体。"""
    component_names: list[str]


def _sse(event: dict) -> str:
    """事件 dict → SSE data 行。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("/analyze")
async def analyze(
    req: AnalyzeRequest,
    user: dict = Depends(get_current_user),
):
    """
    增量 AST 分析（SSE 流）。

    必须在进入流之前校验（流内抛 4xx 会被 TestClient / 客户端当作普通响应体）。
    """
    if not req.component_names:
        raise HTTPException(status_code=400, detail="component_names must be non-empty")

    user_id = user["id"]

    async def event_generator():
        async for ev in service.analyze_stream(user_id, req.component_names):
            yield _sse(ev)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.delete("/components/{name}")
async def delete_component_analysis(
    name: str, user: dict = Depends(get_current_user)
):
    """
    删除当前用户对某组件的 AST 分析结果。

    组件本身**不**从注册表中删除，而是把它的 enabled 置为 False（不可用），
    表示「暂未被 AST 分析」。这样前端列表里仍能看到该组件，只是标记为待分析。

    组件不在该用户注册表中 → 404；该用户本就无此组件的分析结果 → 视作成功（幂等）。
    """
    user_id = user["id"]
    # 1) 组件必须在当前用户的注册表中
    component = await asyncio.to_thread(component_db.get_component, user_id, name)
    if component is None:
        raise HTTPException(status_code=404, detail="Component not found")
    # 2) 删除该用户的 AST 分析数据（files + ast_components 行）；不存在则无害
    await asyncio.to_thread(db.delete_component, user_id, name)
    # 3) 注册表置为不可用（尚未分析）
    await asyncio.to_thread(component_db.set_enabled, user_id, name, False)
    component = await asyncio.to_thread(component_db.get_component, user_id, name)
    return {"deleted": True, "component": component}


@router.get("/result")
async def result(user: dict = Depends(get_current_user)):
    """返回当前用户的聚合分析结果；无任何记录 → 404。"""
    data = await asyncio.to_thread(db.get_result, user["id"])
    if data is None:
        raise HTTPException(status_code=404, detail="No AST analysis result yet")
    return data
