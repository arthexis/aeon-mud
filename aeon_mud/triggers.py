"""One-shot, realm/character-scoped MUD response triggers.

Only complete received server lines can match. All sends are executed on the UI
thread, never on the receiver thread; matching atomically consumes each rule.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
from pathlib import Path
import os
import stat
import tempfile
import json
import re
import shlex
import threading
import time
import uuid
from typing import Any

from .config import config_dir, load_json, save_json
from .realm_watches import realm_key
from .terminal import compile_watch_pattern
from .terminal_text import strip_ansi

MAX_RULES = 128
MAX_QUEUE = 64
MAX_LINE = 16384
MAX_COMMANDS = 16
MAX_COMMAND_SIZE = 4096
MAX_STORE_BYTES = 512 * 1024
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*|\d+)\}|\[([A-Za-z_][A-Za-z0-9_]*)\]")
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def duration(raw: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m)?", raw)
    if not match:
        raise ValueError("delay must be a duration (e.g. 500ms, 2s)")
    value = float(match[1]) * {"ms": .001, "s": 1, "m": 60}[match[2] or "s"]
    if not 0 <= value <= 60:
        raise ValueError("delay must be between 0 and 60 seconds")
    return value


def render_template(template: str, match: re.Match[str]) -> str:
    groups = {"0": match.group(0)}
    groups.update({str(i): match.group(i) for i in range(1, match.re.groups + 1)})
    groups.update(match.groupdict())

    def substitute(found: re.Match[str]) -> str:
        key = found[1] or found[2]
        if key not in groups or groups[key] is None:
            raise ValueError(f"missing capture: {key}")
        return str(groups[key])

    # One substitution pass only; captured content is never re-expanded.
    rendered = PLACEHOLDER.sub(substitute, template)
    if not rendered or len(rendered) > MAX_COMMAND_SIZE or "\n" in rendered or "\r" in rendered:
        raise ValueError("expanded response must be one nonempty line (max 4096 characters)")
    if rendered.lstrip().startswith("//"):
        raise ValueError("trigger responses cannot execute local // commands")
    return rendered


@dataclass
class Trigger:
    id: str
    pattern: str
    commands: list[str]
    name: str = ""
    search: bool = False
    use_regex: bool = False
    persist: bool = False
    once_per_session: bool = False
    dry_run: bool = False
    delay: float = 0
    state: str = "armed"
    receipt: dict[str, Any] | None = None
    regex: re.Pattern[str] = field(init=False, repr=False)

    def __post_init__(self):
        self.regex = compile_watch_pattern(self.pattern, use_regex=self.use_regex)
        if not self.commands or len(self.commands) > MAX_COMMANDS:
            raise ValueError("trigger requires 1-16 responses")
        if any(not v or "\n" in v or "\r" in v or len(v) > MAX_COMMAND_SIZE
               or v.lstrip().startswith("//") for v in self.commands):
            raise ValueError("trigger responses must be single MUD commands (max 4096 chars)")
        if self.name and not NAME.fullmatch(self.name):
            raise ValueError("trigger name must be letters, numbers, _ or -")
        if not 0 <= self.delay <= 60:
            raise ValueError("trigger delay must be between 0 and 60 seconds")

    def match(self, text: str) -> re.Match[str] | None:
        return self.regex.search(text) if self.search else self.regex.match(text)

    def to_data(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("id", "pattern", "commands", "name", "search",
            "use_regex", "persist", "once_per_session", "dry_run", "delay", "state", "receipt")}


@dataclass
class Pending:
    rule_id: str
    commands: list[str]
    due: float
    receipt: dict[str, Any]
    index: int = 0


def trigger_store_path() -> Path:
    return config_dir() / "trigger_rules.json"


def scope_key(profile, character: str | None) -> str:
    return f"{realm_key(profile)}::{(character or 'default').casefold()}"


class TriggerEngine:
    def __init__(self, profile, character: str | None = None, *, clock=None):
        self.profile = profile
        self.character = character
        self.key = scope_key(profile, character)
        self.clock = clock or time.monotonic
        self.lock = threading.RLock()
        self.rules: list[Trigger] = []
        self.pending: deque[Pending] = deque()
        self.partial = ""
        self.paused = False
        self.journal = None
        self._load()

    def _load(self):
        path = trigger_store_path()
        if path.is_symlink():
            raise ValueError("trigger configuration cannot be a symlink")
        if path.exists():
            info = path.stat()
            if info.st_size > MAX_STORE_BYTES or not stat.S_ISREG(info.st_mode):
                raise ValueError("trigger configuration is unsafe or exceeds size limit")
            if os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
                raise ValueError("trigger configuration must be owner-only (0600)")
        data = load_json(path, {"scopes": {}})
        item = data.get("scopes", {}).get(self.key, {})
        if not isinstance(item, dict):
            raise ValueError("invalid trigger scope")
        self.paused = bool(item.get("paused", False))
        for raw in item.get("rules", []):
            if len(self.rules) >= MAX_RULES:
                raise ValueError("too many saved triggers")
            trigger = Trigger(**raw)
            if trigger.once_per_session and trigger.state == "spent":
                trigger.state, trigger.receipt = "armed", None
            self.rules.append(trigger)

    def _save(self):
        # Keep ephemeral rules out of persistent config.
        path = trigger_store_path()
        if path.is_symlink():
            raise ValueError("trigger configuration cannot be a symlink")
        if path.exists():
            info = path.stat()
            if info.st_size > MAX_STORE_BYTES or not stat.S_ISREG(info.st_mode):
                raise ValueError("trigger configuration is unsafe or exceeds size limit")
            if os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
                raise ValueError("trigger configuration must be owner-only (0600)")
        data = load_json(path, {"scopes": {}})
        scopes = data.setdefault("scopes", {})
        if not isinstance(scopes, dict):
            raise ValueError("invalid trigger configuration")
        scopes[self.key] = {"paused": self.paused,
                            "rules": [r.to_data() for r in self.rules if r.persist]}
        if len(json.dumps(data).encode("utf-8")) > MAX_STORE_BYTES:
            raise ValueError("trigger configuration would exceed size limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic owner-only replacement; never expose response templates via 0644 files.
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=".trigger-", suffix=".tmp", delete=False
            ) as target:
                temp_name = target.name
                json.dump(data, target, ensure_ascii=False, indent=2, sort_keys=True)
                target.write("\n")
            os.replace(temp_name, path)
            temp_name = None
        finally:
            if temp_name is not None:
                Path(temp_name).unlink(missing_ok=True)

    def _find(self, selector: str) -> Trigger:
        found = [r for r in self.rules if r.id == selector or r.name == selector]
        if len(found) != 1:
            raise ValueError(f"trigger not found or ambiguous: {selector}")
        return found[0]

    def add(self, pattern: str, commands: list[str], **kwargs) -> Trigger:
        with self.lock:
            if len(self.rules) >= MAX_RULES:
                raise ValueError("too many triggers")
            rule = Trigger(id="t-" + uuid.uuid4().hex[:10], pattern=pattern,
                           commands=commands, **kwargs)
            if rule.name and any(r.name == rule.name for r in self.rules):
                raise ValueError("trigger name already used")
            self.rules.append(rule)
            try:
                if rule.persist:
                    self._save()
            except Exception:
                self.rules.remove(rule)
                raise
            return rule

    def modify(self, selector: str, operation: str) -> Trigger:
        with self.lock:
            rule = self._find(selector)
            if operation == "remove":
                self.rules.remove(rule)
                self.pending = deque(x for x in self.pending if x.rule_id != rule.id)
            else:
                before = rule.state
                if operation == "off":
                    rule.state = "disabled"
                    self.pending = deque(x for x in self.pending if x.rule_id != rule.id)
                elif operation == "on":
                    if rule.state == "spent":
                        raise ValueError("spent triggers require --rearm")
                    rule.state = "armed"
                elif operation == "rearm":
                    rule.state, rule.receipt = "armed", None
                    self.pending = deque(x for x in self.pending if x.rule_id != rule.id)
                else:
                    raise ValueError("invalid trigger action")
            if rule.persist:
                self._save()
            return rule

    def pause(self, value: bool):
        with self.lock:
            self.paused = value
            if value:
                for pending in self.pending:
                    pending.receipt.update(
                        status="outcome_unknown" if pending.index else "failed",
                        error="trigger dispatch paused")
                self.pending.clear()
            self._save()

    def on_disconnect(self):
        with self.lock:
            self.partial = ""
            for pending in self.pending:
                pending.receipt.update(
                    status="outcome_unknown" if pending.index else "failed",
                    error="session disconnected")
            self.pending.clear()
            try:
                if any(rule.persist for rule in self.rules):
                    self._save()
            except (OSError, ValueError):
                pass

    def clear(self):
        with self.lock:
            n = len(self.rules)
            self.rules = []
            self.pending.clear()
            self._save()
            return n

    def list(self) -> str:
        with self.lock:
            if not self.rules:
                return "No triggers"
            return ("Triggers (" + ("paused" if self.paused else "active") + "): " +
                "; ".join(f"{r.id}{' '+r.name if r.name else ''} [{r.state}] {r.pattern}"
                          for r in self.rules))

    def status(self, selector: str) -> dict[str, Any]:
        with self.lock:
            return self._find(selector).to_data()

    def test(self, selector: str, line: str) -> dict[str, Any]:
        with self.lock:
            rule = self._find(selector)
            match = rule.match(strip_ansi(line))
            if match is None:
                return {"matched": False, "commands": [], "captures": {}}
            return {"matched": True, "commands": [render_template(c, match) for c in rule.commands],
                    "captures": {"named": {k: v for k, v in match.groupdict().items() if v is not None},
                                 "groups": list(match.groups())}}

    def feed(self, chunk: str):
        """Receive-thread input; match *only* completed server lines."""
        if not chunk:
            return
        with self.lock:
            normalized = (self.partial + chunk).replace("\r\n", "\n").replace("\r", "\n")
            lines = normalized.split("\n")
            self.partial = lines.pop()[-MAX_LINE:]
            if self.paused:
                return
            for raw in lines:
                line = strip_ansi(raw)[:MAX_LINE]
                if not line:
                    continue
                for rule in self.rules:
                    if rule.state != "armed":
                        continue
                    match = rule.match(line)
                    if not match:
                        continue
                    try:
                        commands = [render_template(command, match) for command in rule.commands]
                        captures = {"named": {k: v for k, v in match.groupdict().items() if v is not None},
                                    "groups": list(match.groups())}
                    except ValueError as exc:
                        rule.receipt = {"status": "failed", "error": str(exc)}
                        rule.state = "spent"
                        if rule.persist:
                            try:
                                self._save()
                            except (OSError, ValueError):
                                pass
                        continue
                    receipt = {"status": "dry_run" if rule.dry_run else "reserved",
                               "matched_text": line, "captures": captures, "commands": commands,
                               "trigger_id": rule.id, "origin": "trigger", "action_id": None}
                    # Reserve and persist *before* enqueue or dispatch; no replay on restart.
                    rule.state, rule.receipt = "spent", receipt
                    if rule.persist:
                        try:
                            self._save()
                        except (OSError, ValueError) as exc:
                            # Never send when one-shot reservation cannot be made durable.
                            receipt.update(status="failed", error=f"persistence failed: {exc}")
                            continue
                    if rule.dry_run:
                        continue
                    if len(self.pending) >= MAX_QUEUE:
                        receipt["status"] = "failed"
                        receipt["error"] = "trigger dispatch queue full"
                        if rule.persist:
                            try:
                                self._save()
                            except (OSError, ValueError):
                                pass
                        continue
                    self.pending.append(Pending(rule.id, commands, self.clock() + rule.delay, receipt))

    def reset_partial(self):
        with self.lock:
            self.partial = ""

    def drain(self, controller):
        """Foreground thread; at most one command dispatched per call."""
        with self.lock:
            if self.paused or not self.pending:
                return
            # Never dispatch queued commands during the character login handshake.
            if not getattr(controller, "trigger_ready", True):
                return
            pending = self.pending[0]
            if self.clock() < pending.due:
                return
            if controller.current_connection() is None:
                pending.receipt.update(status="failed", error="session disconnected")
                self.pending.popleft()
                self._persist_receipt(pending.rule_id)
                return
            if pending.index == 0 and self.journal is not None:
                transcript = controller.transcript
                with transcript.lock:
                    path = transcript.path if transcript.mode in {"text", "plain"} else None
                    rule = self._find(pending.rule_id)
                    try:
                        action = self.journal.start_action(
                            pending.commands, durable=path is not None, log_path=path,
                            origin="trigger", trigger_id=rule.id, trigger_name=rule.name,
                            captures=pending.receipt["captures"],
                            matched_text=pending.receipt["matched_text"])
                    except (OSError, ValueError, RuntimeError) as exc:
                        pending.receipt.update(status="failed", error=f"action journal: {exc}")
                        self.pending.popleft()
                        self._persist_receipt(pending.rule_id)
                        return
                pending.receipt["action_id"] = action["action_id"]
            command = pending.commands[pending.index]
            try:
                controller.send_line(command, secret=False, remember=False)
            except (OSError, RuntimeError, ValueError) as exc:
                pending.receipt.update(status="outcome_unknown", error=str(exc))
                self.pending.popleft()
            else:
                pending.index += 1
                pending.receipt["status"] = "sent" if pending.index == len(pending.commands) else "sending"
                if pending.index == len(pending.commands):
                    self.pending.popleft()
                else:
                    pending.due = self.clock() + controller.profile.paste_line_delay
            self._persist_receipt(pending.rule_id)

    def _persist_receipt(self, rule_id: str):
        rule = self._find(rule_id)
        if rule.persist:
            try:
                self._save()
            except (OSError, ValueError):
                # The action was already reserved as spent before dispatch.
                pass


def handle_trigger_command(line: str, engine: TriggerEngine) -> str | None:
    if line != "//trigger" and not line.startswith("//trigger "):
        return None
    try:
        args = shlex.split(line[len("//trigger"):])
        if not args or args == ["--list"]:
            return engine.list()
        actions = {"--list", "--status", "--off", "--on", "--rearm", "--remove",
                   "--clear", "--pause-all", "--resume-all", "--test"}
        if args[0] in actions:
            action = args[0][2:]
            if action == "list" and len(args) == 1:
                return engine.list()
            if action == "clear" and len(args) == 1:
                return f"Removed {engine.clear()} triggers"
            if action in {"pause-all", "resume-all"} and len(args) == 1:
                engine.pause(action == "pause-all")
                return "Triggers paused" if action == "pause-all" else "Triggers resumed"
            if action == "test" and len(args) >= 3:
                return json.dumps(engine.test(args[1], " ".join(args[2:])), ensure_ascii=False)
            if action == "status" and len(args) == 2:
                return json.dumps(engine.status(args[1]), ensure_ascii=False)
            if action in {"off", "on", "rearm", "remove"} and len(args) == 2:
                rule = engine.modify(args[1], action)
                return f"Trigger {rule.id}: {action}"
            raise ValueError("invalid trigger management arguments")
        opts: dict[str, Any] = {}
        commands: list[str] = []
        pattern: list[str] = []
        i = 0
        while i < len(args):
            token = args[i]
            if token in {"--re", "--search", "--persist", "--once-per-session", "--dry-run"}:
                key = {"--re": "use_regex", "--search": "search",
                       "--persist": "persist", "--once-per-session": "once_per_session",
                       "--dry-run": "dry_run"}[token]
                opts[key] = True
                i += 1
            elif token in {"--send", "--name", "--delay"}:
                if i + 1 >= len(args):
                    raise ValueError(f"{token} requires a value")
                value = args[i + 1]
                if token == "--send":
                    commands.append(value)
                elif token == "--name":
                    opts["name"] = value
                else:
                    opts["delay"] = duration(value)
                i += 2
            elif token.startswith("--"):
                raise ValueError(f"unknown trigger option: {token}")
            else:
                pattern.append(token)
                i += 1
        if not pattern or not commands:
            raise ValueError("usage: //trigger PATTERN --send RESPONSE [--send RESPONSE ...]")
        rule = engine.add(" ".join(pattern), commands, **opts)
        return f"Trigger armed: {rule.id}" + (f" ({rule.name})" if rule.name else "")
    except (ValueError, OSError, TypeError) as exc:
        return f"Trigger error: {exc}"
