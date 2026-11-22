"""Local double-slash commands, session settings and input submission."""
from __future__ import annotations
import re
import shlex
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING
from .config import *
from .profiles import Profile, ProfileStore
from .input import KeyReader
from .terminal import *
from .transcript import Transcript, resolve_log_path
from .realm_watches import save_realm_watches
from .watch_events import DEFAULT_COOLDOWN, parse_sound, lcd_argv

if TYPE_CHECKING:
    from .session import SessionController

def append_client_event(transcript: Transcript, ui: MudUi, message: str) -> None:
    plain = f"[client] {message}\n"
    transcript.write("recv", plain)
    ui.append_output(f"\x1b[33m{plain}\x1b[0m", watch_events=False)


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
    persist_watches(transcript, ui)


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
            if not handle_local_command(to_send, controller.transcript, ui):
                controller.send_line(to_send, secret=False, remember=True)
        except OSError as exc:
            remaining = [to_send] + lines[index + 1 :]
            ui.replace_input("\n".join(remaining))
            ui.set_status(f"Paste stopped; input kept while reconnecting: {exc}")
            return
        time.sleep(controller.profile.paste_line_delay)
    ui.set_status(f"Pasted {len(lines)} lines")


def persist_watches(transcript: Transcript, ui: MudUi) -> None:
    with ui.lock:
        rules = watch_rules_to_data(ui.watch_rules)
        enabled = ui.watches_enabled
    try:
        save_realm_watches(ui.profile, rules, enabled)
    except (OSError, ValueError) as exc:
        confirm_local_command(transcript, ui, f"Watch persistence error: {exc}")


def handle_unwatch_command(line: str, transcript: Transcript, ui: MudUi) -> bool:
    if line != "//unwatch" and not line.startswith("//unwatch "):
        return False
    argument = strip_outer_quotes(line[len("//unwatch"):].strip())
    if argument:
        removed = ui.remove_watch(argument)
        confirm_local_command(transcript, ui,
                              f"Removed watch: {argument}" if removed else f"No watch matched: {argument}")
    else:
        ui.set_watches_enabled(False)
        confirm_local_command(transcript, ui, "Watch highlighting disabled (rules retained)")
    persist_watches(transcript, ui)
    return True


def parse_watch_with_actions(argument: str) -> tuple[str, str, str | None, str | None, float, bool, bool, bool]:
    """Read optional actions, preserving legacy PATTERN and PATTERN COLOR."""
    try:
        tokens = shlex.split(argument)
    except ValueError as exc:
        raise ValueError(f"invalid quotes: {exc}") from exc
    sound: str | None = None
    lcd: str | None = None
    cooldown = DEFAULT_COOLDOWN
    search = False
    use_regex = False
    email = False
    color: str | None = None
    words: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("--search", "--re", "--email"):
            if token == "--search":
                search = True
            elif token == "--re":
                use_regex = True
            else:
                email = True
            index += 1
        elif token in ("--sound", "--lcd", "--cooldown", "--color"):
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                raise ValueError(f"{token} requires a value")
            value = tokens[index + 1]
            if token == "--sound":
                parse_sound(value)
                sound = value
            elif token == "--lcd":
                lcd_argv(value)
                lcd = value
            elif token == "--color":
                resolve_watch_color(value)
                color = value
            else:
                try:
                    cooldown = float(value)
                except ValueError as exc:
                    raise ValueError("--cooldown requires seconds 0-86400") from exc
            index += 2
        elif token.startswith("--"):
            raise ValueError(f"unknown watch option: {token}")
        else:
            words.append(token)
            index += 1
    if not words:
        raise ValueError("pattern must not be empty")
    if len(words) == 1:
        # shlex already stripped quotes. Do not misread a quoted pattern
        # such as "danger red" as a legacy positional color.
        pattern, inferred_color = words[0], "yellow"
    elif color is not None:
        pattern, inferred_color = " ".join(words), color
    else:
        pattern, inferred_color = parse_watch_arguments(" ".join(words))
    if color is not None:
        inferred_color = color
    if not 0 <= cooldown <= 86400:
        raise ValueError("--cooldown requires seconds 0-86400")
    return pattern, inferred_color, sound, lcd, cooldown, search, use_regex, email


def handle_watch_command(line: str, transcript: Transcript, ui: MudUi) -> bool:
    if line != "//watch" and not line.startswith("//watch "):
        return False
    argument = line[len("//watch"):].strip()
    lowered = argument.lower()
    if not argument or lowered in {"list", "status", "?"}:
        confirm_local_command(transcript, ui, ui.describe_watches())
        return True
    if lowered in {"clear", "off"}:
        removed = ui.clear_watches()
        persist_watches(transcript, ui)
        confirm_local_command(transcript, ui, f"Cleared {removed} watch rule" + ("" if removed == 1 else "s"))
        return True
    if lowered.startswith("remove "):
        pattern = strip_outer_quotes(argument[7:].strip())
        if not pattern:
            confirm_local_command(transcript, ui, "Watch error: pattern must not be empty")
            return True
        removed = ui.remove_watch(pattern)
        persist_watches(transcript, ui)
        confirm_local_command(transcript, ui, f"Removed watch: {pattern}" if removed else f"No watch matched: {pattern}")
        return True
    try:
        pattern, color, sound, lcd, cooldown, search, use_regex, email = parse_watch_with_actions(argument)
        rule = ui.add_watch(pattern, color, sound=sound, lcd=lcd, cooldown=cooldown,
                            search=search, use_regex=use_regex, email=email)
        ui.set_watches_enabled(True)
        persist_watches(transcript, ui)
    except ValueError as exc:
        confirm_local_command(transcript, ui, f"Watch error: {exc}")
    else:
        details = (f" sound={rule.sound}" if rule.sound else "")
        details += (f" lcd={rule.lcd}" if rule.lcd else "")
        details += (" --search" if rule.search else "")
        details += (" --re" if rule.use_regex else "")
        details += (" --email" if rule.email else "")
        confirm_local_command(transcript, ui, f"Watching {rule.pattern} as {rule.color}{details}")
    return True


def handle_session_profile_command(line: str, transcript: Transcript, ui: MudUi) -> bool:
    if line != "//save" and not line.startswith("//save ") and line != "//load" and not line.startswith("//load "):
        return False
    if line == "//save" or line.startswith("//save "):
        argument = line[len("//save"):].strip()
        if not argument:
            confirm_local_command(transcript, ui, "Save error: usage: //save file")
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
        argument = line[len("//load"):].strip()
        if argument:
            path = resolve_session_profile_path(argument)
        elif ui.profile_config_path is not None:
            path = ui.profile_config_path
        else:
            confirm_local_command(transcript, ui, "Load error: usage: //load file")
            return True
        data = load_session_profile(path)
        apply_session_profile(data, ui.profile, transcript, ui)
    except (OSError, ValueError) as exc:
        confirm_local_command(transcript, ui, f"Load error: {exc}")
    else:
        ui.profile_config_path = path
        confirm_local_command(transcript, ui, f"Loaded profile: {path}")
    return True


def save_persistent_log_setting(transcript: Transcript) -> None:
    """Persist only explicitly requested logging, not transient profile settings."""
    store = ProfileStore()
    name = transcript.profile.name
    if name not in store.data["profiles"]:
        raise ValueError("profile must be saved to persist logging")
    stored = store.data["profiles"][name]
    stored["log_persist"] = bool(transcript.persist)
    if transcript.persist:
        stored["transcript"] = "text"
        stored["transcript_path"] = str(transcript.path)
    elif stored.get("transcript") == "text":
        stored["transcript"] = "off"
        stored["transcript_path"] = ""
    store.save()


def handle_log_command(line: str, transcript: Transcript, ui: MudUi) -> bool:
    if line != "//log" and not line.startswith("//log "):
        return False
    try:
        args = shlex.split(line[len("//log"):])
        flags = {"--persist", "--switch", "--status", "--stop", "--off"}
        unknown = [arg for arg in args if arg.startswith("--") and arg not in flags]
        if unknown:
            raise ValueError(f"unknown option: {unknown[0]}")
        enabled = {arg for arg in args if arg in flags}
        files = [arg for arg in args if arg not in flags]
        if len(files) > 1:
            raise ValueError("only one log filename can be specified")
        if "--status" in enabled:
            if len(enabled) > 1 or files:
                raise ValueError("--status cannot be combined with other arguments")
            confirm_local_command(transcript, ui, f"Log: {transcript.path or 'off'}; persist: {'on' if transcript.persist else 'off'}")
            return True
        if "--stop" in enabled or "--off" in enabled:
            if enabled - {"--stop", "--off"}:
                raise ValueError("--stop/--off cannot be combined with other flags")
            if files and (transcript.path is None or transcript.path.resolve() != resolve_log_path(files[0]).absolute().resolve()):
                raise ValueError("specified filename is not the active log")
            previous = transcript.path
            was_persistent = transcript.persist
            transcript.disable()
            if was_persistent:
                save_persistent_log_setting(transcript)
            confirm_local_command(transcript, ui, f"Logging stopped: {previous}" if previous else "Logging is already off")
            return True
        if not files and "--switch" in enabled:
            raise ValueError("--switch requires a filename")
        if transcript.mode == "text" and transcript.path is not None:
            requested = resolve_log_path(files[0]).absolute() if files else transcript.path
            if requested.resolve() != transcript.path.resolve() and "--switch" not in enabled:
                raise ValueError("logging is already active; use --switch FILE to change destination")
            if requested.resolve() == transcript.path.resolve() and "--switch" in enabled:
                raise ValueError("--switch requires a different filename")
            path = transcript.enable_text_log(str(requested))
        else:
            if transcript.mode != "off" and "--switch" not in enabled:
                raise ValueError("another transcript is active; use --switch FILE")
            path = transcript.enable_text_log(files[0] if files else None)
        if "--persist" in enabled:
            transcript.persist = True
            transcript.profile.log_persist = True
        if transcript.persist:
            save_persistent_log_setting(transcript)
        confirm_local_command(transcript, ui, f"Logging to {path}; persist: {'on' if transcript.persist else 'off'}")
    except (OSError, ValueError) as exc:
        confirm_local_command(transcript, ui, f"Log error: {exc}")
    return True


def handle_local_command(line: str, transcript: Transcript, ui: MudUi) -> bool:
    if line == "//help":
        confirm_local_command(
            transcript, ui,
            "Local commands: //help, //watch PATTERN [--color COLOR] [--sound [MOTIF.]SOUND] " 
            "[--lcd TEXT] [--cooldown SECONDS] [--search] [--re] [--email] (yellow default), "
            "//watch list, //unwatch [PATTERN], //watch clear, //log [FILE] [--persist|--switch|--stop|--off|--status], "
            "//save FILE, //load [FILE]. Watches persist per host:port."
        )
        return True
    if not line.startswith("//"):
        return False
    if handle_unwatch_command(line, transcript, ui):
        return True
    if handle_watch_command(line, transcript, ui):
        return True
    if handle_session_profile_command(line, transcript, ui):
        return True
    if handle_log_command(line, transcript, ui):
        return True
    if line.startswith("//"):
        confirm_local_command(transcript, ui, f"Unknown local command: {line.split()[0]}. Use //help")
        return True
    return False


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


