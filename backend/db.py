"""
SQLite 组件注册表

存储 BMC 组件元数据（name / git_url / branch / enabled），供组件管理 API 使用。

设计要点：
- 纯标准库 sqlite3，不引入第三方依赖（无 aiosqlite）。
- 模块级全局 COMPONENTS_DB_PATH 在导入时按 config.data_dir 推导默认值；
  测试可通过 set_db_path() 重定向到 tmp_path。
- 每个 CRUD 函数各自打开连接、提交、关闭，互不持有长期连接。
- enabled 在数据库里以 INTEGER(0/1) 存储，在 Python 侧统一以 bool 暴露。
"""

import os
import sqlite3

# 模块级可变路径（导入时按 config.data_dir 推导默认值）
COMPONENTS_DB_PATH = ""


def _default_db_path() -> str:
    """惰性导入 config 推导默认 DB 路径，保持导入顺序干净。"""
    from config import AppConfig

    return os.path.join(AppConfig.from_env().data_dir, "components.db")


COMPONENTS_DB_PATH = _default_db_path()


def set_db_path(path: str) -> None:
    """重定向组件 DB 路径（测试用）。"""
    global COMPONENTS_DB_PATH
    COMPONENTS_DB_PATH = path


# ============================================================
# 首次启动时播种的样本组件
# ============================================================
_SEED_COMPONENTS = [
    ("pcie_device", "https://github.com/bmc-org/pcie_device.git", "main", 1),
    ("sensor", "https://github.com/bmc-org/sensor.git", "main", 1),
    ("hwproxy", "https://github.com/bmc-org/hwproxy.git", "main", 1),
    ("framework", "https://github.com/bmc-org/framework.git", "main", 1),
]


# ============================================================
# 初始化
# ============================================================
def init_db() -> None:
    """
    幂等初始化：建表；表为空时播种样本组件。

    幂等性：CREATE TABLE IF NOT EXISTS + 仅在 COUNT(*)==0 时插入。
    """
    conn = sqlite3.connect(COMPONENTS_DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS components (
                name     TEXT PRIMARY KEY,
                git_url  TEXT NOT NULL DEFAULT '',
                branch   TEXT NOT NULL DEFAULT '',
                enabled  INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.commit()

        cur = conn.execute("SELECT COUNT(*) FROM components")
        (count,) = cur.fetchone()
        if count == 0:
            conn.executemany(
                "INSERT INTO components (name, git_url, branch, enabled) "
                "VALUES (?, ?, ?, ?)",
                _SEED_COMPONENTS,
            )
            conn.commit()
    finally:
        conn.close()


# ============================================================
# 行 ↔ dict 转换（enabled int↔bool 边界转换）
# ============================================================
def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "name": row["name"],
        "git_url": row["git_url"],
        "branch": row["branch"],
        "enabled": bool(row["enabled"]),
    }


# ============================================================
# CRUD
# ============================================================
def list_components() -> list[dict]:
    """列出全部组件。"""
    conn = sqlite3.connect(COMPONENTS_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT name, git_url, branch, enabled FROM components "
            "ORDER BY name"
        )
        return [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def create_component(name: str, git_url: str, branch: str, enabled: bool) -> dict:
    """
    新建组件。主键冲突 → ValueError("exists")。
    """
    conn = sqlite3.connect(COMPONENTS_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        try:
            conn.execute(
                "INSERT INTO components (name, git_url, branch, enabled) "
                "VALUES (?, ?, ?, ?)",
                (name, git_url, branch, 1 if enabled else 0),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError("exists")
        cur = conn.execute(
            "SELECT name, git_url, branch, enabled FROM components WHERE name = ?",
            (name,),
        )
        row = cur.fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def update_component(
    name: str, git_url: str, branch: str, enabled: bool
) -> dict:
    """
    更新组件。行不存在 → KeyError(name)。
    """
    conn = sqlite3.connect(COMPONENTS_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "UPDATE components SET git_url = ?, branch = ?, enabled = ? "
            "WHERE name = ?",
            (git_url, branch, 1 if enabled else 0, name),
        )
        if cur.rowcount == 0:
            raise KeyError(name)
        conn.commit()
        cur = conn.execute(
            "SELECT name, git_url, branch, enabled FROM components WHERE name = ?",
            (name,),
        )
        row = cur.fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def delete_component(name: str) -> bool:
    """
    删除组件。行不存在 → KeyError(name)。
    """
    conn = sqlite3.connect(COMPONENTS_DB_PATH)
    try:
        cur = conn.execute("DELETE FROM components WHERE name = ?", (name,))
        if cur.rowcount == 0:
            raise KeyError(name)
        conn.commit()
        return True
    finally:
        conn.close()


def toggle_component(name: str) -> dict:
    """
    翻转 enabled。行不存在 → KeyError(name)。
    """
    conn = sqlite3.connect(COMPONENTS_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "UPDATE components SET enabled = (1 - enabled) WHERE name = ?",
            (name,),
        )
        if cur.rowcount == 0:
            raise KeyError(name)
        conn.commit()
        cur = conn.execute(
            "SELECT name, git_url, branch, enabled FROM components WHERE name = ?",
            (name,),
        )
        row = cur.fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()
