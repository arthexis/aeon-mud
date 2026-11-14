"""Settings, persisted JSON and endpoint parsing; standard library only."""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
MAX_HISTORY_ENTRIES = 10000
MAX_HISTORY_FILE_BYTES = 8 * 1024 * 1024
MAX_HISTORY_COMMAND_CHARS = 65536
MAX_SCROLLBACK_LINES = 50000
MAX_SCROLLBACK_LINE_CHARS = 16384
MAX_SCROLLBACK_TOTAL_CHARS = 1048576
MAX_DISPLAY_CACHE_ROWS = 65536
MAX_DISPLAY_CACHE_BYTES = 2097152  # approximate UTF-8 payload, not Python heap/RSS
MAX_ACTIVE_SGR_CHARS = 512
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


