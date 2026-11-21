"""Persistent client-only watch settings, scoped by actual host and port."""
from __future__ import annotations

from typing import Any

from .config import config_dir, load_json, save_json
from .profiles import Profile


def realm_key(profile: Profile) -> str:
    host = profile.host.rstrip(".").casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{profile.port}"


def realm_watch_path():
    return config_dir() / "realm_watches.json"


def load_realm_watches(profile: Profile) -> tuple[list[dict[str, str]], bool]:
    data = load_json(realm_watch_path(), {"realms": {}})
    realm = data.get("realms", {}).get(realm_key(profile), {})
    if not isinstance(realm, dict):
        raise ValueError("realm watches must be an object")
    return realm.get("watch_rules", []), realm.get("enabled", True)


def save_realm_watches(profile: Profile, rules: list[dict[str, str]], enabled: bool) -> None:
    path = realm_watch_path()
    data: dict[str, Any] = load_json(path, {"realms": {}})
    realms = data.setdefault("realms", {})
    if not isinstance(realms, dict):
        raise ValueError("realms must be an object")
    realms[realm_key(profile)] = {"watch_rules": rules, "enabled": enabled}
    save_json(path, data)
