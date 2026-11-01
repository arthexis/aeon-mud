"""Windows DPAPI secret protection and per-character credential naming."""
from __future__ import annotations
import base64
import ctypes
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from .config import load_json, save_json, secrets_path
from .profiles import Profile

class Dpapi:
    @staticmethod
    def available() -> bool:
        return os.name == "nt"

    @staticmethod
    def protect(data: bytes) -> bytes:
        if os.name != "nt":
            raise RuntimeError("DPAPI encryption is only available on Windows")
        return _crypt_protect_data(data)

    @staticmethod
    def unprotect(data: bytes) -> bytes:
        if os.name != "nt":
            raise RuntimeError("DPAPI decryption is only available on Windows")
        return _crypt_unprotect_data(data)


if os.name == "nt":
    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.c_uint),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]


def _bytes_to_blob(data: bytes) -> Any:
    buffer = ctypes.create_string_buffer(data)
    blob = _DATA_BLOB()
    blob.cbData = len(data)
    blob.pbData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    return blob, buffer


def _blob_to_bytes(blob: Any) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _crypt_protect_data(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, _buffer = _bytes_to_blob(data)
    out_blob = _DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return _blob_to_bytes(out_blob)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _crypt_unprotect_data(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, _buffer = _bytes_to_blob(data)
    out_blob = _DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return _blob_to_bytes(out_blob)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def validate_character_name(name: str) -> str:
    # This value becomes part of a plaintext MUD command; reject separators.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise SystemExit("Character name must be 1-64 letters, numbers, '_' or '-'")
    return name


def character_secret_key(profile: Profile, character: str) -> str:
    # Include the exact destination to keep credentials isolated across
    # endpoints even when a profile name is reused.
    return "character:" + json.dumps(
        [profile.host.rstrip(".").casefold(), profile.port, character],
        separators=(",", ":"),
    )


def connection_tab_title(profile: Profile, character: str | None = None) -> str:
    host = profile.host.rstrip(".")
    return f"{host} ({character})" if character else host


def set_connection_tab_title(profile: Profile, character: str | None = None) -> None:
    # OSC 1 pins the tab title in WezTerm, even when Windows updates OSC 2
    # for the foreground process. Avoid affecting unrelated terminals.
    if os.environ.get("WEZTERM_PANE") and sys.stdout.isatty():
        sys.stdout.write("\x1b]1;" + connection_tab_title(profile, character) + "\x1b\\")
        sys.stdout.flush()


class SecretStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or secrets_path()
        self.data = load_json(self.path, {"secrets": {}})
        self.data.setdefault("secrets", {})

    def set_password(self, profile_name: str, password: str) -> None:
        encrypted = Dpapi.protect(password.encode("utf-8"))
        self.data["secrets"][profile_name] = base64.b64encode(encrypted).decode("ascii")
        save_json(self.path, self.data)

    def get_password(self, profile_name: str) -> str | None:
        encoded = self.data["secrets"].get(profile_name)
        if not encoded:
            return None
        encrypted = base64.b64decode(encoded)
        return Dpapi.unprotect(encrypted).decode("utf-8")

    def clear_password(self, profile_name: str) -> None:
        self.data["secrets"].pop(profile_name, None)
        save_json(self.path, self.data)


