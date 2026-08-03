from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import os
import shutil
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecureStorageUnavailable(RuntimeError):
    pass


class Protector:
    def protect(self, value: bytes) -> bytes:
        raise NotImplementedError

    def unprotect(self, value: bytes) -> bytes:
        raise NotImplementedError


if os.name == "nt":

    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]


class DpapiProtector(Protector):
    _ENTROPY = b"IntDemoClientOnlineTest/v0.2"

    def __init__(self):
        if os.name != "nt":
            raise SecureStorageUnavailable("DPAPI 仅在 Windows 上可用")
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32
        blob_pointer = ctypes.POINTER(_DataBlob)
        self._crypt32.CryptProtectData.argtypes = [
            blob_pointer,
            wintypes.LPCWSTR,
            blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            blob_pointer,
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            blob_pointer,
            ctypes.c_void_p,
            blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            blob_pointer,
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    @staticmethod
    def _blob(value: bytes):
        buffer = ctypes.create_string_buffer(value)
        blob = _DataBlob(
            len(value),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
        )
        return blob, buffer

    def protect(self, value: bytes) -> bytes:
        source, source_buffer = self._blob(value)
        entropy, entropy_buffer = self._blob(self._ENTROPY)
        output = _DataBlob()
        success = self._crypt32.CryptProtectData(
            ctypes.byref(source),
            "IntDemo online credentials",
            ctypes.byref(entropy),
            None,
            None,
            0x1,
            ctypes.byref(output),
        )
        del source_buffer, entropy_buffer
        if not success:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._kernel32.LocalFree(output.pbData)

    def unprotect(self, value: bytes) -> bytes:
        source, source_buffer = self._blob(value)
        entropy, entropy_buffer = self._blob(self._ENTROPY)
        output = _DataBlob()
        success = self._crypt32.CryptUnprotectData(
            ctypes.byref(source),
            None,
            ctypes.byref(entropy),
            None,
            None,
            0x1,
            ctypes.byref(output),
        )
        del source_buffer, entropy_buffer
        if not success:
            raise SecureStorageUnavailable("无法解密本机在线凭据")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._kernel32.LocalFree(output.pbData)


class SecretServiceProtector(Protector):
    """Encrypt credentials with a key held by the Linux Secret Service."""

    _PREFIX = b"INTDEMO-SECRET-SERVICE-1\0"
    _AAD = b"IntDemoClientOnlineTest/v0.2/linux"
    _ATTRIBUTES = (
        "application",
        "intdemo-client",
        "purpose",
        "credential-encryption-v1",
    )

    def __init__(self, command=None, runner=None):
        if not sys.platform.startswith("linux"):
            raise SecureStorageUnavailable("Secret Service 仅在 Linux 上使用")
        self.command = command or shutil.which("secret-tool")
        if not self.command:
            raise SecureStorageUnavailable(
                "缺少 secret-tool；请安装 libsecret-tools 后重新启动程序"
            )
        self._runner = runner or subprocess.run
        self._key = self._load_or_create_key()

    def _run(self, arguments, *, input_text=None):
        try:
            return self._runner(
                [self.command, *arguments],
                input=input_text,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecureStorageUnavailable(
                f"无法访问 Linux Secret Service：{exc}"
            ) from exc

    @staticmethod
    def _decode_key(value):
        try:
            key = base64.b64decode(str(value or "").strip(), validate=True)
        except (ValueError, TypeError) as exc:
            raise SecureStorageUnavailable(
                "Linux Secret Service 中的应用密钥格式无效"
            ) from exc
        if len(key) != 32:
            raise SecureStorageUnavailable(
                "Linux Secret Service 中的应用密钥长度无效"
            )
        return key

    def _load_or_create_key(self):
        lookup = self._run(["lookup", *self._ATTRIBUTES])
        if lookup.returncode == 0 and str(lookup.stdout or "").strip():
            return self._decode_key(lookup.stdout)
        if lookup.returncode not in {0, 1}:
            detail = str(lookup.stderr or "").strip() or "服务不可用"
            raise SecureStorageUnavailable(
                f"无法读取 Linux Secret Service：{detail}"
            )

        key = os.urandom(32)
        encoded = base64.b64encode(key).decode("ascii")
        stored = self._run(
            [
                "store",
                "--label=逃费车辆智能查询平台",
                *self._ATTRIBUTES,
            ],
            input_text=encoded + "\n",
        )
        if stored.returncode != 0:
            detail = str(stored.stderr or "").strip() or "服务不可用"
            raise SecureStorageUnavailable(
                f"无法写入 Linux Secret Service：{detail}"
            )
        return key

    def protect(self, value: bytes) -> bytes:
        nonce = os.urandom(12)
        encrypted = AESGCM(self._key).encrypt(nonce, bytes(value), self._AAD)
        return self._PREFIX + nonce + encrypted

    def unprotect(self, value: bytes) -> bytes:
        payload = bytes(value)
        if not payload.startswith(self._PREFIX):
            raise SecureStorageUnavailable("Linux 本机在线凭据格式无效")
        encrypted = payload[len(self._PREFIX) :]
        if len(encrypted) < 12 + 16:
            raise SecureStorageUnavailable("Linux 本机在线凭据内容不完整")
        nonce, ciphertext = encrypted[:12], encrypted[12:]
        try:
            return AESGCM(self._key).decrypt(nonce, ciphertext, self._AAD)
        except Exception as exc:
            raise SecureStorageUnavailable("无法解密 Linux 本机在线凭据") from exc


def get_default_protector():
    if os.name == "nt":
        return DpapiProtector()
    if sys.platform.startswith("linux"):
        return SecretServiceProtector()
    raise SecureStorageUnavailable("当前系统没有受支持的安全凭据存储")


@dataclass(frozen=True)
class PasswordVerifier:
    salt: str
    digest: str
    iterations: int = 600_000

    def as_dict(self) -> dict:
        return {
            "salt": self.salt,
            "digest": self.digest,
            "iterations": self.iterations,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> PasswordVerifier:
        return cls(
            salt=str(payload["salt"]),
            digest=str(payload["digest"]),
            iterations=int(payload["iterations"]),
        )


def create_password_verifier(password: str) -> PasswordVerifier:
    salt = os.urandom(32)
    iterations = 600_000
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return PasswordVerifier(
        salt=base64.b64encode(salt).decode("ascii"),
        digest=base64.b64encode(digest).decode("ascii"),
        iterations=iterations,
    )


def verify_password_verifier(password: str, verifier: PasswordVerifier) -> bool:
    try:
        salt = base64.b64decode(verifier.salt, validate=True)
        expected = base64.b64decode(verifier.digest, validate=True)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        verifier.iterations,
    )
    return hmac.compare_digest(actual, expected)
