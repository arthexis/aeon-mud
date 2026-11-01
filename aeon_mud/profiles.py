"""Server profiles, known host banners and command history."""
from __future__ import annotations
import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from .config import *

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
    characters: list[str] = field(default_factory=list)
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


