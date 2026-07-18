import hashlib
import hmac
import os
from typing import Tuple


PBKDF2_ITERATIONS = 260_000


def validate_password(password: str) -> None:
    if len(password or "") < 6:
        raise ValueError("密码至少需要 6 位")
    if password.isspace():
        raise ValueError("密码不能全部为空白字符")


def hash_password(password: str, salt: bytes = None) -> Tuple[str, str]:
    validate_password(password)
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, digest_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return hmac.compare_digest(actual, expected)
