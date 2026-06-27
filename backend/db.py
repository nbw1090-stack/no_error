"""
SQLite 组件注册表（按用户隔离）

每个用户拥有独立的组件注册表，存储 BMC 组件元数据
（name / git_url / branch / enabled）。表的主键为 (user_id, name)。

enabled 的语义：「该组件是否已被该用户完成 AST 语法分析」。
- 不再是手动开关；由 AST 分析流程（ast_analysis/service.py）在每次
  分析变动时经 set_enabled() 写回维护。
- 新建组件 enabled=0；编辑改动 git_url/branch（源码变了，分析失效）时
  自动重置为 0。
- 前端「是否可用」列直接读 enabled，无需 join ast.db。

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
# 新用户首次接触组件注册表时播种的样本组件
# ============================================================
_SEED_COMPONENTS = [
    ("pcie_device", "https://github.com/bmc-org/pcie_device.git", "main"),
    ("sensor", "https://github.com/bmc-org/sensor.git", "main"),
    ("hwproxy", "https://github.com/bmc-org/hwproxy.git", "main"),
    ("framework", "https://github.com/bmc-org/framework.git", "main"),
]


# ============================================================
# 初始化
# ============================================================
def init_db() -> None:
    """
    幂等建表（按用户隔离的复合主键）。

    不再在此全局播种——播种改为按用户在 ensure_seeded(user_id) /
    注册流程中触发，使每个用户获得自己的初始组件列表。
    """
    conn = sqlite3.connect(COMPONENTS_DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS components (
                user_id  INTEGER NOT NULL,
                name     TEXT NOT NULL,
                git_url  TEXT NOT NULL DEFAULT '',
                branch   TEXT NOT NULL DEFAULT '',
                enabled  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, name)
            )
            """
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
# 播种（按用户）
# ============================================================
def ensure_seeded(user_id: int) -> None:
    """
    幂等播种：仅当该用户当前 0 个组件时插入 4 个默认组件（enabled=0）。

    供注册流程调用，使每个新用户都有初始可分析的组件列表。
    """
    conn = sqlite3.connect(COMPONENTS_DB_PATH)
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM components WHERE user_id = ?", (user_id,)
        )
        (count,) = cur.fetchone()
        if count > 0:
            return
        conn.executemany(
            "INSERT INTO components (user_id, name, git_url, branch, enabled) "
            "VALUES (?, ?, ?, ?, 0)",
            [(user_id, n, u, b) for (n, u, b) in _SEED_COMPONENTS],
        )
        conn.commit()
    finally:
        conn.close()


# ============================================================
# CRUD（全部以 user_id 为第一维度）
# ============================================================
def list_components(user_id: int) -> list[dict]:
    """列出某用户的全部组件。"""
    conn = sqlite3.connect(COMPONENTS_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT name, git_url, branch, enabled FROM components "
            "WHERE user_id = ? ORDER BY name",
            (user_id,),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_component(user_id: int, name: str) -> dict | None:
    """取某用户单个组件，不存在返回 None。"""
    conn = sqlite3.connect(COMPONENTS_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT name, git_url, branch, enabled FROM components "
            "WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        row = cur.fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def create_component(
    user_id: int, name: str, git_url: str, branch: str
) -> dict:
    """
    新建组件（enabled=0，尚未分析）。主键 (user_id, name) 冲突 → ValueError("exists")。
    """
    conn = sqlite3.connect(COMPONENTS_DB_PATH)
    try:
        try:
            conn.execute(
                "INSERT INTO components (user_id, name, git_url, branch, enabled) "
                "VALUES (?, ?, ?, ?, 0)",
                (user_id, name, git_url, branch),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError("exists")
    finally:
        conn.close()
    return get_component(user_id, name)  # type: ignore[return-value]


def update_component(
    user_id: int, name: str, git_url: str, branch: str
) -> dict:
    """
    更新组件的 git_url / branch。行不存在 → KeyError(name)。

    若 git_url 或 branch 实际发生变更（源码变了，既有 AST 分析失效），
    则把 enabled 重置为 0；否则保留原 enabled。
    """
    existing = get_component(user_id, name)
    if existing is None:
        raise KeyError(name)
    source_changed = (
        existing["git_url"] != git_url or existing["branch"] != branch
    )
    new_enabled = 0 if source_changed else (1 if existing["enabled"] else 0)
    conn = sqlite3.connect(COMPONENTS_DB_PATH)
    try:
        conn.execute(
            "UPDATE components SET git_url = ?, branch = ?, enabled = ? "
            "WHERE user_id = ? AND name = ?",
            (git_url, branch, new_enabled, user_id, name),
        )
        conn.commit()
    finally:
        conn.close()
    return get_component(user_id, name)  # type: ignore[return-value]


def delete_component(user_id: int, name: str) -> bool:
    """删除组件。行不存在 → KeyError(name)。"""
    conn = sqlite3.connect(COMPONENTS_DB_PATH)
    try:
        cur = conn.execute(
            "DELETE FROM components WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        if cur.rowcount == 0:
            raise KeyError(name)
        conn.commit()
        return True
    finally:
        conn.close()


def set_enabled(user_id: int, name: str, enabled: bool) -> None:
    """
    维护组件的 enabled（= 已分析）状态，由 AST 分析流程调用。

    组件不存在时静默忽略（例如该组件已被用户从注册表删除），不报错，
    避免分析流程因注册表与 AST 结果不一致而中断。
    """
    conn = sqlite3.connect(COMPONENTS_DB_PATH)
    try:
        conn.execute(
            "UPDATE components SET enabled = ? WHERE user_id = ? AND name = ?",
            (1 if enabled else 0, user_id, name),
        )
        conn.commit()
    finally:
        conn.close()
