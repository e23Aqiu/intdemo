from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import os
from ctypes import wintypes
from dataclasses import dataclass


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
