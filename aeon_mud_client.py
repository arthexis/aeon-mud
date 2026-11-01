#!/usr/bin/env python3
"""Local line-oriented Telnet client for MUDs.

The client intentionally treats Telnet as insecure transport. It adds guard
rails around plaintext sessions, but it does not make Telnet cryptographically
safe. For secure hops, use a SOCKS5 proxy or a profile proxy_command such as
OpenSSH's "-W host:port" mode.
"""

from __future__ import annotations

import argparse
import base64
import codecs
import ctypes
import getpass
import hashlib
import ipaddress
import json
import os
import queue
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
from unittest import mock
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


APP_DIR_NAME = "AeonMud"
CLIENT_DIR_NAME = "client"
DEFAULT_PROFILE = "direct"
DEFAULT_PORT = 4000
DEFAULT_LOG_FILE = "output.log"
DEFAULT_KEEPALIVE_INTERVAL = 30.0
DEFAULT_RECONNECT_INITIAL_DELAY = 1.0
DEFAULT_RECONNECT_MAX_DELAY = 20.0
FAST_PAGE_SCROLL_PAGES = 3
SESSION_PROFILE_FORMAT = "aeon-mud-session-profile"
SESSION_PROFILE_VERSION = 1
MAX_INITIAL_BANNER_BYTES = 8192
MAX_TELNET_SUBNEGOTIATION_BYTES = 8192
OUTBOUND_BUFFER_CHARS = 1024
OUTBOUND_SPOOF_PREFIX = "spoof "
RUNTIME_PROFILE_FIELDS = (
    "paste_line_delay",
    "max_history",
    "max_scrollback",
    "keepalive_interval",
    "reconnect",
    "reconnect_initial_delay",
    "reconnect_max_delay",
)

IAC = 255
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250
SE = 240
NOP = 241

TELNET_ECHO = 1
TELNET_SGA = 3
TELNET_TTYPE = 24
TELNET_NAWS = 31
TELNET_LINEMODE = 34

TTYPE_IS = 0
TTYPE_SEND = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def config_dir() -> Path:
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / APP_DIR_NAME / CLIENT_DIR_NAME
    return Path.home() / ".aeon-mud" / CLIENT_DIR_NAME


def profile_path() -> Path:
    return config_dir() / "profiles.json"


def known_hosts_path() -> Path:
    return config_dir() / "known_hosts.json"


def history_path() -> Path:
    return config_dir() / "history.json"


def secrets_path() -> Path:
    return config_dir() / "secrets.json"


def transcript_dir() -> Path:
    return config_dir() / "transcripts"


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def save_json(path: Path, data: dict[str, Any]) -> None:
    ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def compact_json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


class HostPortError(ValueError):
    pass


def parse_port_value(raw_port: str) -> int:
    if not raw_port:
        raise HostPortError("PORT must not be empty")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise HostPortError("PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise HostPortError("PORT must be between 1 and 65535")
    return port


def split_optional_host_port(value: str) -> tuple[str, int | None]:
    text = value.strip()
    if not text:
        raise HostPortError("HOST must not be empty")
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            raise HostPortError("Bracketed IPv6 host is missing a closing ]")
        host = text[1:end].strip()
        rest = text[end + 1 :]
        if not host:
            raise HostPortError("HOST must not be empty")
        if not rest:
            return host, None
        if not rest.startswith(":"):
            raise HostPortError("Expected [HOST]:PORT")
        return host, parse_port_value(rest[1:])
    if ":" not in text:
        return text, None
    if text.count(":") == 1:
        host, raw_port = text.rsplit(":", 1)
        host = host.strip()
        if not host:
            raise HostPortError("HOST must not be empty")
        return host, parse_port_value(raw_port.strip())
    return text, None


def parse_host_port(value: str) -> tuple[str, int]:
    host, port = split_optional_host_port(value)
    if port is None:
        if ":" in host:
            raise HostPortError("Bracket IPv6 addresses as [HOST]:PORT")
        raise HostPortError("Expected HOST:PORT")
    return host, port


def normalize_host_port(host: str, port: int | None, *, explicit_port: bool = False) -> tuple[str, int]:
    parsed_host, parsed_port = split_optional_host_port(host)
    selected_port = DEFAULT_PORT if port is None else parse_port_value(str(port))
    if parsed_port is not None:
        if explicit_port and port is not None and selected_port != parsed_port:
            raise HostPortError(f"--host includes port {parsed_port}, but --port is {selected_port}")
        selected_port = parsed_port
    return parsed_host, selected_port


@dataclass
class Profile:
    name: str
    host: str
    port: int = DEFAULT_PORT
    allow_plaintext: bool = False
    require_private_host: bool = True
    approved_public_endpoint: str = ""
    expected_banner: str = ""
    username: str = ""
    auto_login: bool = False
    login_prompt: str = r"(?im)(?:name|username|login):\s*$"
    password_prompt: str = r"(?im)password:\s*$"
    transcript: str = "off"
    transcript_path: str = ""
    socks5_host: str = ""
    socks5_port: int = 0
    proxy_command: list[str] = field(default_factory=list)
    terminal_type: str = "ANSI"
    encoding: str = "utf-8"
    newline: str = "\r\n"
    connect_timeout: float = 10.0
    sanity_timeout: float = 0.0
    keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL
    reconnect: bool = True
    reconnect_initial_delay: float = DEFAULT_RECONNECT_INITIAL_DELAY
    reconnect_max_delay: float = DEFAULT_RECONNECT_MAX_DELAY
    paste_line_delay: float = 0.06
    max_history: int = 200
    max_scrollback: int = 5000

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "Profile":
        allowed = set(cls.__dataclass_fields__.keys()) - {"name"}
        values = {key: value for key, value in data.items() if key in allowed}
        profile = cls(name=name, **values)
        try:
            profile.host, profile.port = normalize_host_port(profile.host, profile.port)
        except HostPortError as exc:
            raise SystemExit(f"Invalid host for profile {name}: {exc}") from exc
        return profile

    def to_stored_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("name", None)
        return data

    def label(self) -> str:
        return self.name or DEFAULT_PROFILE

    def target_key(self) -> str:
        return f"{self.host}:{self.port}"


class ProfileStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or profile_path()
        self.data = load_json(self.path, {"profiles": {}})
        self.data.setdefault("profiles", {})

    def save(self) -> None:
        save_json(self.path, self.data)

    def names(self) -> list[str]:
        return sorted(self.data["profiles"])

    def get(self, name: str) -> Profile:
        try:
            raw = self.data["profiles"][name]
        except KeyError as exc:
            raise SystemExit(f"Profile not found: {name}") from exc
        return Profile.from_dict(name, raw)

    def set(self, profile: Profile) -> None:
        self.data["profiles"][profile.name] = profile.to_stored_dict()
        self.save()

    def find_host(self, host: str, port: int | None = None, *, short: bool = False) -> Profile | None:
        """Resolve a host by exact FQDN or unique leftmost subdomain."""
        normalized = host.rstrip(".").casefold()
        matches = [
            self.get(name) for name in self.names()
            if (self.data["profiles"][name].get("host", "").rstrip(".").casefold().split(".")[0]
                if short else self.data["profiles"][name].get("host", "").rstrip(".").casefold()) == normalized
        ]
        if port is not None:
            matches = [item for item in matches if item.port == port]
        return matches[0] if len(matches) == 1 else None

    def auto_profile_name(self, host: str, port: int) -> str:
        normalized = host.rstrip(".").casefold()
        for candidate in (normalized, f"{normalized}:{port}"):
            if candidate not in self.data["profiles"]:
                return candidate
        raise SystemExit(f"Cannot save automatic profile: name already in use for {host}:{port}")

    def remove(self, name: str) -> None:
        if name not in self.data["profiles"]:
            raise SystemExit(f"Profile not found: {name}")
        del self.data["profiles"][name]
        self.save()


class KnownHosts:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or known_hosts_path()
        self.data = load_json(self.path, {"hosts": {}})
        self.data.setdefault("hosts", {})

    def get(self, key: str) -> dict[str, Any] | None:
        item = self.data["hosts"].get(key)
        return item if isinstance(item, dict) else None

    def set(self, key: str, banner_text: str) -> None:
        self.data["hosts"][key] = {
            "banner_sha256": hashlib.sha256(banner_text.encode("utf-8", "replace")).hexdigest(),
            "updated_at": utc_now(),
        }
        save_json(self.path, self.data)


class HistoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or history_path()
        self.data = load_json(self.path, {"profiles": {}})
        self.data.setdefault("profiles", {})

    def list_for(self, profile: Profile) -> list[str]:
        values = self.data["profiles"].get(profile.label(), [])
        return [value for value in values if isinstance(value, str)]

    def add(self, profile: Profile, command: str) -> None:
        command = command.rstrip("\r\n")
        if not command:
            return
        values = self.list_for(profile)
        if values and values[-1] == command:
            return
        values.append(command)
        if len(values) > profile.max_history:
            values = values[-profile.max_history :]
        self.data["profiles"][profile.label()] = values
        save_json(self.path, self.data)

    def clear(self, name: str) -> None:
        self.data["profiles"][name] = []
        save_json(self.path, self.data)


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


ANSI_CSI_RE = re.compile(r"\x1b\[([0-?]*[ -/]*[@-~])")
ANSI_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
ANSI_DROP_RE = re.compile(r"\x1b(?:P|X|\^|_).*?\x1b\\", re.DOTALL)
ANSI_UNSAFE_ESC_RE = re.compile(r"\x1b(?!\[[0-9;]*m).")
ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
WATCH_COLOR_CODES = {
    "black": "30",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "orange": "33",
    "blue": "34",
    "magenta": "35",
    "purple": "35",
    "cyan": "36",
    "white": "37",
    "default": "39",
    "gray": "90",
    "grey": "90",
    "bright-black": "90",
    "bright-gray": "90",
    "bright-grey": "90",
    "bright-red": "91",
    "bright-green": "92",
    "bright-yellow": "93",
    "bright-blue": "94",
    "bright-magenta": "95",
    "bright-purple": "95",
    "bright-cyan": "96",
    "bright-white": "97",
}
WATCH_RESET = "\x1b[0m"


def sanitize_terminal_text(text: str) -> str:
    """Keep MUD color SGR while dropping risky terminal controls."""
    text = ANSI_OSC_RE.sub("", text)
    text = ANSI_DROP_RE.sub("", text)

    def keep_safe_csi(match: re.Match[str]) -> str:
        sequence = match.group(1)
        if sequence.endswith("m"):
            return match.group(0)
        return ""

    text = ANSI_CSI_RE.sub(keep_safe_csi, text)
    text = ANSI_UNSAFE_ESC_RE.sub("", text)
    return text



class TerminalStreamDecoder:
    """Decode fragmented text and allow only complete, safe SGR sequences."""

    def __init__(self, encoding: str) -> None:
        self.decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        self.state = "text"
        self.escape = ""
        self.max_escape = 128

    def feed(self, data: bytes) -> str:
        result: list[str] = []
        for char in self.decoder.decode(data):
            if self.state == "text":
                if char == "\x1b":
                    self.state = "esc"
                elif char not in ("\x7f",) and not ("\x80" <= char <= "\x9f"):
                    result.append(char)
            elif self.state == "esc":
                if char == "[":
                    self.state = "csi"
                    self.escape = ""
                elif char == "]":
                    self.state = "osc"
                elif char in ("P", "X", "^", "_"):
                    self.state = "string"
                else:
                    self.state = "text"
            elif self.state == "csi":
                if "@" <= char <= "~":
                    if char == "m" and re.fullmatch(r"[0-9;]*", self.escape):
                        result.append("\x1b[" + self.escape + "m")
                    self.state = "text"
                    self.escape = ""
                elif len(self.escape) >= self.max_escape:
                    self.state = "discard_csi"
                    self.escape = ""
                else:
                    self.escape += char
            elif self.state == "discard_csi":
                if "@" <= char <= "~":
                    self.state = "text"
            elif self.state in ("osc", "string"):
                if char == "\x07" and self.state == "osc":
                    self.state = "text"
                elif char == "\x1b":
                    self.state = "string_esc" if self.state == "string" else "osc_esc"
            elif self.state in ("osc_esc", "string_esc"):
                if char == "\\":
                    self.state = "text"
                elif char != "\x1b":
                    self.state = "osc" if self.state == "osc_esc" else "string"
        return "".join(result)


def strip_ansi(text: str) -> str:
    return ANSI_CSI_RE.sub("", ANSI_OSC_RE.sub("", text))


def split_outbound_buffers(line: str) -> list[str]:
    if len(line) <= OUTBOUND_BUFFER_CHARS:
        return [line]
    spoof_payload_chars = OUTBOUND_BUFFER_CHARS - len(OUTBOUND_SPOOF_PREFIX)
    chunks = [line[:OUTBOUND_BUFFER_CHARS]]
    remaining = line[OUTBOUND_BUFFER_CHARS:]
    while remaining:
        chunks.append(OUTBOUND_SPOOF_PREFIX + remaining[:spoof_payload_chars])
        remaining = remaining[spoof_payload_chars:]
    return chunks


@dataclass(frozen=True)
class WatchRule:
    pattern: str
    color: str
    sgr: str
    regex: re.Pattern[str]

    def matches(self, line: str) -> bool:
        return self.regex.match(strip_ansi(str(line))) is not None


def strip_outer_quotes(text: str) -> str:
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def resolve_watch_color(color: str) -> tuple[str, str]:
    normalized = color.strip().lower().replace("_", "-")
    if not normalized:
        raise ValueError("color must not be empty")
    code = WATCH_COLOR_CODES.get(normalized)
    if code is not None:
        return normalized, f"\x1b[{code}m"
    if normalized.startswith("bold-"):
        base = normalized[5:]
        code = WATCH_COLOR_CODES.get(base)
        if code is not None:
            return normalized, f"\x1b[1;{code}m"
    raise ValueError("unknown color; use red, green, yellow, blue, magenta, cyan, white, gray, or bright-*")


def compile_watch_pattern(pattern: str) -> re.Pattern[str]:
    text = strip_outer_quotes(pattern)
    if not text:
        raise ValueError("pattern must not be empty")
    regex = "^" + ".*".join(re.escape(part) for part in text.split("*"))
    return re.compile(regex)


def make_watch_rule(pattern: str, color: str) -> WatchRule:
    clean_pattern = strip_outer_quotes(pattern)
    clean_color, sgr = resolve_watch_color(color)
    return WatchRule(
        pattern=clean_pattern,
        color=clean_color,
        sgr=sgr,
        regex=compile_watch_pattern(clean_pattern),
    )


def apply_watch_rules(line: str, rules: list[WatchRule]) -> str:
    for rule in reversed(rules):
        try:
            matched = rule.matches(line)
        except Exception:
            continue
        if matched:
            return f"{rule.sgr}{line}{WATCH_RESET}"
    return line


def describe_watch_rules(rules: list[WatchRule]) -> str:
    if not rules:
        return "No watch rules"
    return "Watches: " + ", ".join(f"{rule.pattern}={rule.color}" for rule in rules)


def parse_watch_arguments(argument: str) -> tuple[str, str]:
    text = argument.strip()
    if not text:
        raise ValueError("usage: /watch pattern color")
    parts = text.rsplit(None, 1)
    if len(parts) != 2:
        raise ValueError("usage: /watch pattern color")
    pattern, color = parts
    pattern = strip_outer_quotes(pattern)
    if not pattern:
        raise ValueError("pattern must not be empty")
    return pattern, color


def watch_rules_to_data(rules: list[WatchRule]) -> list[dict[str, str]]:
    return [{"pattern": rule.pattern, "color": rule.color} for rule in rules]


def watch_rules_from_data(data: Any) -> list[WatchRule]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError("watch_rules must be a list")
    rules: list[WatchRule] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"watch rule {index} must be an object")
        pattern = item.get("pattern")
        color = item.get("color")
        if not isinstance(pattern, str) or not isinstance(color, str):
            raise ValueError(f"watch rule {index} must include string pattern and color")
        rules.append(make_watch_rule(pattern, color))
    return rules


def truncate_plain(text: str, width: int) -> str:
    plain = strip_ansi(text)
    if width <= 0:
        return ""
    if len(plain) <= width:
        return plain
    return plain[: max(0, width - 1)] + ">"


def truncate_ansi(text: str, width: int) -> str:
    if width <= 0:
        return ""
    out: list[str] = []
    visible = 0
    index = 0
    for match in ANSI_SGR_RE.finditer(text):
        if match.start() > index:
            chunk = text[index : match.start()]
            for char in chunk:
                if visible >= width:
                    return "".join(out)
                out.append(char)
                visible += 1
        out.append(match.group(0))
        index = match.end()
    if index < len(text):
        for char in text[index:]:
            if visible >= width:
                break
            out.append(char)
            visible += 1
    return "".join(out)


def update_sgr_state(active: str, sequence: str) -> str:
    params = sequence[2:-1]
    raw_codes = ["0"] if not params else params.split(";")
    codes: list[int] = []
    for raw_code in raw_codes:
        try:
            codes.append(int(raw_code or "0"))
        except ValueError:
            return active + sequence
    if 0 not in codes:
        return active + sequence
    reset_index = max(index for index, code in enumerate(codes) if code == 0)
    return sequence if any(code != 0 for code in codes[reset_index + 1 :]) else ""


def wrap_ansi(text: str, width: int) -> list[str]:
    if width <= 0:
        return [""]
    rows = [""]
    visible = 0
    active_sgr = ""
    index = 0
    for match in ANSI_SGR_RE.finditer(text):
        for char in text[index : match.start()]:
            if visible >= width:
                rows.append(active_sgr)
                visible = 0
            rows[-1] += char
            visible += 1
        sequence = match.group(0)
        rows[-1] += sequence
        active_sgr = update_sgr_state(active_sgr, sequence)
        index = match.end()
    for char in text[index:]:
        if visible >= width:
            rows.append(active_sgr)
            visible = 0
        rows[-1] += char
        visible += 1
    return rows


class TelnetCodec:
    def __init__(self, terminal_type: str = "ANSI") -> None:
        self.state = "data"
        self.command = 0
        self.sb_data = bytearray()
        self.sb_overflow = False
        self.terminal_type = terminal_type.encode("ascii", "replace")[:40] or b"ANSI"
        self.width = 100
        self.height = 30

    def set_window_size(self, width: int, height: int) -> bytes:
        self.width = max(1, min(width, 65535))
        self.height = max(1, min(height, 65535))
        return self._naws_bytes()

    def feed(self, data: bytes) -> tuple[bytes, bytes]:
        output = bytearray()
        responses = bytearray()
        for byte in data:
            if self.state == "data":
                if byte == IAC:
                    self.state = "iac"
                else:
                    output.append(byte)
            elif self.state == "iac":
                if byte == IAC:
                    output.append(IAC)
                    self.state = "data"
                elif byte in (DO, DONT, WILL, WONT):
                    self.command = byte
                    self.state = "option"
                elif byte == SB:
                    self.sb_data = bytearray()
                    self.sb_overflow = False
                    self.state = "sb"
                else:
                    self.state = "data"
            elif self.state == "option":
                responses.extend(self._negotiate(self.command, byte))
                self.state = "data"
            elif self.state == "sb":
                if byte == IAC:
                    self.state = "sb_iac"
                elif not self.sb_overflow:
                    if len(self.sb_data) < MAX_TELNET_SUBNEGOTIATION_BYTES:
                        self.sb_data.append(byte)
                    else:
                        self.sb_overflow = True
                        self.sb_data.clear()
            elif self.state == "sb_iac":
                if byte == SE:
                    if not self.sb_overflow:
                        responses.extend(self._subnegotiate(bytes(self.sb_data)))
                    self.sb_data.clear()
                    self.sb_overflow = False
                    self.state = "data"
                else:
                    if not self.sb_overflow:
                        escaped = (IAC,) if byte == IAC else (IAC, byte)
                        if len(self.sb_data) + len(escaped) <= MAX_TELNET_SUBNEGOTIATION_BYTES:
                            self.sb_data.extend(escaped)
                        else:
                            self.sb_overflow = True
                            self.sb_data.clear()
                    self.state = "sb"
        return bytes(output), bytes(responses)

    def _cmd(self, command: int, option: int) -> bytes:
        return bytes([IAC, command, option])

    def _negotiate(self, command: int, option: int) -> bytes:
        if command == DO:
            if option in (TELNET_SGA, TELNET_NAWS, TELNET_TTYPE):
                response = bytearray(self._cmd(WILL, option))
                if option == TELNET_NAWS:
                    response.extend(self._naws_bytes())
                return bytes(response)
            return self._cmd(WONT, option)
        if command == WILL:
            if option in (TELNET_ECHO, TELNET_SGA):
                return self._cmd(DO, option)
            return self._cmd(DONT, option)
        if command in (DONT, WONT):
            return b""
        return b""

    def _subnegotiate(self, data: bytes) -> bytes:
        if not data:
            return b""
        option = data[0]
        payload = data[1:]
        if option == TELNET_TTYPE and payload[:1] == bytes([TTYPE_SEND]):
            return bytes([IAC, SB, TELNET_TTYPE, TTYPE_IS]) + self.terminal_type + bytes([IAC, SE])
        return b""

    def _naws_bytes(self) -> bytes:
        width = struct.pack("!H", self.width).replace(bytes([IAC]), bytes([IAC, IAC]))
        height = struct.pack("!H", self.height).replace(bytes([IAC]), bytes([IAC, IAC]))
        return bytes([IAC, SB, TELNET_NAWS]) + width + height + bytes([IAC, SE])


class Transport:
    def sendall(self, data: bytes) -> None:
        raise NotImplementedError

    def recv(self, size: int) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class SocketTransport(Transport):
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock

    def sendall(self, data: bytes) -> None:
        self.sock.sendall(data)

    def recv(self, size: int) -> bytes:
        return self.sock.recv(size)

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class ProcessTransport(Transport):
    def __init__(self, command: list[str]) -> None:
        if not command:
            raise ValueError("proxy_command cannot be empty")
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

    def sendall(self, data: bytes) -> None:
        if not self.process.stdin:
            raise OSError("proxy command stdin is closed")
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def recv(self, size: int) -> bytes:
        if not self.process.stdout:
            return b""
        return os.read(self.process.stdout.fileno(), size)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()


class ConnectionUnavailable(OSError):
    pass


class ConnectionChanged(OSError):
    pass


def configure_tcp_socket(sock: socket.socket, keepalive_interval: float) -> None:
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass
    if hasattr(socket, "TCP_NODELAY"):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
    if keepalive_interval <= 0:
        return
    idle_seconds = max(10, int(keepalive_interval))
    interval_seconds = max(5, min(idle_seconds, idle_seconds // 3 or 5))
    if os.name == "nt" and hasattr(socket, "SIO_KEEPALIVE_VALS"):
        try:
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle_seconds * 1000, interval_seconds * 1000))
        except OSError:
            pass
        return
    for option_name, value in (
        ("TCP_KEEPIDLE", idle_seconds),
        ("TCP_KEEPINTVL", interval_seconds),
        ("TCP_KEEPCNT", 3),
    ):
        if hasattr(socket, option_name):
            try:
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, option_name), value)
            except OSError:
                pass


def recv_exact(sock: socket.socket, count: int) -> bytes:
    """Receive precisely count bytes or reject a truncated protocol frame."""
    result = bytearray()
    while len(result) < count:
        chunk = sock.recv(count - len(result))
        if not chunk:
            raise OSError("Connection closed during SOCKS5 handshake")
        result.extend(chunk)
    return bytes(result)


def connect_socks5(profile: Profile) -> socket.socket:
    sock = socket.create_connection(
        (profile.socks5_host, profile.socks5_port), timeout=profile.connect_timeout
    )
    try:
        configure_tcp_socket(sock, profile.keepalive_interval)
        sock.sendall(b"\x05\x01\x00")
        if recv_exact(sock, 2) != b"\x05\x00":
            raise OSError("SOCKS5 proxy does not allow no-auth connections")

        try:
            ip = ipaddress.ip_address(profile.host)
        except ValueError:
            host_bytes = profile.host.encode("idna")
            if len(host_bytes) > 255:
                raise OSError("SOCKS5 destination hostname is too long")
            address = bytes([3, len(host_bytes)]) + host_bytes
        else:
            address = (b"\x01" if ip.version == 4 else b"\x04") + ip.packed

        sock.sendall(b"\x05\x01\x00" + address + struct.pack("!H", profile.port))
        header = recv_exact(sock, 4)
        if header[0] != 5 or header[2] != 0:
            raise OSError(f"Malformed SOCKS5 response: {header!r}")
        if header[1] != 0:
            raise OSError(f"SOCKS5 connect failed with status {header[1]}")
        atyp = header[3]
        if atyp == 1:
            length = 4
        elif atyp == 3:
            length = recv_exact(sock, 1)[0]
            if not length:
                raise OSError("SOCKS5 returned empty bound hostname")
        elif atyp == 4:
            length = 16
        else:
            raise OSError("SOCKS5 returned unknown address type")
        recv_exact(sock, length + 2)
        return sock
    except BaseException:
        sock.close()
        raise


def is_private_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def make_transport(profile: Profile, *, require_private_peer: bool = False) -> Transport:
    if profile.proxy_command:
        return ProcessTransport(profile.proxy_command)
    if profile.socks5_host:
        sock = connect_socks5(profile)
    else:
        sock = socket.create_connection((profile.host, profile.port), timeout=profile.connect_timeout)
        configure_tcp_socket(sock, profile.keepalive_interval)
    try:
        if require_private_peer and not is_private_address(sock.getpeername()[0]):
            raise OSError("Connected peer violates the private-address policy")
        sock.settimeout(None)
        return SocketTransport(sock)
    except BaseException:
        sock.close()
        raise


def connection_target_label(profile: Profile) -> str:
    if profile.proxy_command:
        return "proxy command: " + " ".join(profile.proxy_command)
    if profile.socks5_host:
        return f"{profile.host}:{profile.port} via SOCKS5 {profile.socks5_host}:{profile.socks5_port}"
    return f"{profile.host}:{profile.port}"


def is_private_target(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addresses: set[str] = {item[4][0] for item in infos}
    if not addresses:
        return False
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            return False
    return True


def prompt_yes_no(
    message: str,
    default: bool = False,
    assume_yes: bool = False,
    input_func: Callable[[str], str] = input,
) -> bool:
    if assume_yes:
        return True
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input_func(message + suffix).strip().lower()
    except EOFError:
        print()
        return default
    if not answer:
        return default
    return answer in {"y", "yes"}


def pre_read_banner(transport: Transport, codec: TelnetCodec, timeout: float) -> str:
    if not isinstance(transport, SocketTransport) or timeout <= 0:
        return ""
    sock = transport.sock
    sock.setblocking(False)
    deadline = time.monotonic() + timeout
    raw = bytearray()
    try:
        while time.monotonic() < deadline and len(raw) < MAX_INITIAL_BANNER_BYTES:
            remaining = max(0.0, deadline - time.monotonic())
            readable, _, _ = select.select([sock], [], [], min(0.1, remaining))
            if not readable:
                continue
            chunk = sock.recv(4096)
            if not chunk:
                break
            output, response = codec.feed(chunk)
            if response:
                sock.sendall(response)
            raw.extend(output)
    finally:
        sock.setblocking(True)
    text = raw.decode("utf-8", "replace")
    return sanitize_terminal_text(text)


def open_session_transport(
    profile: Profile, *, require_private_peer: bool = False,
) -> tuple[Transport, TelnetCodec, str]:
    transport = make_transport(profile, require_private_peer=require_private_peer)
    codec = TelnetCodec(profile.terminal_type)
    try:
        width, height = shutil.get_terminal_size((100, 30))
        try:
            transport.sendall(codec.set_window_size(width, height))
        except OSError:
            pass
        initial_banner = pre_read_banner(transport, codec, profile.sanity_timeout)
    except Exception:
        transport.close()
        raise
    return transport, codec, initial_banner


def run_sanity_checks(
    profile: Profile, banner: str, accept_host_change: bool, assume_yes: bool,
    *, interactive: bool = True,
) -> None:
    if profile.expected_banner and not banner and profile.sanity_timeout > 0 and not interactive:
        raise ValueError("Expected banner was absent on reconnect")
    if profile.expected_banner and banner:
        try:
            matched = re.search(profile.expected_banner, strip_ansi(banner), re.MULTILINE) is not None
        except re.error as exc:
            raise SystemExit(f"Invalid expected_banner regex for {profile.label()}: {exc}") from exc
        if not matched:
            if not interactive:
                raise ValueError("Expected banner did not match on reconnect")
            sample = strip_ansi(banner).replace("\n", " ")[:300]
            ok = prompt_yes_no(
                f"Expected banner did not match for {profile.target_key()}. Saw: {sample!r}. Continue?",
                default=False,
                assume_yes=assume_yes,
            )
            if not ok:
                raise SystemExit("Connection aborted by banner policy")

    if not banner:
        return
    known = KnownHosts()
    key = profile.label()
    current_hash = hashlib.sha256(banner.encode("utf-8", "replace")).hexdigest()
    existing = known.get(key)
    if existing and existing.get("banner_sha256") != current_hash and not accept_host_change:
        if not interactive:
            raise ValueError("Known host banner changed on reconnect")
        ok = prompt_yes_no(
            f"Initial banner changed for profile {profile.label()}. Continue and trust this banner?",
            default=False,
            assume_yes=assume_yes,
        )
        if not ok:
            raise SystemExit("Connection aborted because banner changed")
    known.set(key, banner)


def resolve_log_path(destination: str | None) -> Path:
    raw = (destination or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        raw = raw[1:-1].strip()
    if not raw:
        raw = DEFAULT_LOG_FILE
    return Path(raw).expanduser()


def resolve_session_profile_path(destination: str) -> Path:
    raw = strip_outer_quotes(destination)
    if not raw:
        raise ValueError("file must not be empty")
    return Path(raw).expanduser()


def runtime_profile_settings(profile: Profile) -> dict[str, Any]:
    return {field_name: getattr(profile, field_name) for field_name in RUNTIME_PROFILE_FIELDS}


def coerce_runtime_setting(field_name: str, value: Any) -> Any:
    if field_name == "reconnect":
        if not isinstance(value, bool):
            raise ValueError("reconnect must be true or false")
        return value
    if field_name in {"max_history", "max_scrollback"}:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{field_name} must be a positive integer")
        if value < 1:
            raise ValueError(f"{field_name} must be at least 1")
        return value
    if field_name in {
        "paste_line_delay",
        "keepalive_interval",
        "reconnect_initial_delay",
        "reconnect_max_delay",
    }:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{field_name} must be a non-negative number")
        coerced = float(value)
        if coerced < 0:
            raise ValueError(f"{field_name} must be non-negative")
        return coerced
    raise ValueError(f"unknown runtime setting {field_name}")


def extract_runtime_settings(data: dict[str, Any]) -> dict[str, Any]:
    source = data.get("runtime_settings")
    if source is None:
        source = data.get("profile", {})
    if not isinstance(source, dict):
        raise ValueError("runtime_settings must be an object")
    settings: dict[str, Any] = {}
    for field_name in RUNTIME_PROFILE_FIELDS:
        if field_name in source:
            settings[field_name] = coerce_runtime_setting(field_name, source[field_name])
    if (
        "reconnect_initial_delay" in settings
        and "reconnect_max_delay" in settings
        and settings["reconnect_max_delay"] < settings["reconnect_initial_delay"]
    ):
        raise ValueError("reconnect_max_delay must be greater than or equal to reconnect_initial_delay")
    return settings


def session_profile_data(profile: Profile, transcript: "Transcript", ui: "MudUi") -> dict[str, Any]:
    with ui.lock:
        watch_rules = watch_rules_to_data(ui.watch_rules)
    log_path = str(transcript.path) if transcript.path else ""
    log_mode = "text" if transcript.mode == "text" and log_path else "off"
    return {
        "format": SESSION_PROFILE_FORMAT,
        "version": SESSION_PROFILE_VERSION,
        "saved_at": utc_now(),
        "profile": profile.to_stored_dict(),
        "runtime_settings": runtime_profile_settings(profile),
        "log": {
            "mode": log_mode,
            "path": log_path,
        },
        "watch_rules": watch_rules,
    }


def save_session_profile(path: Path, profile: Profile, transcript: "Transcript", ui: "MudUi") -> None:
    save_json(path, session_profile_data(profile, transcript, ui))


def load_session_profile(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    data = load_json(path, {})
    if data.get("format") not in {None, SESSION_PROFILE_FORMAT}:
        raise ValueError(f"unsupported profile format: {data.get('format')}")
    version = data.get("version", SESSION_PROFILE_VERSION)
    if version != SESSION_PROFILE_VERSION:
        raise ValueError(f"unsupported profile version: {version}")
    return data


def apply_session_profile(data: dict[str, Any], profile: Profile, transcript: "Transcript", ui: "MudUi") -> None:
    settings = extract_runtime_settings(data)
    rules = watch_rules_from_data(data.get("watch_rules", data.get("watches")))
    log = data.get("log", {})
    if log is None:
        log = {}
    if not isinstance(log, dict):
        raise ValueError("log must be an object")
    log_mode = str(log.get("mode", "")).lower() if "mode" in log else ""
    log_path = log.get("path", "")
    if log_path is not None and not isinstance(log_path, str):
        raise ValueError("log.path must be a string")
    if log_mode not in {"", "off", "text", "plain"}:
        raise ValueError(f"unsupported log mode: {log_mode}")
    if log_mode in {"text", "plain"}:
        transcript.enable_text_log(log_path or None)
    elif log_mode == "off":
        transcript.disable()
    for field_name, value in settings.items():
        setattr(profile, field_name, value)
    with ui.lock:
        if len(ui.lines) > profile.max_scrollback:
            ui.lines = ui.lines[-profile.max_scrollback :]
    ui.replace_watches(rules)


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


@dataclass
class Key:
    name: str
    char: str = ""


@dataclass
class InputLayout:
    rows: list[str]
    cursor_row: int
    cursor_col: int


def key_from_control_char(char: str) -> Key:
    if char in ("\r", "\n"):
        return Key("enter")
    if char in ("\b", "\x7f"):
        return Key("backspace")
    if char == "\x03":
        return Key("quit")
    if char == "\x1d":
        return Key("quit")
    if char == "\x0c":
        return Key("redraw")
    if char == "\x15":
        return Key("clear_line")
    if char == "\x17":
        return Key("delete_word")
    if char == "\x01":
        return Key("home")
    if char == "\x05":
        return Key("end")
    if char == "\x16":
        return Key("paste")
    if char == "\x0e":
        return Key("newline")
    if char == "\x0b":
        return Key("delete_to_end")
    if char == "\t":
        return Key("switch_input")
    if char >= " ":
        return Key("char", char)
    return Key(f"control:{ord(char)}")


def key_from_ansi_escape(sequence: str) -> Key:
    if sequence == "\x1b":
        return Key("escape")
    if sequence.startswith("\x1bO"):
        return {
            "P": Key("f1"),
            "Q": Key("f2"),
            "R": Key("f3"),
            "S": Key("f4"),
            "H": Key("home"),
            "F": Key("end"),
        }.get(sequence[2:], Key(f"escape:{sequence[2:]!r}"))
    if not sequence.startswith("\x1b["):
        return Key(f"escape:{sequence[1:]!r}")
    code = sequence[2:]
    simple = {
        "A": Key("up"),
        "B": Key("down"),
        "C": Key("right"),
        "D": Key("left"),
        "H": Key("home"),
        "F": Key("end"),
        "1~": Key("home"),
        "3~": Key("delete"),
        "4~": Key("end"),
        "5~": Key("page_up"),
        "6~": Key("page_down"),
        "11~": Key("f1"),
        "15~": Key("f5"),
    }
    if code in simple:
        return simple[code]
    ctrl_match = re.fullmatch(r"1;5([ABCD])", code)
    if ctrl_match:
        return {
            "A": Key("ctrl_up"),
            "B": Key("ctrl_down"),
            "C": Key("ctrl_right"),
            "D": Key("ctrl_left"),
        }[ctrl_match.group(1)]
    return Key(f"escape:{code!r}")


def layout_input_buffer(buffer: str, cursor: int, width: int, prompt: str, secret: bool = False) -> InputLayout:
    cursor = max(0, min(len(buffer), cursor))
    continuation = " " * len(prompt)
    prefix = prompt
    rows: list[str] = []
    content = ""
    cursor_row = 0
    cursor_col = len(prompt) + 1
    cursor_recorded = False

    def capacity(current_prefix: str) -> int:
        return max(1, width - len(current_prefix))

    def record_cursor() -> None:
        nonlocal cursor_row, cursor_col, cursor_recorded
        cursor_row = len(rows)
        cursor_col = len(prefix) + len(content) + 1
        cursor_recorded = True

    for index, char in enumerate(buffer):
        if char != "\n" and len(content) >= capacity(prefix):
            rows.append(prefix + content)
            prefix = continuation
            content = ""
        if index == cursor:
            record_cursor()
        if char == "\n":
            rows.append(prefix + content)
            prefix = continuation
            content = ""
            continue
        content += "*" if secret else char
    if not cursor_recorded:
        record_cursor()
    rows.append(prefix + content)
    return InputLayout(rows=rows, cursor_row=cursor_row, cursor_col=cursor_col)


def fast_page_scroll_amount(body_height: int) -> int:
    return max(1, (body_height - 1) * FAST_PAGE_SCROLL_PAGES)


class KeyReader:
    EXTENDED_KEYS = {
        "H": "up",
        "P": "down",
        "K": "left",
        "M": "right",
        "I": "page_up",
        "Q": "page_down",
        "G": "home",
        "O": "end",
        "S": "delete",
        "R": "insert",
        ";": "f1",
        "<": "f2",
        "=": "f3",
        ">": "f4",
        "?": "f5",
        "\x8d": "ctrl_up",
        "\x91": "ctrl_down",
        "s": "ctrl_left",
        "t": "ctrl_right",
    }

    def __init__(self) -> None:
        self.msvcrt = None
        self.termios = None
        self.original_termios: list[Any] | None = None
        if os.name == "nt":
            import msvcrt

            self.msvcrt = msvcrt
            return
        if not sys.stdin.isatty():
            raise SystemExit("This interactive client requires a terminal")
        import termios
        import tty

        self.termios = termios
        self.original_termios = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())

    def close(self) -> None:
        if self.termios is not None and self.original_termios is not None:
            self.termios.tcsetattr(sys.stdin.fileno(), self.termios.TCSADRAIN, self.original_termios)
            self.original_termios = None

    def _read_unix_char(self, timeout: float) -> str | None:
        readable, _, _ = select.select([sys.stdin], [], [], max(0.0, timeout))
        if not readable:
            return None
        return os.read(sys.stdin.fileno(), 1).decode("utf-8", "ignore")

    def _read_unix_escape(self) -> Key:
        chars = ["\x1b"]
        first = self._read_unix_char(0.02)
        if first is None:
            return Key("escape")
        chars.append(first)
        if first not in {"[", "O"}:
            return key_from_ansi_escape("".join(chars))
        while True:
            char = self._read_unix_char(0.02)
            if char is None:
                break
            chars.append(char)
            if first == "O" or char.isalpha() or char == "~":
                break
        return key_from_ansi_escape("".join(chars))

    def read_key(self, timeout: float = 0.1) -> Key | None:
        if self.msvcrt is None:
            char = self._read_unix_char(timeout)
            if char is None:
                return None
            if char == "\x1b":
                return self._read_unix_escape()
            return key_from_control_char(char)

        deadline = time.monotonic() + timeout
        while not self.msvcrt.kbhit():
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)
        char = self.msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            code = self.msvcrt.getwch()
            return Key(self.EXTENDED_KEYS.get(code, f"extended:{ord(code)}"))
        if char == "\x1b":
            return Key("escape")
        return key_from_control_char(char)


class MudUi:
    def __init__(self, profile: Profile, no_alt_screen: bool = False) -> None:
        self.profile = profile
        self.no_alt_screen = no_alt_screen
        self.lines: list[str] = [""]
        self.input_buffer = ""
        self.cursor = 0
        self.history = HistoryStore().list_for(profile)
        self.history_index: int | None = None
        # The active composer uses the existing fields; the inactive composer
        # has independent draft, cursor, and history navigation state.
        self.active_input = 0
        self.other_input_buffer = ""
        self.other_cursor = 0
        self.other_history_index: int | None = None
        self.history_draft = ""
        self.other_history_draft = ""
        self.scroll_offset = 0
        self.status_message = ""
        self.modal_message = ""
        self.help_expanded = False
        self.secret_entry = False
        self.connected = True
        self.watch_rules: list[WatchRule] = []
        self.profile_config_path: Path | None = None
        self.lock = threading.RLock()
        self.started = False
        self.original_stdout_mode: int | None = None
        self._display_cache_width: int | None = None
        self._display_cache_rows: list[str] = []

    def _invalidate_display_cache(self) -> None:
        self._display_cache_width = None
        self._display_cache_rows = []

    def start(self) -> None:
        with self.lock:
            self._enable_vt()
            if not self.no_alt_screen:
                sys.stdout.write("\x1b[?1049h")
            self.started = True
            self.render(full_clear=True)

    def close(self) -> None:
        with self.lock:
            if self.started:
                sys.stdout.write("\x1b[?25h\x1b[0m")
                if not self.no_alt_screen:
                    sys.stdout.write("\x1b[?1049l")
                sys.stdout.flush()
            self.started = False
            if self.original_stdout_mode is not None and os.name == "nt":
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.GetStdHandle(-11)
                kernel32.SetConsoleMode(handle, self.original_stdout_mode)
                self.original_stdout_mode = None

    def _enable_vt(self) -> None:
        if os.name != "nt":
            return
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            self.original_stdout_mode = mode.value
            kernel32.SetConsoleMode(handle, mode.value | 0x0004 | 0x0008)

    def set_connected(self, value: bool) -> None:
        with self.lock:
            self.connected = value
            self.render()

    def set_secret_entry(self, value: bool) -> None:
        with self.lock:
            if value and self.active_input != 0:
                # Keep password entry in the primary buffer, preserving both drafts.
                self.input_buffer, self.other_input_buffer = self.other_input_buffer, self.input_buffer
                self.cursor, self.other_cursor = self.other_cursor, self.cursor
                self.history_index, self.other_history_index = self.other_history_index, self.history_index
                self.history_draft, self.other_history_draft = self.other_history_draft, self.history_draft
                self.active_input = 0
            self.secret_entry = value
            self.render()

    def set_status(self, message: str) -> None:
        with self.lock:
            self.status_message = message
            self.render()

    def set_modal(self, message: str) -> None:
        with self.lock:
            self.modal_message = message
            self.render()

    def clear_modal(self) -> None:
        with self.lock:
            self.modal_message = ""
            self.render()

    def add_watch(self, pattern: str, color: str) -> WatchRule:
        rule = make_watch_rule(pattern, color)
        with self.lock:
            self.watch_rules = [existing for existing in self.watch_rules if existing.pattern != rule.pattern]
            self.watch_rules.append(rule)
            self._invalidate_display_cache()
            self.render()
        return rule

    def remove_watch(self, pattern: str) -> int:
        clean_pattern = strip_outer_quotes(pattern)
        with self.lock:
            original_count = len(self.watch_rules)
            self.watch_rules = [rule for rule in self.watch_rules if rule.pattern != clean_pattern]
            removed = original_count - len(self.watch_rules)
            if removed:
                self._invalidate_display_cache()
                self.render()
            return removed

    def clear_watches(self) -> int:
        with self.lock:
            removed = len(self.watch_rules)
            self.watch_rules = []
            if removed:
                self._invalidate_display_cache()
                self.render()
            return removed

    def replace_watches(self, rules: list[WatchRule]) -> None:
        with self.lock:
            self.watch_rules = list(rules)
            self._invalidate_display_cache()
            self.render()

    def describe_watches(self) -> str:
        with self.lock:
            return describe_watch_rules(self.watch_rules)

    def append_output(self, text: str) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        with self.lock:
            parts = text.split("\n")
            for index, part in enumerate(parts):
                if index > 0:
                    self.lines.append("")
                self.lines[-1] += part
            if len(self.lines) > self.profile.max_scrollback:
                self.lines = self.lines[-self.profile.max_scrollback :]
            self._invalidate_display_cache()
            self.render()

    def _body_height(self) -> int:
        width, height = shutil.get_terminal_size((100, 30))
        input_rows, _, _ = self._input_window(width, height)
        return max(1, height - len(self._header_rows(width, height, len(input_rows))) - len(input_rows))

    def toggle_help(self) -> None:
        with self.lock:
            self.help_expanded = not self.help_expanded
            self.render()

    def _header_rows(self, width: int, height: int, input_count: int) -> list[str]:
        connection = "connected" if self.connected else "disconnected"
        log_state = "on" if self.profile.transcript != "off" else "off"
        status = (
            f" F1 {'close' if self.help_expanded else 'help'} | {self.profile.label()} "
            f"{self.profile.host}:{self.profile.port} TELNET {connection} log:{log_state}"
        )
        if self.scroll_offset:
            status += f" scroll:{self.scroll_offset}"
        if self.status_message:
            status += f" | {self.status_message}"
        if self.modal_message:
            status = f" {self.modal_message}"

        rows = [status]
        if self.help_expanded:
            help_lines = [
                " F1 close help | Tab switch input | Enter send | Ctrl+] quit",
                " Arrows move cursor | Ctrl+Left/Right words | Home/End start/end",
                " Ctrl+A/E start/end | Ctrl+W delete word | Ctrl+K delete to end",
                " Ctrl+U clear | Ctrl+N newline | Ctrl+V paste (confirm multiline)",
                " Up/Down scroll | PgUp/PgDn fast | Ctrl+Up/Down history",
                " F2 log path | F3 repeat | F4 bottom | F5 quick hints",
                " /log [file] | /watch PATTERN COLOR | /save FILE | /load [FILE]",
            ]
            # Always reserve one output line and the two command prompts.
            available = max(0, height - input_count - 2)
            rows.extend(help_lines[:available])
        return rows

    def _display_lines(self, width: int) -> list[str]:
        if self._display_cache_width == width:
            return self._display_cache_rows
        rows: list[str] = []
        rules = list(self.watch_rules)
        for line in self.lines:
            try:
                display_line = apply_watch_rules(line, rules)
                rows.extend(wrap_ansi(display_line, width))
            except Exception:
                rows.extend(wrap_ansi(strip_ansi(str(line)), width))
        self._display_cache_width = width
        self._display_cache_rows = rows or [""]
        return self._display_cache_rows

    @staticmethod
    def _visible_input_rows(layout: InputLayout, limit: int) -> tuple[list[str], int]:
        if len(layout.rows) <= limit:
            return layout.rows, layout.cursor_row
        start = min(max(0, layout.cursor_row - limit + 1), len(layout.rows) - limit)
        return layout.rows[start : start + limit], layout.cursor_row - start

    def _input_window(self, width: int, height: int) -> tuple[list[str], int, int]:
        # Both input fields use the same bare prompt; focus is shown by the
        # terminal cursor, without labels or a separator row.
        # Prevent a small terminal from consuming all output space.
        max_rows = max(2, min(8, height - 2))
        active_limit = max(1, max_rows // 2)
        other_limit = max(1, max_rows - active_limit)
        active = layout_input_buffer(
            self.input_buffer, self.cursor, width, "> ", self.secret_entry,
        )
        inactive = layout_input_buffer(
            self.other_input_buffer, self.other_cursor, width, "> ",
            self.secret_entry,
        )
        active_rows, active_cursor_row = self._visible_input_rows(active, active_limit)
        other_rows, other_cursor_row = self._visible_input_rows(inactive, other_limit)
        if self.active_input == 0:
            return active_rows + other_rows, active_cursor_row, active.cursor_col
        return other_rows + active_rows, len(other_rows) + active_cursor_row, active.cursor_col

    def switch_input(self) -> None:
        with self.lock:
            # Keep passwords in the primary composer; don't expose a draft
            # while a secret prompt is being answered.
            if self.secret_entry:
                self.set_status("Switching inputs is disabled during secret entry")
                return
            self.input_buffer, self.other_input_buffer = self.other_input_buffer, self.input_buffer
            self.cursor, self.other_cursor = self.other_cursor, self.cursor
            self.history_index, self.other_history_index = self.other_history_index, self.history_index
            self.history_draft, self.other_history_draft = self.other_history_draft, self.history_draft
            self.active_input = 1 - self.active_input
            self.render()

    def delete_to_end(self) -> None:
        with self.lock:
            self.input_buffer = self.input_buffer[:self.cursor]
            self.history_index = None
            self.render()

    def move_word(self, direction: int) -> None:
        with self.lock:
            text = self.input_buffer
            pos = self.cursor
            if direction < 0:
                while pos > 0 and text[pos - 1].isspace():
                    pos -= 1
                while pos > 0 and not text[pos - 1].isspace():
                    pos -= 1
            else:
                while pos < len(text) and not text[pos].isspace():
                    pos += 1
                while pos < len(text) and text[pos].isspace():
                    pos += 1
            self.cursor = pos
            self.render()

    def scroll(self, amount: int) -> None:
        with self.lock:
            width = shutil.get_terminal_size((100, 30)).columns
            max_offset = max(0, len(self._display_lines(width)) - self._body_height())
            new_offset = min(max_offset, max(0, self.scroll_offset + amount))
            if new_offset != self.scroll_offset:
                self.scroll_offset = new_offset
                self.render()

    def bottom(self) -> None:
        with self.lock:
            if self.scroll_offset == 0:
                return
            self.scroll_offset = 0
            self.render()

    def insert_text(self, text: str) -> None:
        with self.lock:
            self.input_buffer = self.input_buffer[: self.cursor] + text + self.input_buffer[self.cursor :]
            self.cursor += len(text)
            self.history_index = None
            self.render()

    def replace_input(self, text: str) -> None:
        with self.lock:
            self.input_buffer = text
            self.cursor = len(text)
            self.render()

    def clear_input(self) -> None:
        with self.lock:
            self.history_index = None
            self.history_draft = ""
        self.replace_input("")

    def backspace(self) -> None:
        with self.lock:
            if self.cursor > 0:
                self.input_buffer = self.input_buffer[: self.cursor - 1] + self.input_buffer[self.cursor :]
                self.cursor -= 1
            self.render()

    def delete(self) -> None:
        with self.lock:
            if self.cursor < len(self.input_buffer):
                self.input_buffer = self.input_buffer[: self.cursor] + self.input_buffer[self.cursor + 1 :]
            self.render()

    def delete_word(self) -> None:
        with self.lock:
            left = self.input_buffer[: self.cursor].rstrip()
            cut = len(left)
            while cut > 0 and not left[cut - 1].isspace():
                cut -= 1
            self.input_buffer = self.input_buffer[:cut] + self.input_buffer[self.cursor :]
            self.cursor = cut
            self.render()

    def move_cursor(self, amount: int) -> None:
        with self.lock:
            self.cursor = max(0, min(len(self.input_buffer), self.cursor + amount))
            self.render()

    def history_prev(self) -> None:
        with self.lock:
            if not self.history:
                self.set_status("No command history for this profile")
                return
            if self.history_index is None:
                self.history_draft = self.input_buffer
                self.history_index = len(self.history) - 1
            else:
                self.history_index = max(0, self.history_index - 1)
            self.replace_input(self.history[self.history_index])

    def history_next(self) -> None:
        with self.lock:
            if not self.history:
                return
            if self.history_index is None:
                return
            if self.history_index >= len(self.history) - 1:
                self.history_index = None
                self.replace_input(self.history_draft)
            else:
                self.history_index += 1
                self.replace_input(self.history[self.history_index])

    def remember(self, command: str) -> None:
        if self.secret_entry:
            return
        HistoryStore().add(self.profile, command)
        self.history = HistoryStore().list_for(self.profile)

    def render(self, full_clear: bool = False) -> None:
        if not self.started:
            return
        width, height = shutil.get_terminal_size((100, 30))
        input_rows, input_cursor_row, input_cursor_col = self._input_window(width, height)
        header_rows = self._header_rows(width, height, len(input_rows))
        body_height = max(1, height - len(header_rows) - len(input_rows))
        display_lines = self._display_lines(width)
        max_offset = max(0, len(display_lines) - body_height)
        self.scroll_offset = min(self.scroll_offset, max_offset)
        end = len(display_lines) - self.scroll_offset
        start = max(0, end - body_height)
        visible = display_lines[start:end]

        output = ["\x1b[?25l\x1b[H"]
        if full_clear:
            output.append("\x1b[2J")
        for header in header_rows:
            output.append("\x1b[2K\x1b[7m" + truncate_plain(header, width).ljust(width) + "\x1b[0m\n")

        for line in visible:
            output.append("\x1b[2K" + truncate_ansi(line, width) + "\x1b[0m\n")
        for _ in range(body_height - len(visible)):
            output.append("\x1b[2K\n")

        for index, row in enumerate(input_rows):
            output.append("\x1b[2K" + truncate_plain(row, width))
            if index < len(input_rows) - 1:
                output.append("\n")
        cursor_line = len(header_rows) + 1 + body_height + input_cursor_row
        output.append(
            f"\x1b[{max(1, min(height, cursor_line))};{max(1, min(width, input_cursor_col))}H"
        )
        output.append("\x1b[?25h")
        sys.stdout.write("".join(output))
        sys.stdout.flush()


def get_clipboard_text() -> str | None:
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            return root.clipboard_get()
        finally:
            root.destroy()
    except Exception:
        return None


class SessionController:
    def __init__(
        self,
        profile: Profile,
        transport: Transport,
        codec: TelnetCodec,
        transcript: Transcript,
        ui: MudUi,
        password: str | None,
        private_peer_required: bool = False,
    ) -> None:
        self.profile = profile
        self.private_peer_required = private_peer_required
        self.transport = transport
        self.codec = codec
        self.transcript = transcript
        self.ui = ui
        self.password = password
        self.tail = ""
        self.sent_username = False
        self.sent_password = False
        self.lock = threading.RLock()
        self.reconnect_lock = threading.Lock()
        self.last_io = time.monotonic()
        self.last_keepalive = 0.0

    def current_connection(self) -> tuple[Transport, TelnetCodec] | None:
        with self.lock:
            if self.transport is None:
                return None
            return self.transport, self.codec

    def install_connection(self, transport: Transport, codec: TelnetCodec) -> None:
        old_transport: Transport | None
        with self.lock:
            old_transport = self.transport
            self.transport = transport
            self.codec = codec
            self.tail = ""
            self.sent_username = False
            self.sent_password = False
            self.last_io = time.monotonic()
            self.last_keepalive = 0.0
        if old_transport is not None and old_transport is not transport:
            old_transport.close()
        self.ui.set_connected(True)

    def mark_disconnected(self, transport: Transport | None, message: str) -> bool:
        with self.lock:
            if transport is not None and self.transport is not transport:
                return False
            old_transport = self.transport
            self.transport = None
            self.codec = None
        if old_transport is not None:
            old_transport.close()
        self.ui.set_connected(False)
        if message:
            self.ui.set_status(message)
        return True

    def close(self) -> None:
        self.mark_disconnected(None, "")

    def send_raw(self, data: bytes, expected_transport: Transport | None = None) -> None:
        failed_transport: Transport | None = None
        failed_message = ""
        with self.lock:
            if self.transport is None:
                raise ConnectionUnavailable("not connected; reconnecting")
            if expected_transport is not None and self.transport is not expected_transport:
                raise ConnectionChanged("connection changed")
            try:
                self.transport.sendall(data)
            except OSError as exc:
                failed_transport = self.transport
                failed_message = str(exc)
                self.transport = None
                self.codec = None
            else:
                self.last_io = time.monotonic()
                return
        if failed_transport is not None:
            failed_transport.close()
            self.ui.set_connected(False)
            self.ui.set_status(f"Send failed: {failed_message}")
        raise OSError(failed_message)

    def maybe_send_keepalive(self) -> None:
        interval = self.profile.keepalive_interval
        if interval <= 0:
            return
        now = time.monotonic()
        with self.lock:
            if self.transport is None:
                return
            if now - self.last_io < interval or now - self.last_keepalive < interval:
                return
            self.last_keepalive = now
        try:
            self.send_raw(bytes([IAC, NOP]))
        except OSError:
            pass

    def send_line(self, line: str, secret: bool = False, remember: bool = True) -> None:
        outbound_lines = [line] if secret else split_outbound_buffers(line)
        for index, outbound_line in enumerate(outbound_lines):
            data = (outbound_line + self.profile.newline).encode(self.profile.encoding, "replace")
            self.send_raw(data)
            self.transcript.write("send", outbound_line, secret=secret)
            if index < len(outbound_lines) - 1:
                time.sleep(self.profile.paste_line_delay)
        if remember and not secret:
            self.ui.remember(line)
        if len(outbound_lines) > 1 and not secret:
            self.ui.set_status(f"Sent {len(outbound_lines)} buffers; continuations used spoof")

    def observe_output(self, text: str) -> None:
        self.tail = (self.tail + strip_ansi(text))[-2000:]
        if self._matches(self.profile.password_prompt) and not self.sent_password:
            self.ui.set_secret_entry(True)
            if self.profile.auto_login and self.password is not None:
                try:
                    self.send_line(self.password, secret=True, remember=False)
                except OSError as exc:
                    self.ui.set_status(f"Auto-login send failed: {exc}")
                    return
                self.sent_password = True
                self.ui.set_secret_entry(False)
                self.ui.set_status("Auto-login sent password")
            return
        if self._matches(self.profile.login_prompt) and not self.sent_username:
            if self.profile.auto_login and self.profile.username:
                try:
                    self.send_line(self.profile.username, secret=False, remember=False)
                except OSError as exc:
                    self.ui.set_status(f"Auto-login send failed: {exc}")
                    return
                self.sent_username = True
                self.ui.set_status("Auto-login sent username")

    def mark_manual_secret_sent(self) -> None:
        self.sent_password = True

    def _matches(self, pattern: str) -> bool:
        if not pattern:
            return False
        try:
            return re.search(pattern, self.tail) is not None
        except re.error:
            return False


def append_client_event(transcript: Transcript, ui: MudUi, message: str) -> None:
    plain = f"[client] {message}\n"
    transcript.write("recv", plain)
    ui.append_output(f"\x1b[33m{plain}\x1b[0m")


def confirm_local_command(transcript: Transcript, ui: MudUi, message: str) -> None:
    ui.set_status(message)
    append_client_event(transcript, ui, message)


def sleep_until_stop(stop: threading.Event, seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, seconds)
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.2, remaining))
    return True


def reconnect_session(
    stop: threading.Event,
    controller: SessionController,
    transcript: Transcript,
    reason: str,
) -> bool:
    profile = controller.profile
    ui = controller.ui
    if not profile.reconnect:
        append_client_event(transcript, ui, f"Disconnected: {reason}. Reconnect is disabled.")
        return False

    with controller.reconnect_lock:
        if stop.is_set():
            return False
        if controller.current_connection() is not None:
            return True
        target = connection_target_label(profile)
        append_client_event(transcript, ui, f"Disconnected: {reason}. Reconnecting to {target}.")
        delay = max(0.0, profile.reconnect_initial_delay)
        max_delay = max(delay, profile.reconnect_max_delay)
        attempt = 1
        while not stop.is_set():
            if delay > 0:
                ui.set_status(f"Disconnected; reconnecting in {delay:g}s")
                if sleep_until_stop(stop, delay):
                    return False
            ui.set_status(f"Reconnecting to {target} (attempt {attempt})")
            transport = None
            try:
                transport, codec, initial_banner = open_session_transport(
                    profile, require_private_peer=controller.private_peer_required
                )
                run_sanity_checks(profile, initial_banner, False, False, interactive=False)
            except Exception as exc:
                if transport is not None:
                    transport.close()
                ui.set_status(f"Reconnect failed: {exc}")
                attempt += 1
                delay = min(max_delay, max(1.0, delay * 2 if delay else 1.0))
                continue
            if stop.is_set():
                transport.close()
                return False
            controller.install_connection(transport, codec)
            append_client_event(transcript, ui, f"Reconnected to {target}.")
            if initial_banner:
                transcript.write("recv", initial_banner)
                ui.append_output(initial_banner)
                controller.observe_output(initial_banner)
            return True
    return False


def receiver_loop(
    stop: threading.Event,
    transcript: Transcript,
    controller: SessionController,
) -> None:
    decoder: TerminalStreamDecoder | None = None
    active_transport: Transport | None = None
    while not stop.is_set():
        connection = controller.current_connection()
        if connection is None:
            if not reconnect_session(stop, controller, transcript, "connection unavailable"):
                break
            continue
        transport, codec = connection
        if transport is not active_transport:
            active_transport = transport
            decoder = TerminalStreamDecoder(controller.profile.encoding)
        try:
            data = transport.recv(4096)
        except OSError as exc:
            reason = f"Receive error: {exc}"
            controller.mark_disconnected(transport, reason)
            if not reconnect_session(stop, controller, transcript, reason):
                break
            continue
        if not data:
            reason = "Remote closed the connection"
            controller.mark_disconnected(transport, reason)
            if not reconnect_session(stop, controller, transcript, reason):
                break
            continue
        output, response = codec.feed(data)
        if response:
            try:
                controller.send_raw(response, expected_transport=transport)
            except ConnectionChanged:
                continue
            except OSError as exc:
                reason = f"Negotiation send error: {exc}"
                controller.mark_disconnected(transport, reason)
                if not reconnect_session(stop, controller, transcript, reason):
                    break
                continue
        if output:
            assert decoder is not None
            text = decoder.feed(output)
            transcript.write("recv", text)
            controller.ui.append_output(text)
            controller.observe_output(text)
    if not stop.is_set():
        stop.set()
    controller.ui.set_connected(False)


def handle_paste(ui: MudUi, controller: SessionController) -> None:
    text = get_clipboard_text()
    if text is None:
        ui.set_status("Clipboard text unavailable")
        return
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\n" not in text:
        ui.insert_text(text)
        return
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return
    ui.set_modal(f"Paste {len(lines)} lines to the game? y/n")
    reader = KeyReader()
    try:
        approved = False
        while True:
            key = reader.read_key(timeout=0.2)
            if key is None:
                continue
            if key.char.lower() == "y":
                approved = True
                break
            if key.char.lower() == "n" or key.name in {"quit", "escape"}:
                break
    finally:
        reader.close()
        ui.clear_modal()
    if not approved:
        ui.set_status("Paste cancelled")
        return
    first = True
    prefix = ui.input_buffer
    ui.clear_input()
    for index, line in enumerate(lines):
        to_send = (prefix + line) if first else line
        first = False
        try:
            controller.send_line(to_send, secret=False, remember=True)
        except OSError as exc:
            remaining = [to_send] + lines[index + 1 :]
            ui.replace_input("\n".join(remaining))
            ui.set_status(f"Paste stopped; input kept while reconnecting: {exc}")
            return
        time.sleep(controller.profile.paste_line_delay)
    ui.set_status(f"Pasted {len(lines)} lines")


def handle_watch_command(line: str, transcript: Transcript, ui: MudUi) -> bool:
    if line != "/watch" and not line.startswith("/watch "):
        return False
    argument = line[6:].strip()
    lowered = argument.lower()
    if not argument or lowered in {"list", "status", "?"}:
        confirm_local_command(transcript, ui, ui.describe_watches())
        return True
    if lowered in {"clear", "off"}:
        removed = ui.clear_watches()
        confirm_local_command(transcript, ui, f"Cleared {removed} watch rule" + ("" if removed == 1 else "s"))
        return True
    if lowered.startswith("remove "):
        pattern = strip_outer_quotes(argument[7:].strip())
        if not pattern:
            confirm_local_command(transcript, ui, "Watch error: pattern must not be empty")
            return True
        removed = ui.remove_watch(pattern)
        confirm_local_command(transcript, ui, f"Removed watch: {pattern}" if removed else f"No watch matched: {pattern}")
        return True
    try:
        pattern, color = parse_watch_arguments(argument)
        rule = ui.add_watch(pattern, color)
    except ValueError as exc:
        confirm_local_command(transcript, ui, f"Watch error: {exc}")
    else:
        confirm_local_command(transcript, ui, f"Watching {rule.pattern} as {rule.color}")
    return True


def handle_session_profile_command(line: str, transcript: Transcript, ui: MudUi) -> bool:
    if line != "/save" and not line.startswith("/save ") and line != "/load" and not line.startswith("/load "):
        return False
    if line == "/save" or line.startswith("/save "):
        argument = line[5:].strip()
        if not argument:
            confirm_local_command(transcript, ui, "Save error: usage: /save file")
            return True
        try:
            path = resolve_session_profile_path(argument)
            save_session_profile(path, ui.profile, transcript, ui)
        except (OSError, ValueError) as exc:
            confirm_local_command(transcript, ui, f"Save error: {exc}")
        else:
            ui.profile_config_path = path
            confirm_local_command(transcript, ui, f"Saved profile: {path}")
        return True
    try:
        argument = line[5:].strip()
        if argument:
            path = resolve_session_profile_path(argument)
        elif ui.profile_config_path is not None:
            path = ui.profile_config_path
        else:
            confirm_local_command(transcript, ui, "Load error: usage: /load file")
            return True
        data = load_session_profile(path)
        apply_session_profile(data, ui.profile, transcript, ui)
    except (OSError, ValueError) as exc:
        confirm_local_command(transcript, ui, f"Load error: {exc}")
    else:
        ui.profile_config_path = path
        confirm_local_command(transcript, ui, f"Loaded profile: {path}")
    return True


def handle_local_command(line: str, transcript: Transcript, ui: MudUi) -> bool:
    if handle_watch_command(line, transcript, ui):
        return True
    if handle_session_profile_command(line, transcript, ui):
        return True
    if line != "/log" and not line.startswith("/log "):
        return False
    argument = line[4:].strip()
    lowered = argument.lower()
    if lowered in {"off", "stop", "close"}:
        if transcript.path:
            previous = transcript.path
            transcript.disable()
            confirm_local_command(transcript, ui, f"Logging stopped: {previous}")
        else:
            confirm_local_command(transcript, ui, "Logging is already off")
        return True
    if lowered in {"status", "path", "?"}:
        confirm_local_command(transcript, ui, f"Log: {transcript.path or 'off'}")
        return True
    try:
        path = transcript.enable_text_log(argument or None)
    except (OSError, ValueError) as exc:
        confirm_local_command(transcript, ui, f"Log error: {exc}")
    else:
        confirm_local_command(transcript, ui, f"Logging to {path}")
    return True


def split_composed_input(text: str) -> list[str]:
    lines = text.split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()
    return lines


def send_composed_input(
    text: str,
    secret: bool,
    transcript: Transcript,
    ui: MudUi,
    controller: SessionController,
) -> bool:
    if secret:
        try:
            controller.send_line(text, secret=True, remember=False)
        except OSError as exc:
            ui.set_status(f"Input kept while reconnecting: {exc}")
            return False
        return True
    lines = split_composed_input(text)
    sent = 0
    for index, line in enumerate(lines):
        if handle_local_command(line, transcript, ui):
            continue
        try:
            controller.send_line(line, secret=False, remember=True)
        except OSError as exc:
            ui.set_status(f"Input kept while reconnecting: {exc}")
            return False
        sent += 1
        if index < len(lines) - 1:
            time.sleep(controller.profile.paste_line_delay)
    if sent and len(lines) > 1:
        ui.set_status(f"Sent {sent} lines")
    return True


def session_loop(
    profile: Profile, args: argparse.Namespace,
    on_connected: Callable[[Profile], None] | None = None,
) -> None:
    if not profile.allow_plaintext:
        raise SystemExit("Refusing plaintext Telnet until --allow-plaintext is set on the profile or command.")
    private_peer_required = False
    public_approval_given = False
    if profile.require_private_host and not profile.proxy_command and not profile.socks5_host:
        private_peer_required = is_private_target(profile.host)
        if not private_peer_required:
            # Approval is for this exact host and TCP port, not the domain suffix.
            endpoint = f"{profile.host.rstrip('.').casefold()}:{profile.port}"
            if profile.approved_public_endpoint != endpoint:
                ok = prompt_yes_no(
                    f"{profile.host} does not resolve only to private/link-local addresses. Continue with plaintext Telnet?",
                    default=False,
                    assume_yes=args.yes,
                )
                if not ok:
                    raise SystemExit("Connection aborted by private-host policy")
                public_approval_given = True

    password: str | None = None
    if profile.auto_login:
        password = SecretStore().get_password(profile.label())
        if profile.username and password is None:
            password = getpass.getpass(f"Password for {profile.label()} auto-login: ")

    try:
        transport, codec, initial_banner = open_session_transport(
            profile, require_private_peer=private_peer_required
        )
    except OSError as exc:
        raise SystemExit(f"Could not connect to {connection_target_label(profile)}: {exc}") from exc
    try:
        run_sanity_checks(profile, initial_banner, args.accept_host_change, args.yes)
        if public_approval_given:
            profile.approved_public_endpoint = f"{profile.host.rstrip('.').casefold()}:{profile.port}"
        if on_connected is not None:
            on_connected(profile)
    except BaseException:
        transport.close()
        raise

    transcript = Transcript(profile)
    ui = MudUi(profile, no_alt_screen=args.no_alt_screen)
    controller = SessionController(
        profile, transport, codec, transcript, ui, password,
        private_peer_required=private_peer_required,
    )
    stop = threading.Event()
    receiver = threading.Thread(
        target=receiver_loop,
        args=(stop, transcript, controller),
        daemon=True,
    )
    reader: KeyReader | None = None

    try:
        reader = KeyReader()
        ui.start()
        if initial_banner:
            transcript.write("recv", initial_banner)
            ui.append_output(initial_banner)
            controller.observe_output(initial_banner)
        receiver.start()
        while not stop.is_set():
            controller.maybe_send_keepalive()
            key = reader.read_key(timeout=0.1)
            if key is None:
                continue
            if key.name == "quit":
                stop.set()
                break
            if key.name == "redraw":
                ui.render(full_clear=True)
            elif key.name == "up":
                ui.scroll(1)
            elif key.name == "down":
                ui.scroll(-1)
            elif key.name == "page_up":
                ui.scroll(fast_page_scroll_amount(ui._body_height()))
            elif key.name == "page_down":
                ui.scroll(-fast_page_scroll_amount(ui._body_height()))
            elif key.name == "switch_input":
                ui.switch_input()
            elif key.name == "ctrl_left":
                ui.move_word(-1)
            elif key.name == "ctrl_right":
                ui.move_word(1)
            elif key.name == "delete_to_end":
                ui.delete_to_end()
            elif key.name == "ctrl_up":
                ui.history_prev()
            elif key.name == "ctrl_down":
                ui.history_next()
            elif key.name == "left":
                ui.move_cursor(-1)
            elif key.name == "right":
                ui.move_cursor(1)
            elif key.name == "home":
                ui.cursor = 0
                ui.render()
            elif key.name == "end":
                ui.cursor = len(ui.input_buffer)
                ui.render()
            elif key.name == "delete":
                ui.delete()
            elif key.name == "backspace":
                ui.backspace()
            elif key.name == "clear_line":
                ui.clear_input()
            elif key.name == "delete_word":
                ui.delete_word()
            elif key.name == "paste":
                handle_paste(ui, controller)
            elif key.name == "f1":
                ui.toggle_help()
            elif key.name == "f2":
                ui.set_status(f"Log: {transcript.path or 'off'}")
            elif key.name == "f3":
                if ui.history:
                    try:
                        controller.send_line(ui.history[-1], secret=False, remember=False)
                    except OSError as exc:
                        ui.set_status(f"Repeat skipped while reconnecting: {exc}")
                    else:
                        ui.set_status("Repeated last command")
            elif key.name == "f4":
                ui.bottom()
            elif key.name == "f5":
                ui.set_status(
                    "Keys: TAB switch buffer, CTRL+LEFT/RIGHT move words, CTRL+K delete to end, "
                    "UP/DOWN scroll, CTRL+UP/DOWN history, CTRL+N newline, CTRL+V paste, "
                    "/save FILE, /load [FILE], CTRL+] quit"
                )
            elif key.name == "newline":
                if ui.secret_entry:
                    ui.set_status("Newline is disabled for secret entry")
                else:
                    ui.insert_text("\n")
            elif key.name == "enter":
                line = ui.input_buffer
                secret = ui.secret_entry
                ui.bottom()
                if line or secret:
                    if not send_composed_input(line, secret, transcript, ui, controller):
                        continue
                    ui.clear_input()
                if secret:
                    controller.mark_manual_secret_sent()
                    ui.set_secret_entry(False)
            elif key.name == "char":
                ui.insert_text(key.char)
    finally:
        stop.set()
        controller.close()
        if reader is not None:
            reader.close()
        ui.close()
        if transcript.path:
            print(f"Log: {transcript.path}")


def apply_connect_overrides(profile: Profile, args: argparse.Namespace) -> Profile:
    for field_name in (
        "expected_banner",
        "username",
        "transcript",
        "terminal_type",
        "sanity_timeout",
        "keepalive_interval",
        "reconnect_initial_delay",
        "reconnect_max_delay",
    ):
        value = getattr(args, field_name, None)
        if value is not None:
            setattr(profile, field_name, value)
    host = getattr(args, "host", None)
    port = getattr(args, "port", None)
    if host is not None or port is not None:
        try:
            profile.host, profile.port = normalize_host_port(
                host if host is not None else profile.host,
                port if port is not None else profile.port,
                explicit_port=port is not None,
            )
        except HostPortError as exc:
            raise SystemExit(str(exc)) from exc
    if getattr(args, "allow_plaintext", False):
        profile.allow_plaintext = True
    if getattr(args, "allow_public", False):
        profile.require_private_host = False
    if getattr(args, "auto_login", False):
        profile.auto_login = True
    if getattr(args, "no_auto_login", False):
        profile.auto_login = False
    if getattr(args, "no_reconnect", False):
        profile.reconnect = False
    if getattr(args, "socks5", None):
        try:
            host, port = parse_host_port(args.socks5)
        except HostPortError as exc:
            raise SystemExit(f"Invalid --socks5 value: {exc}") from exc
        profile.socks5_host = host
        profile.socks5_port = port
    if getattr(args, "proxy_command", None):
        profile.proxy_command = args.proxy_command
    return profile


def profile_from_args(args: argparse.Namespace, store: ProfileStore | None = None) -> Profile:
    store = store or ProfileStore()
    if args.profile:
        if args.profile in store.data["profiles"]:
            profile = store.get(args.profile)
        else:
            # A unique hostname can also access a saved profile by FQDN.
            profile = store.find_host(args.profile)
            if profile is None and "." not in args.profile and ":" not in args.profile:
                profile = store.find_host(args.profile, short=True)
            if profile is None:
                raise SystemExit(f"Profile not found or ambiguous host alias: {args.profile}")
    else:
        if not args.host:
            raise SystemExit("connect requires a profile name or --host")
        try:
            host, port = normalize_host_port(args.host, args.port, explicit_port=args.port is not None)
        except HostPortError as exc:
            raise SystemExit(str(exc)) from exc
        specified_port = args.port is not None or split_optional_host_port(args.host)[1] is not None
        profile = store.find_host(host, port if specified_port else None)
        if profile is None:
            name = store.auto_profile_name(host, port)
            profile = Profile(name=name, host=host, port=port)
    return apply_connect_overrides(profile, args)


def command_connect(args: argparse.Namespace) -> None:
    store = ProfileStore()
    profile = profile_from_args(args, store)
    original = store.data["profiles"].get(profile.name)

    # Successful sessions persist their hostname profile. Never overwrite a
    # manually named profile when --host/--port temporarily overrides its target.
    def remember(connected: Profile) -> None:
        if original is not None:
            old_host = original.get("host", "").rstrip(".").casefold()
            if old_host != connected.host.rstrip(".").casefold() or original.get("port", DEFAULT_PORT) != connected.port:
                return
        if getattr(args, "allow_public", False) and not connected.proxy_command and not connected.socks5_host:
            # A one-off --allow-public acknowledgement must not disable future
            # checks for unrelated endpoints when saved automatically.
            connected.approved_public_endpoint = f"{connected.host.rstrip('.').casefold()}:{connected.port}"
            if original is None:
                connected.require_private_host = True
        store.set(connected)

    session_loop(profile, args, on_connected=remember)


def command_profile_list(_args: argparse.Namespace) -> None:
    store = ProfileStore()
    if not store.names():
        print(f"No profiles yet. Config path: {store.path}")
        return
    for name in store.names():
        profile = store.get(name)
        wrapper = "proxy" if profile.proxy_command else ("socks5" if profile.socks5_host else "direct")
        private = "private-only" if profile.require_private_host else "public-allowed"
        print(f"{name}\t{profile.host}:{profile.port}\t{wrapper}\t{private}\tlog:{profile.transcript}")


def command_profile_show(args: argparse.Namespace) -> None:
    profile = ProfileStore().get(args.name)
    print(compact_json(profile.to_stored_dict()))


def command_profile_remove(args: argparse.Namespace) -> None:
    ProfileStore().remove(args.name)
    print(f"Removed profile {args.name}")


def command_profile_add(args: argparse.Namespace) -> None:
    if not args.allow_plaintext:
        raise SystemExit("Use --allow-plaintext to acknowledge that this profile uses insecure Telnet.")
    try:
        host, port = normalize_host_port(args.host, args.port, explicit_port=args.port is not None)
    except HostPortError as exc:
        raise SystemExit(str(exc)) from exc
    profile = Profile(
        name=args.name,
        host=host,
        port=port,
        allow_plaintext=True,
        require_private_host=not args.allow_public,
        expected_banner=args.expected_banner or "",
        username=args.username or "",
        auto_login=args.auto_login,
        transcript=args.transcript,
        terminal_type=args.terminal_type,
        keepalive_interval=args.keepalive_interval,
        reconnect=not args.no_reconnect,
        reconnect_initial_delay=args.reconnect_initial_delay,
        reconnect_max_delay=args.reconnect_max_delay,
    )
    if args.socks5:
        try:
            profile.socks5_host, profile.socks5_port = parse_host_port(args.socks5)
        except HostPortError as exc:
            raise SystemExit(f"Invalid --socks5 value: {exc}") from exc
    if args.proxy_command:
        profile.proxy_command = args.proxy_command
    ProfileStore().set(profile)
    print(f"Saved profile {profile.name} in {profile_path()}")


def command_secret_set(args: argparse.Namespace) -> None:
    if not Dpapi.available():
        raise SystemExit("Stored secrets require Windows DPAPI")
    password = args.password or getpass.getpass(f"Password for {args.profile}: ")
    SecretStore().set_password(args.profile, password)
    print(f"Stored DPAPI-protected password for {args.profile}")


def command_secret_clear(args: argparse.Namespace) -> None:
    SecretStore().clear_password(args.profile)
    print(f"Cleared password for {args.profile}")


def command_history_clear(args: argparse.Namespace) -> None:
    HistoryStore().clear(args.profile)
    print(f"Cleared history for {args.profile}")


def command_transcript_decrypt(args: argparse.Namespace) -> None:
    decrypt_transcript(Path(args.file), Path(args.out) if args.out else None)


def command_paths(_args: argparse.Namespace) -> None:
    print(f"config_dir: {config_dir()}")
    print(f"profiles: {profile_path()}")
    print(f"known_hosts: {known_hosts_path()}")
    print(f"history: {history_path()}")
    print(f"secrets: {secrets_path()}")
    print(f"transcripts: {transcript_dir()}")


def command_self_test(_args: argparse.Namespace) -> None:
    codec = TelnetCodec()
    output, response = codec.feed(bytes([IAC, DO, TELNET_NAWS]) + b"hello" + bytes([IAC, WILL, TELNET_ECHO]))
    assert output == b"hello", output
    assert bytes([IAC, WILL, TELNET_NAWS]) in response
    assert bytes([IAC, DO, TELNET_ECHO]) in response

    codec = TelnetCodec("XTERM")
    _output, response = codec.feed(bytes([IAC, SB, TELNET_TTYPE, TTYPE_SEND, IAC, SE]))
    assert b"XTERM" in response

    dangerous = "\x1b]52;c;AAAA\x07safe \x1b[31mred\x1b[0m \x1b[2Jclear"
    sanitized = sanitize_terminal_text(dangerous)
    assert "\x1b]52" not in sanitized
    assert "\x1b[31m" in sanitized
    assert "\x1b[2J" not in sanitized


    # Fragmentation must not leak terminal controls or corrupt UTF-8.
    for split in range(1, len(b"hello\x1b]52;c;secret\x1b\\world")):
        payload = b"hello\x1b]52;c;secret\x1b\\world"
        dec = TerminalStreamDecoder("utf-8")
        assert dec.feed(payload[:split]) + dec.feed(payload[split:]) == "helloworld"
    dec = TerminalStreamDecoder("utf-8")
    unicode_bytes = "café".encode("utf-8")
    assert dec.feed(unicode_bytes[:-1]) == "caf"
    assert dec.feed(unicode_bytes[-1:]) == "é"
    dec = TerminalStreamDecoder("utf-8")
    assert dec.feed(b"red\x1b[31") == "red"
    assert dec.feed(b"mtext\x1b[2J") == "\x1b[31mtext"
    codec = TelnetCodec()
    payload = bytes([IAC, SB, TELNET_TTYPE, TTYPE_SEND]) + b"x" * (MAX_TELNET_SUBNEGOTIATION_BYTES + 1)
    for part in (payload[:7], payload[7:], bytes([IAC, SE]) + b"ok"):
        output, response = codec.feed(part)
    assert output == b"ok" and response == b""
    assert codec.sb_data == bytearray() and not codec.sb_overflow
    codec = TelnetCodec()
    output, response = codec.feed(bytes([IAC, SB, TELNET_TTYPE, TTYPE_SEND, IAC, SE]))
    assert b"ANSI" in response and output == b""

    wrapped = wrap_ansi("abcdef", 3)
    assert wrapped == ["abc", "def"], wrapped
    colored = wrap_ansi("\x1b[31mred words\x1b[0m", 5)
    assert [strip_ansi(line) for line in colored] == ["red w", "ords"], colored
    assert colored[1].startswith("\x1b[31m"), colored
    assert key_from_control_char("\n").name == "enter"
    assert key_from_control_char("\x7f").name == "backspace"
    assert key_from_control_char("\x0e").name == "newline"
    assert key_from_control_char("\x16").name == "paste"
    assert key_from_ansi_escape("\x1b[A").name == "up"
    assert key_from_ansi_escape("\x1b[1;5A").name == "ctrl_up"
    assert key_from_ansi_escape("\x1b[5~").name == "page_up"
    assert key_from_ansi_escape("\x1bOQ").name == "f2"
    assert fast_page_scroll_amount(20) == 57
    assert fast_page_scroll_amount(1) == 1
    input_layout = layout_input_buffer("abcdef", 6, 6, "> ")
    assert input_layout.rows == ["> abcd", "  ef"], input_layout
    assert (input_layout.cursor_row, input_layout.cursor_col) == (1, 5), input_layout
    boundary_layout = layout_input_buffer("abcde", 4, 6, "> ")
    assert boundary_layout.rows == ["> abcd", "  e"], boundary_layout
    assert (boundary_layout.cursor_row, boundary_layout.cursor_col) == (1, 3), boundary_layout
    multiline_layout = layout_input_buffer("look\nsay hi", 5, 20, "> ")
    assert multiline_layout.rows == ["> look", "  say hi"], multiline_layout
    assert (multiline_layout.cursor_row, multiline_layout.cursor_col) == (1, 3), multiline_layout
    assert split_composed_input("one\ntwo\n") == ["one", "two"]
    assert split_outbound_buffers("a" * OUTBOUND_BUFFER_CHARS) == ["a" * OUTBOUND_BUFFER_CHARS]
    assert split_outbound_buffers("a" * (OUTBOUND_BUFFER_CHARS + 1)) == [
        "a" * OUTBOUND_BUFFER_CHARS,
        OUTBOUND_SPOOF_PREFIX + "a",
    ]
    long_buffers = split_outbound_buffers("a" * (OUTBOUND_BUFFER_CHARS + 1019))
    assert len(long_buffers) == 3
    assert all(len(buffer) <= OUTBOUND_BUFFER_CHARS for buffer in long_buffers)
    assert long_buffers[1] == OUTBOUND_SPOOF_PREFIX + ("a" * 1018)
    assert long_buffers[2] == OUTBOUND_SPOOF_PREFIX + "a"
    plain_watch = make_watch_rule("You", "cyan")
    assert plain_watch.matches("You see a terminal")
    assert plain_watch.matches("You")
    assert not plain_watch.matches("Then You see a terminal")
    assert apply_watch_rules("You see a terminal", [plain_watch]).startswith("\x1b[36m")
    wildcard_watch = make_watch_rule("You*door", "bright-yellow")
    assert wildcard_watch.matches("You see a door")
    assert wildcard_watch.matches("You door")
    assert not wildcard_watch.matches("They say You see a door")
    assert parse_watch_arguments('"You say" red') == ("You say", "red")
    try:
        make_watch_rule("", "red")
        raise AssertionError("empty watch pattern should fail")
    except ValueError:
        pass
    try:
        make_watch_rule("You", "not-a-color")
        raise AssertionError("unknown watch color should fail")
    except ValueError:
        pass
    cache_profile = Profile(name="cache-test", host="127.0.0.1", allow_plaintext=True)
    cache_ui = MudUi(cache_profile)
    cache_ui.lines = ["abcdef"]
    cached_rows = cache_ui._display_lines(3)
    assert cached_rows == ["abc", "def"], cached_rows
    assert cache_ui._display_lines(3) is cached_rows
    cache_ui.append_output("ghi")
    refreshed_rows = cache_ui._display_lines(3)
    assert refreshed_rows is not cached_rows
    assert refreshed_rows == ["abc", "def", "ghi"], refreshed_rows
    watched_cache_ui = MudUi(cache_profile)
    watched_cache_ui.lines = ["You see a terminal"]
    watched_cache_ui._display_lines(40)
    watched_cache_ui.add_watch("You", "cyan")
    assert watched_cache_ui._display_lines(40)[0].startswith("\x1b[36m")

    profile = Profile(name="test", host="127.0.0.1", allow_plaintext=True)
    encoded = profile.to_stored_dict()
    decoded = Profile.from_dict("test", encoded)
    assert decoded.host == profile.host
    assert decoded.allow_plaintext is True
    assert decoded.transcript == "off"
    assert decoded.sanity_timeout == 0.0
    assert decoded.keepalive_interval == DEFAULT_KEEPALIVE_INTERVAL
    assert decoded.reconnect is True
    assert decoded.reconnect_initial_delay == DEFAULT_RECONNECT_INITIAL_DELAY
    assert decoded.reconnect_max_delay == DEFAULT_RECONNECT_MAX_DELAY


    strict_profile = Profile(name="strict", host="127.0.0.1", expected_banner="login", sanity_timeout=0.1)
    with tempfile.TemporaryDirectory() as sanity_dir:
        with mock.patch(__name__ + ".known_hosts_path", return_value=Path(sanity_dir) / "known.json"):
            run_sanity_checks(strict_profile, "login", False, False, interactive=False)
            for bad_banner in ("", "wrong"):
                try:
                    run_sanity_checks(strict_profile, bad_banner, False, False, interactive=False)
                    raise AssertionError("Reconnect accepted an invalid banner")
                except ValueError:
                    pass
            try:
                run_sanity_checks(strict_profile, "login changed", False, False, interactive=False)
                raise AssertionError("Reconnect accepted changed host banner")
            except ValueError:
                pass

    assert prompt_yes_no("continue", default=True, input_func=lambda _message: "") is True

    def raise_eof(_message: str) -> str:
        raise EOFError

    assert prompt_yes_no("continue", default=False, input_func=raise_eof) is False

    assert resolve_log_path(None).name == DEFAULT_LOG_FILE
    assert resolve_log_path('"named file.log"').name == "named file.log"
    with tempfile.TemporaryDirectory() as temp_dir:
        previous_cwd = os.getcwd()
        os.chdir(temp_dir)
        try:
            log_profile = Profile(name="log-test", host="127.0.0.1", allow_plaintext=True)
            transcript = Transcript(log_profile)
            assert transcript.path is None
            transcript.write("recv", "ignored")
            assert not Path(DEFAULT_LOG_FILE).exists()
            path = transcript.enable_text_log(None)
            assert path == Path(DEFAULT_LOG_FILE)
            transcript.write("recv", "\x1b[31mhello\x1b[0m\n")
            transcript.write("send", "look")
            text = path.read_text(encoding="utf-8")
            assert "hello" in text
            assert "\x1b[31m" not in text
            assert "> look" in text
            transcript.disable()
            assert log_profile.transcript == "off"

            class FakeUi:
                def __init__(self, profile: Profile = log_profile) -> None:
                    self.profile = profile
                    self.status = ""
                    self.connected = True
                    self.remembered: list[str] = []
                    self.watch_rules: list[WatchRule] = []
                    self.profile_config_path: Path | None = None
                    self.lines = [""]
                    self.lock = threading.RLock()

                def set_status(self, text: str) -> None:
                    self.status = text

                def set_connected(self, connected: bool) -> None:
                    self.connected = connected

                def remember(self, line: str) -> None:
                    self.remembered.append(line)

                def append_output(self, text: str) -> None:
                    self.lines.append(text)

                def add_watch(self, pattern: str, color: str) -> WatchRule:
                    rule = make_watch_rule(pattern, color)
                    self.watch_rules = [existing for existing in self.watch_rules if existing.pattern != rule.pattern]
                    self.watch_rules.append(rule)
                    return rule

                def remove_watch(self, pattern: str) -> int:
                    clean_pattern = strip_outer_quotes(pattern)
                    original_count = len(self.watch_rules)
                    self.watch_rules = [rule for rule in self.watch_rules if rule.pattern != clean_pattern]
                    return original_count - len(self.watch_rules)

                def clear_watches(self) -> int:
                    removed = len(self.watch_rules)
                    self.watch_rules = []
                    return removed

                def replace_watches(self, rules: list[WatchRule]) -> None:
                    self.watch_rules = list(rules)

                def describe_watches(self) -> str:
                    return describe_watch_rules(self.watch_rules)

            ui = FakeUi()
            assert handle_local_command("/look", transcript, ui) is False
            assert handle_local_command("/watch You say cyan", transcript, ui) is True
            assert len(ui.watch_rules) == 1
            assert ui.watch_rules[0].pattern == "You say"
            assert apply_watch_rules("You say hello", ui.watch_rules).startswith("\x1b[36m")
            assert handle_local_command("/watch", transcript, ui) is True
            assert "You say=cyan" in ui.status
            assert any("[client] Watches: You say=cyan" in line for line in ui.lines)
            assert handle_local_command("/watch remove You say", transcript, ui) is True
            assert not ui.watch_rules
            assert handle_local_command("/watch * red", transcript, ui) is True
            assert apply_watch_rules("anything", ui.watch_rules).startswith("\x1b[31m")
            assert handle_local_command("/watch clear", transcript, ui) is True
            assert not ui.watch_rules
            assert handle_local_command("/save", transcript, ui) is True
            assert "usage" in ui.status
            assert handle_local_command("/watch You cyan", transcript, ui) is True
            ui.profile.paste_line_delay = 0.25
            assert handle_local_command("/save session-profile.json", transcript, ui) is True
            assert ui.profile_config_path == Path("session-profile.json")
            saved = load_json(Path("session-profile.json"), {})
            assert saved["format"] == SESSION_PROFILE_FORMAT
            assert saved["watch_rules"] == [{"color": "cyan", "pattern": "You"}]
            assert saved["runtime_settings"]["paste_line_delay"] == 0.25
            ui.clear_watches()
            ui.profile.paste_line_delay = 0.01
            assert handle_local_command("/load", transcript, ui) is True
            assert ui.profile.paste_line_delay == 0.25
            assert len(ui.watch_rules) == 1
            assert ui.watch_rules[0].matches("You see a gate")
            assert handle_local_command("/load missing-profile.json", transcript, ui) is True
            assert ui.status.startswith("Load error:")
            assert handle_local_command("/log named.log", transcript, ui) is True
            assert transcript.path == Path("named.log")
            transcript.write("recv", "named")
            assert "named" in Path("named.log").read_text(encoding="utf-8")
            assert handle_local_command("/log off", transcript, ui) is True
            assert transcript.path is None

            class CaptureTransport(Transport):
                def __init__(self) -> None:
                    self.sent: list[bytes] = []

                def sendall(self, data: bytes) -> None:
                    self.sent.append(data)

                def recv(self, size: int) -> bytes:
                    return b""

                def close(self) -> None:
                    pass

            split_profile = Profile(name="split-test", host="127.0.0.1", allow_plaintext=True)
            split_profile.paste_line_delay = 0
            split_transcript = Transcript(split_profile)
            split_ui = FakeUi(split_profile)
            split_transport = CaptureTransport()
            split_controller = SessionController(
                split_profile,
                split_transport,
                TelnetCodec(split_profile.terminal_type),
                split_transcript,
                split_ui,
                None,
            )
            split_controller.send_line("a" * (OUTBOUND_BUFFER_CHARS + 1), secret=False, remember=True)
            assert split_transport.sent == [
                (("a" * OUTBOUND_BUFFER_CHARS) + split_profile.newline).encode(split_profile.encoding),
                (OUTBOUND_SPOOF_PREFIX + "a" + split_profile.newline).encode(split_profile.encoding),
            ]
            assert split_ui.remembered == ["a" * (OUTBOUND_BUFFER_CHARS + 1)]
            assert "2 buffers" in split_ui.status

            class FailingController:
                profile = log_profile

                def send_line(self, line: str, secret: bool = False, remember: bool = True) -> None:
                    raise OSError("offline")

            assert send_composed_input("look", False, transcript, ui, FailingController()) is False
            assert "reconnecting" in ui.status
        finally:
            os.chdir(previous_cwd)

    host, port = normalize_host_port("tapestries.fur.com:2069", None)
    assert host == "tapestries.fur.com"
    assert port == 2069
    host, port = normalize_host_port("[::1]:4000", None)
    assert host == "::1"
    assert port == 4000
    host, port = normalize_host_port("::1", 2069, explicit_port=True)
    assert host == "::1"
    assert port == 2069
    print("Self-test passed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="aeon-mud line-oriented Telnet client")
    subparsers = parser.add_subparsers(dest="command", required=True)

    connect = subparsers.add_parser("connect", help="connect to a profile or host")
    connect.add_argument("profile", nargs="?", help="saved profile name")
    connect.add_argument("--host", help="direct host when no profile is supplied")
    connect.add_argument("--port", type=int, default=None)
    connect.add_argument("--allow-plaintext", action="store_true")
    connect.add_argument("--allow-public", action="store_true", help="allow plaintext target outside private/link-local ranges")
    connect.add_argument("--expected-banner")
    connect.add_argument("--username")
    connect.add_argument("--auto-login", action="store_true")
    connect.add_argument("--no-auto-login", action="store_true")
    connect.add_argument("--transcript", choices=["off", "plain", "encrypted"])
    connect.add_argument("--terminal-type")
    connect.add_argument("--sanity-timeout", type=float)
    connect.add_argument("--keepalive-interval", type=float, help="seconds between idle Telnet NOP keepalives; 0 disables")
    connect.add_argument("--no-reconnect", action="store_true", help="exit instead of reconnecting after disconnect")
    connect.add_argument("--reconnect-initial-delay", type=float, default=None)
    connect.add_argument("--reconnect-max-delay", type=float, default=None)
    connect.add_argument("--socks5", help="SOCKS5 proxy as HOST:PORT")
    connect.add_argument("--proxy-command", nargs=argparse.REMAINDER, help="command that connects stdin/stdout to the remote Telnet stream")
    connect.add_argument("--accept-host-change", action="store_true")
    connect.add_argument("--yes", action="store_true", help="answer yes to safety prompts")
    connect.add_argument("--no-alt-screen", action="store_true")
    connect.set_defaults(func=command_connect)

    profile = subparsers.add_parser("profile", help="manage saved connection profiles")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)

    profile_list = profile_sub.add_parser("list", help="list profiles")
    profile_list.set_defaults(func=command_profile_list)

    profile_show = profile_sub.add_parser("show", help="show one profile")
    profile_show.add_argument("name")
    profile_show.set_defaults(func=command_profile_show)

    profile_remove = profile_sub.add_parser("remove", help="remove one profile")
    profile_remove.add_argument("name")
    profile_remove.set_defaults(func=command_profile_remove)

    profile_add = profile_sub.add_parser("add", help="add or replace a profile")
    profile_add.add_argument("name")
    profile_add.add_argument("--host", required=True)
    profile_add.add_argument("--port", type=int, default=None)
    profile_add.add_argument("--allow-plaintext", action="store_true")
    profile_add.add_argument("--allow-public", action="store_true")
    profile_add.add_argument("--expected-banner")
    profile_add.add_argument("--username")
    profile_add.add_argument("--auto-login", action="store_true")
    profile_add.add_argument("--transcript", choices=["off", "plain", "encrypted"], default="off")
    profile_add.add_argument("--terminal-type", default="ANSI")
    profile_add.add_argument(
        "--keepalive-interval",
        type=float,
        default=DEFAULT_KEEPALIVE_INTERVAL,
        help="seconds between idle Telnet NOP keepalives; 0 disables",
    )
    profile_add.add_argument("--no-reconnect", action="store_true")
    profile_add.add_argument("--reconnect-initial-delay", type=float, default=DEFAULT_RECONNECT_INITIAL_DELAY)
    profile_add.add_argument("--reconnect-max-delay", type=float, default=DEFAULT_RECONNECT_MAX_DELAY)
    profile_add.add_argument("--socks5", help="SOCKS5 proxy as HOST:PORT")
    profile_add.add_argument("--proxy-command", nargs=argparse.REMAINDER)
    profile_add.set_defaults(func=command_profile_add)

    secret = subparsers.add_parser("secret", help="manage DPAPI-protected saved passwords")
    secret_sub = secret.add_subparsers(dest="secret_command", required=True)
    secret_set = secret_sub.add_parser("set", help="store a password")
    secret_set.add_argument("profile")
    secret_set.add_argument("--password", help="avoid this on shared shells; omit to prompt")
    secret_set.set_defaults(func=command_secret_set)
    secret_clear = secret_sub.add_parser("clear", help="remove a stored password")
    secret_clear.add_argument("profile")
    secret_clear.set_defaults(func=command_secret_clear)

    history = subparsers.add_parser("history", help="manage command history")
    history_sub = history.add_subparsers(dest="history_command", required=True)
    history_clear = history_sub.add_parser("clear")
    history_clear.add_argument("profile")
    history_clear.set_defaults(func=command_history_clear)

    transcript = subparsers.add_parser("transcript", help="manage transcripts")
    transcript_sub = transcript.add_subparsers(dest="transcript_command", required=True)
    transcript_decrypt = transcript_sub.add_parser("decrypt", help="decrypt a DPAPI transcript")
    transcript_decrypt.add_argument("file")
    transcript_decrypt.add_argument("--out")
    transcript_decrypt.set_defaults(func=command_transcript_decrypt)

    paths = subparsers.add_parser("paths", help="show local config paths")
    paths.set_defaults(func=command_paths)

    self_test = subparsers.add_parser("self-test", help="run built-in parser checks")
    self_test.set_defaults(func=command_self_test)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nDisconnected (Ctrl+C).")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
