"""Bounded Puppet event stream with optional durable indexes alongside text logs."""
from __future__ import annotations
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import threading
import time
import uuid

from .config import config_dir
from .terminal_text import strip_ansi

MAX_EVENTS = 4096
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_TEXT = 16384


class PuppetJournal:
    def __init__(self, character: str):
        self.session_id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.events = deque(maxlen=MAX_EVENTS)
        self.actions = {}
        self.last_action = None
        self.sequence = 0
        self.path = config_dir() / "puppet-journals" / (character.casefold() + ".jsonl")
        self._restore()

    def _restore(self):
        if not self.path.exists():
            return
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077)):
            raise RuntimeError("unsafe Puppet journal permissions")
        if info.st_size > MAX_JOURNAL_BYTES:
            raise RuntimeError("Puppet journal exceeds size limit")
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(record, dict):
                    continue
                self.sequence = max(self.sequence, int(record.get("seq", 0)))
                if record.get("kind") == "event":
                    self.events.append(record)
                if record.get("kind") == "action":
                    action = record.get("action_id")
                    if isinstance(action, str):
                        self.actions[action] = record
                        self.last_action = action
        # Keep only bounded actions which still have accessible history.
        earliest = self.events[0]["seq"] if self.events else self.sequence + 1
        self.actions = {k: v for k, v in self.actions.items() if v.get("seq", 0) >= earliest - 1}
        if self.last_action not in self.actions:
            self.last_action = None

    def _append(self, record: dict, durable: bool):
        if not durable:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder = self.path.parent.stat()
        if os.name == "posix" and (folder.st_uid != os.getuid() or stat.S_IMODE(folder.st_mode) != 0o700):
            raise RuntimeError("unsafe Puppet journal directory")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or (os.name == "posix" and (st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077)):
                raise RuntimeError("unsafe Puppet journal file")
            data = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            if st.st_size + len(data) > MAX_JOURNAL_BYTES:
                # Rotate at a bounded size; readers receive an expired-cursor signal.
                os.close(fd)
                fd = -1
                old = self.path.with_suffix(".previous")
                self.path.replace(old)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, data)
        finally:
            if fd >= 0:
                os.close(fd)

    def record(self, direction: str, text: str, *, secret: bool = False, log_path: Path | None = None):
        clean = "[redacted]" if secret else strip_ansi(text)
        with self.changed:
            self.sequence += 1
            row = {"kind": "event", "seq": self.sequence, "session_id": self.session_id,
                   "ts": datetime.now(timezone.utc).isoformat(), "direction": direction,
                   "text": clean[:MAX_TEXT], "truncated": len(clean) > MAX_TEXT,
                   "log_path": str(log_path.absolute()) if log_path else None}
            self.events.append(row)
            self._append(row, log_path is not None)
            self.changed.notify_all()
            return row

    def start_action(self, commands: list[str], *, durable: bool = False, log_path=None):
        with self.changed:
            self.sequence += 1
            action = uuid.uuid4().hex
            row = {"kind": "action", "seq": self.sequence, "session_id": self.session_id,
                   "ts": datetime.now(timezone.utc).isoformat(), "action_id": action,
                   "commands": commands, "log_path": str(log_path.absolute()) if log_path else None}
            self.actions[action] = row
            self.last_action = action
            self._append(row, durable)
            self.changed.notify_all()
            return row

    def read(self, *, action_id=None, last=False, after=None, until_next=False,
             wait=0.0, idle=0.0, received_only=True):
        deadline = time.monotonic() + max(0, min(float(wait), 10))
        with self.changed:
            if last:
                action_id = self.last_action
                if action_id is None:
                    raise ValueError("no Puppet action recorded")
            if action_id is not None:
                if action_id not in self.actions:
                    raise ValueError("unknown or expired action ID")
                start = self.actions[action_id]["seq"]
            elif after is not None:
                start = int(after)
            else:
                raise ValueError("provide action_id, last or after cursor")
            newest = self.sequence
            if self.events and start < self.events[0]["seq"] - 1:
                raise ValueError("cursor expired: older events no longer retained")
            end = None
            if until_next:
                for action in self.actions.values():
                    if action["seq"] > start and (end is None or action["seq"] < end):
                        end = action["seq"]
            collected = []
            last_activity = time.monotonic()
            while True:
                current = [r for r in self.events if r["seq"] > start and (end is None or r["seq"] < end)
                           and (not received_only or r["direction"] == "recv")]
                if len(current) != len(collected):
                    collected = current
                    last_activity = time.monotonic()
                now = time.monotonic()
                if (wait <= 0 or now >= deadline or (idle > 0 and collected and now - last_activity >= idle)
                    or (until_next and end is not None)):
                    break
                self.changed.wait(timeout=min(0.1, deadline - now))
            return {"action_id": action_id, "session_id": self.session_id, "cursor_start": start,
                    "cursor_end": self.sequence, "events": collected,
                    "complete": bool(until_next and end is not None)}
