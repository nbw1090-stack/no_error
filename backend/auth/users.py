"""
用户 SQLite 存储（users.db）

表：
- users：id / username(UNIQUE) / password_hash / salt / created_at
- auth_tokens：token(PK) / user_id / created_at（由 tokens.py 复用同一 DB）

设计要点（与 db.py 对齐）：
- 纯标准库 sqlite3，模块级 USERS_DB_PATH 在导入时惰性按 config.data_dir 推导。
- 测试可通过 set_db_path() 重定向到 tmp_path。
- 每个 CRUD 函数各自打开连接、提交、关闭，互不持有长期连接。
"""

import os
import sqlite3
from datetime import datetime, timezone

from auth.hashing import hash_password, verify_password

# 模块级可变路径（导入时按 config.data_dir 推导默认值）
USERS_DB_PATH = ""


def _default_db_path() -> str:
    """惰性导入 config 推导默认 DB 路径，保持导入顺序干净。"""
    from config import AppConfig

    return os.path.join(AppConfig.from_env().data_dir, "users.db")


USERS_DB_PATH = _default_db_path()


def set_db_path(path: str) -> None:
    """重定向用户 DB 路径（测试用）。"""
    global USERS_DB_PATH
    USERS_DB_PATH = path


# ============================================================
# 初始化
# ============================================================
def init_auth_db() -> None:
    """
    幂等初始化：建 users + auth_tokens 表。

    tokens.py 复用同一 DB 文件，因此在此一并建表。
    """
    conn = sqlite3.connect(USERS_DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                salt          TEXT NOT NULL,
                created_at    TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_tokens (
                token      TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_user(row: sqlite3.Row) -> dict:
    """users 行 → 对外 dict（不含密码哈希）。"""
    return {
        "id": row["id"],
        "username": row["username"],
        "created_at": row["created_at"],
    }


# ============================================================
# CRUD
# ============================================================
def create_user(username: str, password: str) -> dict:
    """
    新建用户。username 冲突 → ValueError("exists")。

    Returns:
        {id, username}
    """
    pw_hash, salt = hash_password(password)
    now = _now_iso()
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        try:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, salt, created_at) "
                "VALUES (?, ?, ?, ?)",
                (username, pw_hash, salt, now),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError("exists")
        return {"id": cur.lastrowid, "username": username}
    finally:
        conn.close()


def get_user_by_username(username: str) -> dict | None:
    """
    按用户名查询（含 password_hash / salt，供登录校验）。

    Returns:
        {id, username, password_hash, salt, created_at} 或 None。
    """
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT id, username, password_hash, salt, created_at "
            "FROM users WHERE username = ?",
            (username,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "password_hash": row["password_hash"],
            "salt": row["salt"],
            "created_at": row["created_at"],
        }
    finally:
        conn.close()


def get_user(user_id: int) -> dict | None:
    """
    按主键查询（对外形状，不含哈希）。

    Returns:
        {id, username, created_at} 或 None。
    """
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT id, username, created_at FROM users WHERE id = ?",
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_user(row)
    finally:
        conn.close()


def verify_credentials(username: str, password: str) -> dict | None:
    """
    校验登录凭据。成功返回对外 user dict（{id, username, created_at}），失败 None。
    """
    record = get_user_by_username(username)
    if record is None:
        return None
    if not verify_password(
        password, record["password_hash"], record["salt"]
    ):
        return None
    return {
        "id": record["id"],
        "username": record["username"],
        "created_at": record["created_at"],
    }
