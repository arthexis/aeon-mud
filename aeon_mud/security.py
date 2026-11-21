"""Windows DPAPI secret protection and per-character credential naming."""
from __future__ import annotations
import base64
import ctypes
import json
import os
import re
import stat
import tempfile
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


def _unix_secret_key(path: Path, *, create: bool) -> bytes | None:
    """Return an account-local Fernet key, refusing unsafe files and links.

    This is file-at-rest protection, not a defense against an attacker with
    access to the same Unix account (which holds both ciphertext and key).
    """
    folder = path.parent
    if not folder.exists():
        if not create:
            return None
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if folder.is_symlink() or not folder.is_dir() or folder.stat().st_uid != os.getuid():
        raise PermissionError("unsafe secret configuration directory")
    folder.chmod(0o700)
    key_path = folder / "secrets.key"
    if create:
        from cryptography.fernet import Fernet
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(Fernet.generate_key())
    try:
        fd = os.open(key_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("secret key must be an owner-only regular file (0600)")
        key = handle.read(4096)
    from cryptography.fernet import Fernet
    Fernet(key)  # refuse malformed key rather than losing access to old tokens
    return key


def _unix_check_secret_file(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("secret file must be an owner-only regular file (0600)")


def _unix_write_secrets(path: Path, data: dict[str, Any]) -> None:
    """Replace secrets atomically with restrictive permissions from creation."""
    _unix_check_secret_file(path)
    fd, temporary = tempfile.mkstemp(prefix=".secrets-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class SecretStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or secrets_path()
        if os.name != "nt":
            _unix_check_secret_file(self.path)
        self.data = load_json(self.path, {"secrets": {}})
        self.data.setdefault("secrets", {})

    @staticmethod
    def available() -> bool:
        if Dpapi.available():
            return True
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            return False
        return True

    def _save(self) -> None:
        if Dpapi.available():
            save_json(self.path, self.data)
        else:
            _unix_secret_key(self.path, create=True)
            _unix_write_secrets(self.path, self.data)

    def set_password(self, profile_name: str, password: str) -> None:
        if Dpapi.available():
            encrypted = Dpapi.protect(password.encode("utf-8"))
            token = base64.b64encode(encrypted).decode("ascii")
        else:
            from cryptography.fernet import Fernet
            key = _unix_secret_key(self.path, create=True)
            token = "fernet:" + Fernet(key).encrypt(password.encode("utf-8")).decode("ascii")
        self.data["secrets"][profile_name] = token
        self._save()

    def get_password(self, profile_name: str) -> str | None:
        encoded = self.data["secrets"].get(profile_name)
        if not encoded:
            return None
        if Dpapi.available():
            return Dpapi.unprotect(base64.b64decode(encoded)).decode("utf-8")
        if not encoded.startswith("fernet:"):
            raise ValueError("unsupported Unix secret format; no plaintext fallback")
        from cryptography.fernet import Fernet
        key = _unix_secret_key(self.path, create=False)
        if key is None:
            raise RuntimeError("Unix secret key is missing; refusing to overwrite encrypted credentials")
        return Fernet(key).decrypt(encoded[len("fernet:"):].encode("ascii")).decode("utf-8")

    def clear_password(self, profile_name: str) -> None:
        self.data["secrets"].pop(profile_name, None)
        self._save()


