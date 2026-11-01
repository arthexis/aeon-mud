"""Plaintext, text-log and DPAPI encrypted session transcripts."""
from __future__ import annotations
import base64
import json
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from .config import DEFAULT_LOG_FILE, ensure_parent, transcript_dir, utc_now
from .profiles import Profile
from .security import Dpapi
from .terminal_text import strip_ansi

def resolve_log_path(destination: str | None) -> Path:
    raw = (destination or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        raw = raw[1:-1].strip()
    if not raw:
        raw = DEFAULT_LOG_FILE
    return Path(raw).expanduser()


class Transcript:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.mode = profile.transcript
        self.path: Path | None = None
        self.lock = threading.Lock()
        if self.mode == "off":
            return
        if self.mode == "encrypted" and not Dpapi.available():
            raise SystemExit("Encrypted transcripts require Windows DPAPI. Use --transcript plain or off.")
        base_dir = Path(profile.transcript_path).expanduser() if profile.transcript_path else transcript_dir()
        base_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", profile.label())
        suffix = ".dpapi.jsonl" if self.mode == "encrypted" else ".jsonl"
        self.path = base_dir / f"{safe_name}-{stamp}{suffix}"

    def enable_text_log(self, destination: str | None = None) -> Path:
        path = resolve_log_path(destination)
        if path.exists() and path.is_dir():
            raise ValueError(f"{path} is a directory")
        ensure_parent(path)
        with self.lock:
            self.mode = "text"
            self.path = path
            self.profile.transcript = "text"
            self.profile.transcript_path = str(path)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(f"\n--- log started {utc_now()} ---\n")
        return path

    def disable(self) -> None:
        with self.lock:
            self.mode = "off"
            self.path = None
            self.profile.transcript = "off"
            self.profile.transcript_path = ""

    def write(self, direction: str, text: str, secret: bool = False) -> None:
        if self.mode == "off" or not self.path:
            return
        if self.mode == "text":
            clean_text = "[redacted]" if secret else strip_ansi(text)
            with self.lock:
                ensure_parent(self.path)
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    if direction == "send":
                        handle.write(f"\n> {clean_text}\n")
                    else:
                        handle.write(clean_text)
            return
        payload = {
            "ts": utc_now(),
            "direction": direction,
            "text": "[redacted]" if secret else text,
            "secret": bool(secret),
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self.lock:
            ensure_parent(self.path)
            with self.path.open("ab") as handle:
                if self.mode == "encrypted":
                    encrypted = Dpapi.protect(raw)
                    handle.write(base64.b64encode(encrypted) + b"\n")
                else:
                    handle.write(raw + b"\n")


def decrypt_transcript(source: Path, destination: Path | None) -> None:
    lines: list[str] = []
    with source.open("rb") as handle:
        for raw_line in handle:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            decrypted = Dpapi.unprotect(base64.b64decode(raw_line))
            lines.append(decrypted.decode("utf-8"))
    output = "\n".join(lines) + ("\n" if lines else "")
    if destination:
        ensure_parent(destination)
        destination.write_text(output, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(output)


