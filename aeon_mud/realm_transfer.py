"""Portable watch-realm export/import (never includes credentials or SMTP settings)."""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from .config import load_json
from .realm_watches import realm_watch_path
from .terminal import watch_rules_from_data, watch_rules_to_data

FORMAT = "aeon-mud-realm-watches"
VERSION = 1
MAX_BYTES = 1024 * 1024
MAX_REALMS = 256
MAX_RULES = 256
KEY_PATTERN = re.compile(r"(?:\[[0-9a-fA-F:.%]+\]|[A-Za-z0-9._:-]+):[0-9]{1,5}\Z")


def _validate_realms(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict) or len(raw) > MAX_REALMS:
        raise ValueError("realms must be an object with at most 256 endpoints")
    valid: dict[str, dict[str, Any]] = {}
    for endpoint, entry in raw.items():
        if not isinstance(endpoint, str) or len(endpoint) > 270 or not KEY_PATTERN.fullmatch(endpoint):
            raise ValueError("invalid realm endpoint key")
        if not isinstance(entry, dict) or set(entry) - {"enabled", "watch_rules"}:
            raise ValueError(f"invalid realm entry: {endpoint}")
        enabled = entry.get("enabled", True)
        rules = entry.get("watch_rules", [])
        if not isinstance(enabled, bool) or not isinstance(rules, list) or len(rules) > MAX_RULES:
            raise ValueError(f"invalid realm settings: {endpoint}")
        # Full compilation validates all regexes, sigils, colors and options
        # before touching the destination file.
        valid[endpoint] = {
            "enabled": enabled,
            "watch_rules": watch_rules_to_data(watch_rules_from_data(rules)),
        }
    return valid


def export_realms(destination: Path, realm: str | None = None) -> int:
    raw = load_json(realm_watch_path(), {"realms": {}})
    realms = _validate_realms(raw.get("realms", {}))
    if realm is not None:
        if realm not in realms:
            raise ValueError(f"realm not found: {realm}")
        realms = {realm: realms[realm]}
    payload = {"format": FORMAT, "version": VERSION, "realms": realms}
    _atomic_json(Path(destination).expanduser(), payload, overwrite=False)
    return len(realms)


def import_realms(source: Path, *, replace: bool = False) -> int:
    source = Path(source).expanduser()
    if source.stat().st_size > MAX_BYTES:
        raise ValueError("realm file exceeds 1 MiB")
    with source.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or set(payload) != {"format", "version", "realms"}:
        raise ValueError("unsupported realm file structure")
    if payload["format"] != FORMAT or type(payload["version"]) is not int or payload["version"] != VERSION:
        raise ValueError("unsupported realm file version")
    incoming = _validate_realms(payload["realms"])
    current = load_json(realm_watch_path(), {"realms": {}})
    present = _validate_realms(current.get("realms", {}))
    if replace:
        result = incoming
    else:
        result = dict(present)
        result.update(incoming)  # Imported endpoints replace matching endpoints only.
    if len(result) > MAX_REALMS:
        raise ValueError("merged realm collection exceeds 256 endpoints")
    path = realm_watch_path()
    _atomic_json(path, {**current, "realms": result}, overwrite=True)
    return len(incoming)


def _atomic_json(path: Path, payload: dict[str, Any], *, overwrite: bool) -> None:
    parent = path.parent
    if parent.is_symlink() or path.is_symlink():
        raise ValueError("unsafe symbolic link for realm file")
    if not overwrite and path.exists():
        raise FileExistsError(f"destination exists: {path}")
    parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".aeon-realms-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if not overwrite and path.exists():
            raise FileExistsError(f"destination exists: {path}")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
