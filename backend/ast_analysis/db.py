"""
AST 分析结果 SQLite 存储（ast.db）

表（均以 user_id 为第一维度，实现用户隔离）：
- ast_components：(user_id, component) 为主键，存每个组件的 git 元信息 + 聚合计数。
- ast_files：每行一个被解析的源文件，symbols / node_types 以 JSON 文本存储。
- ast_meta：用户级别的「最近分析时间」单行记录。

设计要点（与 db.py 对齐）：
- 纯标准库 sqlite3，模块级 AST_DB_PATH 在导入时惰性按 config.data_dir 推导。
- 测试可通过 set_db_path() 重定向到 tmp_path。
- 每个函数各自打开连接、提交、关闭。
- replace_component 在单事务内 DELETE+INSERT，保证增量替换的原子性。
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

# 模块级可变路径（导入时按 config.data_dir 推导默认值）
AST_DB_PATH = ""


def _default_db_path() -> str:
    """惰性导入 config 推导默认 DB 路径，保持导入顺序干净。"""
    from config import AppConfig

    return os.path.join(AppConfig.from_env().data_dir, "ast.db")


AST_DB_PATH = _default_db_path()


def set_db_path(path: str) -> None:
    """重定向 AST DB 路径（测试用）。"""
    global AST_DB_PATH
    AST_DB_PATH = path


# 源码快照根目录：clone 的源码树保留于此（路径 <user_id>/<component>/），
# 供 get_function_source 按行号区间切出函数体。与 ast.db 同生命周期增删。
SOURCE_DIR = ""


def _default_source_dir() -> str:
    """惰性导入 config 推导默认源码快照根目录。"""
    from config import AppConfig

    return os.path.join(AppConfig.from_env().data_dir, "source")


SOURCE_DIR = _default_source_dir()


def set_source_dir(path: str) -> None:
    """重定向源码快照根目录（测试用）。"""
    global SOURCE_DIR
    SOURCE_DIR = path


# ============================================================
# 初始化
# ============================================================
def init_ast_db() -> None:
    """幂等建表 + 索引。"""
    conn = sqlite3.connect(AST_DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ast_components (
                user_id       INTEGER NOT NULL,
                component     TEXT NOT NULL,
                git_url       TEXT NOT NULL,
                branch        TEXT NOT NULL,
                commit_sha    TEXT NOT NULL,
                analyzed_at   TEXT NOT NULL,
                file_count    INTEGER NOT NULL DEFAULT 0,
                symbol_count  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, component)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ast_files (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                component     TEXT NOT NULL,
                rel_path      TEXT NOT NULL,
                language      TEXT NOT NULL,
                symbol_count  INTEGER NOT NULL,
                symbols       TEXT NOT NULL,
                node_types    TEXT NOT NULL,
                analyzed_at   TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ast_files_uc "
            "ON ast_files(user_id, component)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ast_meta (
                user_id          INTEGER PRIMARY KEY,
                last_analyzed_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# 查询
# ============================================================
def list_user_components(user_id: int) -> list[dict]:
    """
    列出某用户已分析的全部组件（= S_old）。

    Returns:
        [{component, git_url, branch, commit_sha, analyzed_at, file_count, symbol_count}]
    """
    conn = sqlite3.connect(AST_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT component, git_url, branch, commit_sha, analyzed_at, "
            "file_count, symbol_count FROM ast_components WHERE user_id = ? "
            "ORDER BY component",
            (user_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_component_commit(user_id: int, component: str) -> str | None:
    """取某组件已存储的 commit sha（用于增量比对），不存在返回 None。"""
    conn = sqlite3.connect(AST_DB_PATH)
    try:
        cur = conn.execute(
            "SELECT commit_sha FROM ast_components WHERE user_id = ? AND component = ?",
            (user_id, component),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _stored_counts(user_id: int, component: str) -> tuple[int, int]:
    """取已存储的 (file_count, symbol_count)，供 unchanged 事件回填。"""
    conn = sqlite3.connect(AST_DB_PATH)
    try:
        cur = conn.execute(
            "SELECT file_count, symbol_count FROM ast_components "
            "WHERE user_id = ? AND component = ?",
            (user_id, component),
        )
        row = cur.fetchone()
        return (row[0], row[1]) if row else (0, 0)
    finally:
        conn.close()


def find_function_at_line(
    user_id: int, component: str, file_basename: str, line: int
) -> list[dict]:
    """
    按 (user_id, component, basename(file), line) 定位包含该行的符号。

    日志的 file 是裸文件名（如 pcie_card.lua），而 ast_files.rel_path 是相对
    仓库根的路径（可能带子目录），故用 basename 匹配；再在匹配文件内找
    start_line ≤ line ≤ end_line 的符号。

    Returns:
        [{rel_path, name, kind, start_line, end_line}, ...] —— 同 basename 不同
        子目录的多个文件都可能命中，调用方按需取舍。
    """
    conn = sqlite3.connect(AST_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT rel_path, symbols FROM ast_files "
            "WHERE user_id = ? AND component = ?",
            (user_id, component),
        )
        hits: list[dict] = []
        for r in cur.fetchall():
            rel_path = r["rel_path"]
            if os.path.basename(rel_path) != file_basename:
                continue
            try:
                symbols = json.loads(r["symbols"] or "[]")
            except json.JSONDecodeError:
                symbols = []
            for sym in symbols:
                start = int(sym.get("start_line", 0))
                end = int(sym.get("end_line", 0))
                if start and end and start <= line <= end:
                    hits.append(
                        {
                            "rel_path": rel_path,
                            "name": sym.get("name", ""),
                            "kind": sym.get("kind", ""),
                            "start_line": start,
                            "end_line": end,
                        }
                    )
        return hits
    finally:
        conn.close()


def find_symbols_by_name(
    user_id: int, component: str, name: str, limit: int = 20
) -> list[dict]:
    """
    按 (user_id, component, name) 模糊匹配符号（大小写不敏感子串匹配）。

    用于 agent 主动按函数/类名检索源码，区别于 find_function_at_line
    的按日志行号反查：用户问"xxx 函数是做什么的"时命中这里。

    Returns:
        [{rel_path, name, kind, start_line, end_line}, ...] —— 跨文件的全部命中，
        最多 limit 条。
    """
    needle = (name or "").lower()
    conn = sqlite3.connect(AST_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT rel_path, symbols FROM ast_files "
            "WHERE user_id = ? AND component = ?",
            (user_id, component),
        )
        hits: list[dict] = []
        for r in cur.fetchall():
            rel_path = r["rel_path"]
            try:
                symbols = json.loads(r["symbols"] or "[]")
            except json.JSONDecodeError:
                symbols = []
            for sym in symbols:
                sym_name = str(sym.get("name", ""))
                if needle and needle in sym_name.lower():
                    hits.append(
                        {
                            "rel_path": rel_path,
                            "name": sym_name,
                            "kind": sym.get("kind", ""),
                            "start_line": int(sym.get("start_line", 0)),
                            "end_line": int(sym.get("end_line", 0)),
                        }
                    )
                    if len(hits) >= limit:
                        return hits
        return hits
    finally:
        conn.close()


def list_component_files(user_id: int, component: str) -> list[dict]:
    """
    列出某组件下所有已索引的源码文件，附带每个文件的符号清单。

    供 agent 浏览组件代码结构后再决定读取哪个文件/符号。

    Returns:
        [{rel_path, language, symbol_count,
          symbols:[{name, kind, start_line, end_line}, ...]}, ...]
    """
    conn = sqlite3.connect(AST_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT rel_path, language, symbol_count, symbols FROM ast_files "
            "WHERE user_id = ? AND component = ? ORDER BY rel_path",
            (user_id, component),
        )
        files: list[dict] = []
        for r in cur.fetchall():
            try:
                symbols = json.loads(r["symbols"] or "[]")
            except json.JSONDecodeError:
                symbols = []
            files.append(
                {
                    "rel_path": r["rel_path"],
                    "language": r["language"],
                    "symbol_count": r["symbol_count"],
                    "symbols": [
                        {
                            "name": s.get("name", ""),
                            "kind": s.get("kind", ""),
                            "start_line": int(s.get("start_line", 0)),
                            "end_line": int(s.get("end_line", 0)),
                        }
                        for s in symbols
                    ],
                }
            )
        return files
    finally:
        conn.close()


def get_file_symbols(
    user_id: int, component: str, rel_path: str
) -> dict | None:
    """
    取某组件单个源文件的符号清单（单行精确查询，避免 list_component_files
    把整个组件所有文件符号全部载入）。

    供 gather_code_context 取「同文件兄弟符号」之用。

    Returns:
        {rel_path, language, symbol_count,
         symbols:[{name, kind, start_line, end_line}, ...]}
        rel_path 不存在时返回 None。
    """
    conn = sqlite3.connect(AST_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT rel_path, language, symbol_count, symbols FROM ast_files "
            "WHERE user_id = ? AND component = ? AND rel_path = ?",
            (user_id, component, rel_path),
        )
        r = cur.fetchone()
        if r is None:
            return None
        try:
            symbols = json.loads(r["symbols"] or "[]")
        except json.JSONDecodeError:
            symbols = []
        return {
            "rel_path": r["rel_path"],
            "language": r["language"],
            "symbol_count": r["symbol_count"],
            "symbols": [
                {
                    "name": s.get("name", ""),
                    "kind": s.get("kind", ""),
                    "start_line": int(s.get("start_line", 0)),
                    "end_line": int(s.get("end_line", 0)),
                }
                for s in symbols
            ],
        }
    finally:
        conn.close()


# ============================================================
# 写入（单事务原子）
# ============================================================
def replace_component(
    user_id: int,
    component: str,
    git_url: str,
    branch: str,
    commit_sha: str,
    analyzed_at: str,
    files: list[dict],
) -> None:
    """
    原子替换某组件的全部数据：删旧 files → upsert ast_components → 插入新 files。

    files 元素形状（由 analyzer 产出）：
        {rel_path, language, symbol_count, symbols:[...], node_types:{...}}
    """
    file_count = len(files)
    symbol_count = sum(int(f.get("symbol_count", 0)) for f in files)

    conn = sqlite3.connect(AST_DB_PATH)
    try:
        # 显式开启事务：DELETE + INSERT 必须原子
        conn.execute("BEGIN")
        # 1) 删旧文件行
        conn.execute(
            "DELETE FROM ast_files WHERE user_id = ? AND component = ?",
            (user_id, component),
        )
        # 2) upsert 组件元信息
        conn.execute(
            "DELETE FROM ast_components WHERE user_id = ? AND component = ?",
            (user_id, component),
        )
        conn.execute(
            "INSERT INTO ast_components "
            "(user_id, component, git_url, branch, commit_sha, analyzed_at, "
            " file_count, symbol_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, component, git_url, branch, commit_sha, analyzed_at,
                file_count, symbol_count,
            ),
        )
        # 3) 插入新文件行
        for f in files:
            conn.execute(
                "INSERT INTO ast_files "
                "(user_id, component, rel_path, language, symbol_count, "
                " symbols, node_types, analyzed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id, component, f["rel_path"], f["language"],
                    int(f.get("symbol_count", 0)),
                    json.dumps(f.get("symbols", []), ensure_ascii=False),
                    json.dumps(f.get("node_types", {}), ensure_ascii=False),
                    analyzed_at,
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def delete_component(user_id: int, component: str) -> bool:
    """
    删除某组件的全部数据（files + ast_components 行）。

    返回是否确实删除了组件行（False 表示本就不存在）。
    """
    conn = sqlite3.connect(AST_DB_PATH)
    try:
        conn.execute("BEGIN")
        conn.execute(
            "DELETE FROM ast_files WHERE user_id = ? AND component = ?",
            (user_id, component),
        )
        cur = conn.execute(
            "DELETE FROM ast_components WHERE user_id = ? AND component = ?",
            (user_id, component),
        )
        conn.execute("COMMIT")
        return cur.rowcount > 0
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def set_last_analyzed(user_id: int, analyzed_at: str) -> None:
    """upsert 用户的「最近分析时间」。"""
    conn = sqlite3.connect(AST_DB_PATH)
    try:
        conn.execute(
            "INSERT INTO ast_meta (user_id, last_analyzed_at) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_analyzed_at = excluded.last_analyzed_at",
            (user_id, analyzed_at),
        )
        conn.commit()
    finally:
        conn.close()


# ============================================================
# 聚合结果（供 GET /api/ast/result）
# ============================================================
def get_result(user_id: int) -> dict | None:
    """
    汇总某用户的全部 AST 分析结果。

    形状：
        {
          "user_id": int,
          "last_analyzed_at": str | None,
          "components": [
            {"component","git_url","branch","commit_sha"(short 8),"analyzed_at",
             "file_count","symbol_count"}, ...
          ],
          "stats": {
            "components": int, "files": int, "symbols": int,
            "by_language": {"c": N, "cpp": N, "lua": N, ...}
          }
        }
    无任何记录时返回 None（→ 路由回 404）。
    """
    conn = sqlite3.connect(AST_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT component, git_url, branch, commit_sha, analyzed_at, "
            "file_count, symbol_count FROM ast_components WHERE user_id = ? "
            "ORDER BY component",
            (user_id,),
        )
        components = []
        for r in cur.fetchall():
            sha = r["commit_sha"] or ""
            components.append(
                {
                    "component": r["component"],
                    "git_url": r["git_url"],
                    "branch": r["branch"],
                    "commit_sha": sha[:8],
                    "analyzed_at": r["analyzed_at"],
                    "file_count": r["file_count"],
                    "symbol_count": r["symbol_count"],
                }
            )

        if not components:
            return None

        # 聚合 files / symbols / by_language
        cur2 = conn.execute(
            "SELECT language, COUNT(*) AS files, SUM(symbol_count) AS symbols "
            "FROM ast_files WHERE user_id = ? GROUP BY language",
            (user_id,),
        )
        by_language: dict[str, int] = {}
        files_total = 0
        symbols_total = 0
        for r in cur2.fetchall():
            by_language[r["language"]] = r["files"]
            files_total += r["files"]
            symbols_total += r["symbols"] or 0

        # 最近分析时间
        cur3 = conn.execute(
            "SELECT last_analyzed_at FROM ast_meta WHERE user_id = ?",
            (user_id,),
        )
        row = cur3.fetchone()
        last_at = row["last_analyzed_at"] if row else None

        return {
            "user_id": user_id,
            "last_analyzed_at": last_at,
            "components": components,
            "stats": {
                "components": len(components),
                "files": files_total,
                "symbols": symbols_total,
                "by_language": by_language,
            },
        }
    finally:
        conn.close()
