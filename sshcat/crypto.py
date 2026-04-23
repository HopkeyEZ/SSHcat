"""Password encryption/decryption for connection history.

Uses Fernet (AES-128-CBC) with a machine-specific key derived via PBKDF2,
so encrypted passwords are tied to the current OS user + machine.
"""

import base64
import hashlib
import os
import platform
import getpass

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


def _derive_key() -> bytes:
    """从机器特征（用户名 + 主机名）派生 Fernet 密钥。

    同一台机器、同一个 OS 用户始终生成相同的密钥，
    换机器或换用户后历史密码将无法解密（安全特性）。
    """
    identity = f"{getpass.getuser()}@{platform.node()}".encode("utf-8")
    salt = hashlib.sha256(identity).digest()[:16]
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480_000,
    )
    raw_key = kdf.derive(identity)
    return base64.urlsafe_b64encode(raw_key)


# 模块级缓存，避免每次调用都重新派生
_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_derive_key())
    return _fernet


def encrypt_password(plain: str) -> str:
    """加密密码，返回 base64 编码的密文字符串。"""
    if not plain:
        return ""
    token = _get_fernet().encrypt(plain.encode("utf-8"))
    return token.decode("ascii")


def decrypt_password(cipher: str) -> str:
    """解密密码，返回明文。解密失败返回空字符串。"""
    if not cipher:
        return ""
    try:
        plain = _get_fernet().decrypt(cipher.encode("ascii"))
        return plain.decode("utf-8")
    except (InvalidToken, Exception):
        return ""
