"""
AST 分析模块

面向已登录用户做**增量**的 git 源码 AST 解析与持久化：
- downloader：git ls-remote / git clone（subprocess）
- analyzer：tree-sitter 解析 C / C++ / Lua
- db：用户维度的 ast.db（组件 → 文件 → 符号），支持增量替换 / 删除
- service：SSE 编排器，实现「未变更跳过 / 已变更更新 / 新选新增 / 取消移除」
- routes：/api/ast/analyze（SSE）+ /api/ast/result

模块导出：
- init_ast_db / set_db_path / set_source_dir：DB 与源码快照目录的初始化、路径重定向
- router：FastAPI APIRouter（前缀 /api/ast）
"""

from ast_analysis.db import init_ast_db, set_db_path, set_source_dir
from ast_analysis.routes import router

__all__ = ["init_ast_db", "set_db_path", "set_source_dir", "router"]
