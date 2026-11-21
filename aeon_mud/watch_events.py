"""Bounded, asynchronous, local watch-event delivery.

MUD output is untrusted: only completed lines become events; commands are
fixed argv arrays and no server-supplied text is executed. This is the local
Stage A/B bus, not a remotely writable webhook or external workflow engine.
"""
from __future__ import annotations

import queue
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from .terminal_text import strip_ansi
from .realm_watches import realm_key

_SOUND_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
MAX_EVENT_LINE = 1024
MAX_PENDING_EVENTS = 128
MAX_EVENT_RECEIPTS = 128
DEFAULT_COOLDOWN = 5.0


def parse_sound(value: str) -> tuple[str | None, str]:
    """Parse SOUND or MOTIF.SOUND, without requiring a motif."""
    if not isinstance(value, str):
        raise ValueError("sound must be a string")
    parts = value.split(".")
    if len(parts) not in (1, 2) or any(not _SOUND_COMPONENT.fullmatch(part) for part in parts):
        raise ValueError("sound must be SOUND or MOTIF.SOUND (letters, digits, _ or -)")
    return (None, parts[0]) if len(parts) == 1 else (parts[0], parts[1])


def sound_argv(value: str) -> list[str]:
    motif, name = parse_sound(value)
    argv = ["gway-lcd-sound", "sound", "play", name]
    if motif is not None:
        argv.extend(["--motif", motif])
    return argv


def lcd_argv(label: str) -> list[str]:
    if not isinstance(label, str) or not label or len(label) > 160 or any(ord(c) < 32 for c in label):
        raise ValueError("LCD label must be 1-160 printable characters")
    return ["gway-lcd-sound", "lcd", "show", "MUD WATCH", label[:16], "--seconds", "15"]


def _run_local(argv: list[str]) -> None:
    # The installed owner CLI provides the sanctioned user-level LCD/sound
    # path on gway-001. Do not invoke node_exec, a shell, or privileged APIs.
    subprocess.run(argv, check=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                   timeout=10)


class WatchEventBus:
    """One bounded producer/consumer per MUD session.

    Never execute a provider call while the TUI renderer or network receiver
    holds a lock. Failed or unavailable local providers are isolated; no retry
    can cause a burst of sound after a disconnected session.
    """

    def __init__(self, executor=None, clock=None) -> None:
        self.executor = executor or _run_local
        self.clock = clock or time.monotonic
        self.pending: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=MAX_PENDING_EVENTS)
        self.receipts: deque[dict[str, Any]] = deque(maxlen=MAX_EVENT_RECEIPTS)
        self.lock = threading.Lock()
        self.last_match: dict[tuple[str, str], float] = {}
        self.partial = ""
        self.worker: threading.Thread | None = None
        self.closed = False
        self.dropped = 0

    def feed(self, chunk: str, rules: tuple, profile, character: str | None = None) -> None:
        """Consume *server* text, matching only complete logical lines."""
        if not chunk:
            return
        with self.lock:
            if self.closed:
                return
            # A network packet can end anywhere within a line. Bound the
            # retained tail and match once, only on \n or bare \r.
            normalized = (self.partial + chunk).replace("\r\n", "\n").replace("\r", "\n")
            lines = normalized.split("\n")
            self.partial = lines.pop()[-MAX_EVENT_LINE:]
            for line in lines:
                clean = strip_ansi(line)[:MAX_EVENT_LINE]
                if not clean:
                    continue
                for rule in rules:
                    try:
                        matched = rule.regex.search(clean) is not None
                    except (ValueError, re.error):
                        continue
                    if not matched:
                        continue
                    scope = (realm_key(profile), rule.pattern)
                    now = self.clock()
                    last = self.last_match.get(scope)
                    cooldown = getattr(rule, "cooldown", DEFAULT_COOLDOWN)
                    if last is not None and now - last < cooldown:
                        continue
                    self.last_match[scope] = now
                    event = {
                        "schema": 1,
                        "event_id": uuid.uuid4().hex,
                        "type": "mud.watch.matched",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "realm": realm_key(profile),
                        "character": character,
                        "pattern": rule.pattern,
                        "message": clean,
                        "sound": getattr(rule, "sound", None),
                        "lcd": getattr(rule, "lcd", None),
                    }
                    try:
                        self.pending.put_nowait(event)
                    except queue.Full:
                        self.dropped += 1
                    else:
                        if self.worker is None:
                            self.worker = threading.Thread(target=self._drain, daemon=True, name="aeon-watch-events")
                            self.worker.start()

    def _drain(self) -> None:
        while True:
            try:
                event = self.pending.get(timeout=0.2)
            except queue.Empty:
                with self.lock:
                    if self.closed or self.pending.empty():
                        self.worker = None
                        return
                continue
            try:
                actions: list[list[str]] = []
                if event["sound"]:
                    actions.append(sound_argv(event["sound"]))
                if event["lcd"]:
                    actions.append(lcd_argv(event["lcd"]))
                errors: list[str] = []
                for argv in actions:
                    try:
                        self.executor(argv)
                    except (OSError, subprocess.SubprocessError, ValueError) as exc:
                        errors.append(type(exc).__name__)
                with self.lock:
                    self.receipts.append({
                        "event_id": event["event_id"],
                        "type": event["type"],
                        "realm": event["realm"],
                        "pattern": event["pattern"],
                        "status": "failed" if errors else "delivered",
                        "errors": errors,
                    })
            finally:
                self.pending.task_done()

    def reset_partial(self) -> None:
        with self.lock:
            self.partial = ""

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.partial = ""
            self.last_match.clear()
        # Never block terminal restoration on external LCD/sound playback.
