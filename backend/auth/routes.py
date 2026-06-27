"""
认证 API 路由（前缀 /api/auth，tags=["auth"]）

端点：
- POST /register  注册：201 {user_id, username, token}；冲突 409；参数不合法 400
- POST /login     登录：200 {user_id, username, token}；凭据错误 401
- GET  /me        当前用户：200 {user_id, username}；未认证 401
- POST /logout    登出：200 {ok: true}（吊销当前 token）
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.dependency import get_current_user
from auth.tokens import issue_token, revoke_token
from auth.users import create_user, verify_credentials

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 用户名 / 口令最小长度
_MIN_LEN = 3


class AuthRequest(BaseModel):
    """注册 / 登录共用请求体。"""
    username: str
    password: str


def _validate_credentials(username: str, password: str) -> None:
    """参数基础校验：非空 + 最小长度。不合法 → 400。"""
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")
    if len(username) < _MIN_LEN or len(password) < _MIN_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"username and password must be at least {_MIN_LEN} characters",
        )


@router.post("/register", status_code=201)
async def register(req: AuthRequest):
    """
    注册新用户并直接签发登录 token（省去注册后立即登录的往返）。

    同时为该新用户播种默认组件列表（enabled=0），使其注册后即可在
    组件管理页看到初始可分析的组件。播种失败不阻断注册（best-effort）。
    """
    _validate_credentials(req.username, req.password)
    try:
        user = create_user(req.username, req.password)
    except ValueError:
        # username 已存在
        raise HTTPException(status_code=409, detail="username already exists")

    # 惰性导入避免 auth → db 的循环依赖；best-effort，失败不阻断注册
    try:
        import db as component_db

        component_db.ensure_seeded(user["id"])
    except Exception:
        pass

    token = issue_token(user["id"])
    return {"user_id": user["id"], "username": user["username"], "token": token}


@router.post("/login")
async def login(req: AuthRequest):
    """凭据校验通过 → 签发 token。"""
    _validate_credentials(req.username, req.password)
    user = verify_credentials(req.username, req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = issue_token(user["id"])
    return {"user_id": user["id"], "username": user["username"], "token": token}


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    """返回当前登录用户（不含敏感字段）。"""
    return {"user_id": user["id"], "username": user["username"]}


@router.post("/logout")
async def logout(user: dict = Depends(get_current_user)):
    """吊销当前请求携带的 token。"""
    token = user.get("_token")
    if token:
        revoke_token(token)
    return {"ok": True}
