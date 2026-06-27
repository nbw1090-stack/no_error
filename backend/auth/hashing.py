"""
密码哈希（纯标准库）

使用 PBKDF2-HMAC-SHA256，迭代 100000 次，盐由 secrets 生成。
不引入 bcrypt / argon2 等第三方依赖，保持后端零额外编译要求。

约定：
- hash_password 返回 (hash_hex, salt_hex)，两者均为十六进制字符串，便于存 SQLite。
- verify_password 接收原始口令 + 之前存储的 hash/salt，恒定时间比较。
"""

import hashlib
import hmac
import secrets

# PBKDF2 迭代次数（OWASP 2023 建议 ≥ 600000，这里取 100000 兼顾安全与性能）
_ITERATIONS = 100000
_DIGEST = "sha256"
_SALT_BYTES = 16


def hash_password(password: str) -> tuple[str, str]:
    """
    对口令加盐哈希。

    Returns:
        (hash_hex, salt_hex)：均为 hex 字符串。
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(_DIGEST, password.encode("utf-8"), salt, _ITERATIONS)
    return dk.hex(), salt.hex()


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    """
    校验口令。用同样的 salt 重新派生，与存储的 hash 恒定时间比较。

    salt / hash 非法时返回 False（不抛异常），避免登录路径泄漏内部错误。
    """
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac(_DIGEST, password.encode("utf-8"), salt, _ITERATIONS)
    return hmac.compare_digest(dk, expected)
