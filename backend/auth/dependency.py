"""
FastAPI 依赖：从 Authorization 头解析当前用户。

用法：
    @router.get("/me")
    async def me(user: dict = Depends(get_current_user)):
        return {"user_id": user["id"], "username": user["username"]}

请求头格式：Authorization: Bearer <token>
缺失或无效 → HTTPException(401, "Not authenticated")。
"""

from typing import Optional

from fastapi import Depends, Header, HTTPException

from auth.tokens import resolve_token


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    """从 'Bearer <token>' 头中取出 token；格式不对返回 None。"""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def get_current_user(
    authorization: Optional[str] = Header(default=None),
) -> dict:
    """
    解析 Bearer token，返回 {id, username, created_at}。
    缺失 / 格式错 / token 无效 → 401。
    """
    token = _extract_bearer(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = resolve_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # 附带 token，便于 /logout 路由直接吊销（避免再解析一次头）
    user["_token"] = token
    return user


# 便于显式 Depends(get_current_user) 的同时复用同一可调用对象
CurrentuserDep = Depends(get_current_user)
