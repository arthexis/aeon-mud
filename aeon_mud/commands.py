"""Local slash commands, session settings and input submission."""
from __future__ import annotations
import re
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING
from .config import *
from .profiles import Profile
from .input import KeyReader
from .terminal import *
from .transcript import Transcript

if TYPE_CHECKING:
    from .session import SessionController

def append_client_event(transcript: Transcript, ui: MudUi, message: str) -> None:
    plain = f"[client] {message}\n"
    transcript.write("recv", plain)
    ui.append_output(f"\x1b[33m{plain}\x1b[0m")


def confirm_local_command(transcript: Transcript, ui: MudUi, message: str) -> None:
    ui.set_status(message)
    append_client_event(transcript, ui, message)


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
        maximum = MAX_HISTORY_ENTRIES if field_name == "max_history" else MAX_SCROLLBACK_LINES
        if not 1 <= value <= maximum:
            raise ValueError(f"{field_name} must be between 1 and {maximum}")
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


