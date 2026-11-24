"""Crash-safe, owner-only machine checkpoints and append-only decision ledger.

The ledger is authoritative. Every record contains a checksummed snapshot,
written and fsynced *before* any potentially external MUD side effect.
The optional checkpoint is an atomic fast-path copy; after a torn write or
failed replace, recovery reads the final valid ledger record instead.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
from datetime import datetime, timezone

from .config import config_dir
from .realm_watches import realm_key

MAX_SNAPSHOT = 512 * 1024
MAX_LEDGER = 16 * 1024 * 1024
MAX_ENTRY = MAX_SNAPSHOT + 2048
VERSION = 1
SENSITIVE = ("password", "passwd", "secret", "token", "bearer",
             "authorization", "api_key", "private_key", "credential", "pin")
REDACTED = "[redacted]"


class DurableMachineError(ValueError):
    pass


def sensitive(key: str) -> bool:
    return any(term in str(key).lower() for term in SENSITIVE)


def public_value(value, key="", depth=0):
    """Safe, bounded diagnostic data; never persist unbounded raw MUD text."""
    if sensitive(key) or key == "0":
        return REDACTED
    if depth >= 6:
        return "[truncated]"
    if isinstance(value, dict):
        return {str(k)[:80]: public_value(v, str(k), depth + 1)
                for k, v in list(value.items())[:64]}
    if isinstance(value, (tuple, list)):
        return [public_value(x, key, depth + 1) for x in value[:32]]
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, str):
        return value[:256] if len(value) <= 256 else value[:256] + "[truncated]"
    return "[unsupported]"


def sanitized_receipt(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        if key in ("commands", "matched_text"):
            if key == "commands":
                out["command_hashes"] = [
                    hashlib.sha256(command.encode("utf-8")).hexdigest()
                    for command in value[:32] if isinstance(command, str)]
                out["command_count"] = len(value)
            continue
        out[key] = public_value(value, key)
    return out


def stable_bytes(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


class MachineStore:
    """One file pair per realm/character. Never follows symlinks."""

    def __init__(self, profile, character: str, *, root: Path | None = None):
        scope = realm_key(profile) + "::" + character.casefold()
        digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:32]
        self.root = Path(root) if root is not None else config_dir() / "machine-runs"
        self.path = self.root / (digest + ".json")
        self.ledger = self.root / (digest + ".jsonl")
        self.scope_hash = digest
        self.lock = threading.RLock()
        self.sequence = 0
        self.last_checksum = None
        self.lock_fd = None

    def acquire(self):
        """Hold an OS process lock so two clients cannot corrupt one scope."""
        with self.lock:
            if self.lock_fd is not None:
                return
            self._directory()
            path = self.root / (self.scope_hash + ".lock")
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(path, flags, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or (
                    os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600)
                ):
                    raise DurableMachineError("unsafe machine lock file")
                if os.name == "posix":
                    import fcntl
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        raise DurableMachineError("realm/character already has an active machine store") from exc
                else:
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    if info.st_size == 0:
                        os.write(fd, b"1")
                    os.lseek(fd, 0, os.SEEK_SET)
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    except OSError as exc:
                        raise DurableMachineError("realm/character already has an active machine store") from exc
                self.lock_fd = fd
            except BaseException:
                os.close(fd)
                raise

    def close(self):
        with self.lock:
            if self.lock_fd is not None:
                fd = self.lock_fd
                self.lock_fd = None
                if os.name == "posix":
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
                else:
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                os.close(fd)

    @staticmethod
    def _guard_file(path: Path, *, limit: int):
        if path.is_symlink():
            raise DurableMachineError("symlink machine state rejected")
        if not path.exists():
            return False
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            raise DurableMachineError("unsafe or oversized machine state")
        if os.name == "posix" and (
            st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600):
            raise DurableMachineError("machine state requires owner-only 0600")
        return True

    def _directory(self):
        if self.root.is_symlink():
            raise DurableMachineError("symlink machine directory rejected")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = self.root.lstat()
        if not stat.S_ISDIR(st.st_mode) or (
            os.name == "posix" and (
                st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700)):
            raise DurableMachineError("machine directory requires owner-only 0700")

    def _open_log(self):
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.ledger, flags, 0o600)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (
            os.name == "posix" and (
                st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600)):
            os.close(fd)
            raise DurableMachineError("machine ledger file permissions invalid")
        return fd, st.st_size

    def _load_ledger(self):
        if not self._guard_file(self.ledger, limit=MAX_LEDGER):
            return None, 0
        # Do not silently skip malformed/torn entries in the middle.
        last = None
        seq = 0
        previous = None
        with self.ledger.open("rb") as handle:
            for line in handle:
                if not line.endswith(b"\n"):
                    # The last append was torn. Recover the valid prefix only;
                    # a subsequent writer must first truncate the bad tail.
                    break
                if len(line) > MAX_ENTRY:
                    raise DurableMachineError("oversized ledger entry")
                try:
                    row = json.loads(line)
                    snap = row["snapshot"]
                    if (row["version"] != VERSION or
                        row["scope_hash"] != self.scope_hash or
                        row["seq"] != seq + 1 or
                        row.get("previous") != previous or
                        row["checksum"] != hashlib.sha256(stable_bytes(
                            {k: v for k, v in row.items() if k != "checksum"})).hexdigest()):
                        raise DurableMachineError("invalid ledger ordering or checksum")
                except (ValueError, KeyError, TypeError) as exc:
                    raise DurableMachineError("machine ledger integrity failure") from exc
                last = snap
                seq = row["seq"]
                previous = row["checksum"]
        self.last_checksum = previous
        return last, seq

    def load(self):
        with self.lock:
            latest, number = self._load_ledger()
            self.sequence = number
            if latest is not None:
                # Ledger wins even if a checkpoint was torn or left stale.
                return latest
            if not self._guard_file(self.path, limit=MAX_SNAPSHOT):
                return None
            try:
                obj = json.loads(self.path.read_text(encoding="utf-8"))
                if (obj["version"] != VERSION or obj["scope_hash"] != self.scope_hash or
                    obj["checksum"] != hashlib.sha256(stable_bytes(
                        {k: v for k, v in obj.items() if k != "checksum"})).hexdigest()):
                    raise DurableMachineError("machine checkpoint integrity failure")
                # Never accept a checkpoint without the matching append ledger:
                # a crash might have deleted the only proof of a pending send.
                raise DurableMachineError("orphaned checkpoint without machine ledger")
            except (ValueError, KeyError, TypeError) as exc:
                raise DurableMachineError("invalid machine checkpoint") from exc

    def commit(self, snapshot: dict, *, reason: str) -> int:
        with self.lock:
            if self.lock_fd is None:
                raise DurableMachineError("machine store has no exclusive lock")
            if len(reason) > 64 or not reason:
                raise DurableMachineError("invalid checkpoint reason")
            raw = stable_bytes(snapshot)
            if len(raw) > MAX_SNAPSHOT:
                raise DurableMachineError("machine checkpoint exceeds size limit")
            self._directory()
            self._guard_file(self.path, limit=MAX_SNAPSHOT)
            self._guard_file(self.ledger, limit=MAX_LEDGER)
            # Re-open the entire ledger, including checking for a torn tail.
            # Truncating to a verified newline boundary is safe only when the
            # prefix has already been validated by load().
            if self.ledger.exists():
                with self.ledger.open("rb") as reader:
                    contents = reader.read(MAX_LEDGER + 1)
                if len(contents) > MAX_LEDGER:
                    raise DurableMachineError("machine ledger exceeds size limit")
                if contents and not contents.endswith(b"\n"):
                    valid_prefix = contents.rsplit(b"\n", 1)[0]
                    valid_bytes = len(valid_prefix) + (1 if valid_prefix else 0)
                    if self.sequence != contents[:valid_bytes].count(b"\n"):
                        raise DurableMachineError("machine ledger tail has unverified prefix")
                    flags = os.O_WRONLY
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    fd = os.open(self.ledger, flags)
                    try:
                        os.ftruncate(fd, valid_bytes)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            seq = self.sequence + 1
            row = {"version": VERSION, "scope_hash": self.scope_hash,
                   "seq": seq, "reason": reason,
                   "timestamp": datetime.now(timezone.utc).isoformat(),
                   "previous": self.last_checksum, "snapshot": snapshot}
            row["checksum"] = hashlib.sha256(stable_bytes(row)).hexdigest()
            encoded = stable_bytes(row) + b"\n"
            if len(encoded) > MAX_ENTRY:
                raise DurableMachineError("machine ledger entry exceeds size limit")
            fd, size = self._open_log()
            try:
                if size + len(encoded) > MAX_LEDGER:
                    raise DurableMachineError("machine ledger full; explicit maintenance required")
                sent = 0
                while sent < len(encoded):
                    count = os.write(fd, encoded[sent:])
                    if count <= 0:
                        raise OSError("short machine ledger write")
                    sent += count
                os.fsync(fd)
            finally:
                os.close(fd)
            self.sequence = seq
            self.last_checksum = row["checksum"]
            # Snapshot is a fast-path mirror, not the authority.
            payload = stable_bytes(row)
            temp = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=self.root, prefix=".machine-", suffix=".tmp",
                    delete=False, mode="wb") as file:
                    temp = file.name
                    file.write(payload)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temp, self.path)
                temp = None
                if os.name == "posix":
                    dirfd = os.open(self.root, os.O_RDONLY)
                    try:
                        os.fsync(dirfd)
                    finally:
                        os.close(dirfd)
            finally:
                if temp is not None:
                    Path(temp).unlink(missing_ok=True)
            return seq
