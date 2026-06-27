"""
用户认证模块

提供基于 Bearer Token 的轻量级用户认证：
- 注册 / 登录 / 登出 / 获取当前用户
- 密码使用 PBKDF2-HMAC-SHA256 加盐哈希（纯标准库）
- 令牌持久化到 users.db 的 auth_tokens 表

模块导出：
- init_auth_db / set_db_path：DB 初始化与路径重定向（镜像 db.py 的模式）
- router：FastAPI APIRouter（前缀 /api/auth）
"""

from auth.users import init_auth_db, set_db_path
from auth.routes import router

__all__ = ["init_auth_db", "set_db_path", "router"]
