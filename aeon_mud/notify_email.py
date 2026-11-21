"""Offline-configured, TLS-only SMTP notifications for watch events.

No SMTP passwords in config files, CLI arguments, MUD input, or event payloads.
Credentials are read only from an explicitly named environment variable.
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import stat
import tempfile
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from .config import config_dir, load_json

SMTP_TIMEOUT = 8.0
MAX_TO = 5
MAX_MESSAGE = 1024


def email_config_path() -> Path:
    return config_dir() / "notify_email.json"


def _check_private(path: Path) -> None:
    if os.name == "nt":
        return
    folder = path.parent
    if folder.is_symlink():
        raise PermissionError("unsafe notification config directory")
    if folder.exists() and (folder.stat().st_uid != os.getuid() or folder.stat().st_mode & 0o077):
        raise PermissionError("notification config directory must be private (0700)")
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("notification config must be owner-only (0600)")


def _valid_address(address: str) -> bool:
    if not isinstance(address, str) or len(address) > 254 or any(ord(c) < 33 or ord(c) > 126 for c in address):
        return False
    parsed_name, parsed_addr = parseaddr(address)
    return not parsed_name and parsed_addr == address and address.count("@") == 1 and "." in address.split("@")[1]


def validate_email_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("email configuration must be a JSON object")
    required = {"host", "port", "tls", "from", "to"}
    allowed = required | {"username", "password_env", "enabled"}
    if not required.issubset(config) or set(config) - allowed:
        raise ValueError("email configuration requires host, port, tls, from, to and no unknown fields")
    host = config["host"]
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", host):
        raise ValueError("invalid SMTP hostname")
    port = config["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("SMTP port must be 1-65535")
    if config["tls"] not in ("ssl", "starttls"):
        raise ValueError("SMTP TLS must be ssl or starttls")
    if not _valid_address(config["from"]):
        raise ValueError("invalid SMTP sender address")
    to = config["to"]
    if not isinstance(to, list) or not 1 <= len(to) <= MAX_TO or not all(_valid_address(item) for item in to):
        raise ValueError("to must contain 1-5 valid email addresses")
    username = config.get("username")
    secret_env = config.get("password_env")
    if username is not None and (not isinstance(username, str) or not 1 <= len(username) <= 254 or any(ord(c) < 33 for c in username)):
        raise ValueError("invalid SMTP username")
    if secret_env is not None and (not isinstance(secret_env, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", secret_env)):
        raise ValueError("password_env must be an uppercase environment variable name")
    if bool(username) != bool(secret_env):
        raise ValueError("SMTP username and password_env must be configured together")
    if not isinstance(config.get("enabled", False), bool):
        raise ValueError("enabled must be boolean")
    return config


def load_email_config(path: Path | None = None) -> dict[str, Any] | None:
    path = path or email_config_path()
    _check_private(path)
    if not path.exists():
        return None
    return validate_email_config(load_json(path, {}))


def save_email_config(config: dict[str, Any], path: Path | None = None) -> None:
    config = validate_email_config(config)
    path = path or email_config_path()
    folder = path.parent
    if not folder.exists():
        folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    _check_private(path)
    fd, temporary = tempfile.mkstemp(prefix=".notify-email-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def send_watch_email(event: dict[str, Any], config_path: Path | None = None) -> None:
    config = load_email_config(config_path)
    if not config or not config.get("enabled", False):
        raise RuntimeError("email notifications are not configured and enabled")
    password = None
    if config.get("username"):
        password = os.environ.get(config["password_env"])
        if not password:
            raise RuntimeError("SMTP password environment variable is unavailable")
    msg = EmailMessage()
    msg["From"] = config["from"]
    msg["To"] = ", ".join(config["to"])
    # No untrusted event fields are ever used for MIME headers.
    msg["Subject"] = "aeon-mud watch notification"
    body = {
        "schema": event.get("schema", 1),
        "event_id": str(event.get("event_id", ""))[:64],
        "timestamp": str(event.get("timestamp", ""))[:64],
        "realm": str(event.get("realm", ""))[:128],
        "character": str(event.get("character") or "")[:64],
        "pattern": str(event.get("pattern", ""))[:256],
        "message": str(event.get("message", ""))[:MAX_MESSAGE],
        "captures": event.get("captures", {"named": {}, "groups": []}),
    }
    msg.set_content(json.dumps(body, indent=2, ensure_ascii=False))
    context = ssl.create_default_context()
    if config["tls"] == "ssl":
        with smtplib.SMTP_SSL(config["host"], config["port"], timeout=SMTP_TIMEOUT, context=context) as server:
            if config.get("username"):
                server.login(config["username"], password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(config["host"], config["port"], timeout=SMTP_TIMEOUT) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            if config.get("username"):
                server.login(config["username"], password)
            server.send_message(msg)
