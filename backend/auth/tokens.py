"""
Bearer Token 存储

令牌存于 users.db 的 auth_tokens 表（与 users.py 共用同一 DB 文件）。
set_db_path 与 users.set_db_path 指向同一文件即可（main.py 在两个模块都设一次）。

API：
- issue_token(user_id) -> str：签发 token 并落库，返回明文 token（仅此一次可见）
- resolve_token(token) -> user dict | None：凭 token 解析出 user（JOIN users）
- revoke_token(token) -> bool：吊销 token，返回是否确实删除了一行
"""

import secrets
import sqlite3
from datetime import datetime, timezone

from auth import users as _users  # 复用 USERS_DB_PATH


def set_db_path(path: str) -> None:
    """
    重定向令牌 DB 路径。

    令牌与用户共用同一 DB 文件，因此直接委托给 users 模块。
    保留此函数以满足「每个模块暴露 set_db_path」的约定。
    """
    _users.set_db_path(path)


def issue_token(user_id: int) -> str:
    """
    为 user_id 签发新令牌并落库。

    Returns:
        明文 token（secrets.token_urlsafe(32)），仅此一次返回。
    """
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(_users.USERS_DB_PATH)
    try:
        conn.execute(
            "INSERT INTO auth_tokens (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, user_id, now),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def resolve_token(token: str) -> dict | None:
    """
    凭 token 解析出对应用户。

    Returns:
        {id, username, created_at} 或 None（token 不存在/已吊销）。
    """
    if not token:
        return None
    conn = sqlite3.connect(_users.USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT u.id AS id, u.username AS username, u.created_at AS created_at "
            "FROM auth_tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.token = ?",
            (token,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "created_at": row["created_at"],
        }
    finally:
        conn.close()


def revoke_token(token: str) -> bool:
    """
    吊销 token。返回是否确实删除了一行（False 表示 token 本就不存在）。
    """
    if not token:
        return False
    conn = sqlite3.connect(_users.USERS_DB_PATH)
    try:
        cur = conn.execute("DELETE FROM auth_tokens WHERE token = ?", (token,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_token_for_user(user_id: int) -> str | None:
    """
    工具函数：取该用户最近一个 token（供 /logout 通过 user 查找当前 token）。
    多设备登录时只吊销最近一条；如需精确吊销请前端在 logout 时仍带 Authorization。
    """
    conn = sqlite3.connect(_users.USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT token FROM auth_tokens WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        )
        row = cur.fetchone()
        return row["token"] if row else None
    finally:
        conn.close()
