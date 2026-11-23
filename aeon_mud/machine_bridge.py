"""Bounded live bridge between declarative machines, Puppet journal, and MUD sessions.

The receive thread only matches and plans. A foreground drain performs every
send. No state or pending command is automatically replayed after disconnect.
Stage 2: live, in-memory instances only; durable recovery is a later stage.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
import json
import shlex
import threading
import time
import uuid

from .machines import Definition, MachineError, MachineInstance, Decision, load_machine
from .reaction import reaction_delay
from .terminal_text import strip_ansi

MAX_DEFINITIONS = 16
MAX_INSTANCES = 16
MAX_LINE = 4096
MAX_PENDING_COMMANDS = 16
MAX_RECEIPT_HISTORY = 64


@dataclass
class Dispatch:
    transition_id: str
    decision: Decision
    commands: tuple[str, ...]
    due: float
    receipt: dict
    index: int = 0
    action_id: str | None = None


@dataclass
class Run:
    id: str
    machine: MachineInstance
    writer: bool
    paused: bool = False
    step_budget: int = 0
    pending: Dispatch | None = None
    deadline: float | None = None
    last_error: str | None = None
    receipts: deque[dict] = field(default_factory=lambda: deque(maxlen=MAX_RECEIPT_HISTORY))


class MachineBridge:
    """Thread-safe decisions; outbound commands are exclusively foreground."""

    def __init__(self, *, clock=None, reaction=None):
        self.clock = clock or time.monotonic
        self.reaction = reaction or reaction_delay
        self.lock = threading.RLock()
        self.definitions: dict[str, Definition] = {}
        self.runs: dict[str, Run] = {}
        self.partial = ""
        self.journal = None
        self.session_id: str | None = None
        self.character: str | None = None

    def attach(self, *, journal=None, character=None):
        with self.lock:
            self.journal = journal
            self.session_id = journal.session_id if journal is not None else None
            self.character = character

    @staticmethod
    def _has_send(definition: Definition) -> bool:
        for state in definition.states.values():
            batches = [state.on_enter]
            batches.extend(transition.actions for transition in state.transitions)
            if state.on_timeout is not None:
                batches.append(state.on_timeout.actions)
            if any(action.kind == "send" for batch in batches for action in batch):
                return True
        return False

    def _writer_taken(self) -> bool:
        return any(run.writer and (run.machine.status == "running" or run.pending is not None)
                   for run in self.runs.values())

    def load(self, path: str) -> dict:
        definition = load_machine(Path(path))
        with self.lock:
            if definition.name not in self.definitions and len(self.definitions) >= MAX_DEFINITIONS:
                raise MachineError("machine definition limit reached")
            if definition.name in self.definitions and any(
                run.machine.definition.name == definition.name and run.machine.status == "running"
                for run in self.runs.values()
            ):
                raise MachineError("cannot replace a machine definition while an instance is active")
            self.definitions[definition.name] = definition
            return {"name": definition.name, "states": len(definition.states), "loaded": True}

    def list(self) -> dict:
        with self.lock:
            return {"definitions": sorted(self.definitions),
                    "instances": [self._summary(r) for r in self.runs.values()]}

    def _summary(self, run: Run) -> dict:
        return {"id": run.id, "machine": run.machine.definition.name,
                "state": run.machine.state, "status": self._status(run),
                "writer": run.writer, "paused": run.paused,
                "pending": run.pending is not None, "deadline": run.deadline,
                "last_error": run.last_error}

    @staticmethod
    def _status(run: Run) -> str:
        return "recovery_required" if run.machine.status == "recovery_required" else (
            "stopped" if run.machine.status == "stopped" else (
            "paused" if run.paused and run.machine.status == "running" else run.machine.status))

    def _get(self, selector: str | None) -> Run:
        if selector is None:
            if not self.runs:
                raise MachineError("no machine instances")
            return next(reversed(self.runs.values()))
        if selector not in self.runs:
            raise MachineError(f"unknown machine instance: {selector}")
        return self.runs[selector]

    def status(self, selector: str | None = None) -> dict:
        with self.lock:
            return self._summary(self._get(selector)) if selector else self.list()

    def inspect(self, selector: str | None = None) -> dict:
        with self.lock:
            run = self._get(selector)
            return {**self._summary(run),
                    "variables": dict(run.machine.variables),
                    "receipt": dict(run.receipts[-1]) if run.receipts else None}

    def trace(self, selector: str | None = None) -> dict:
        with self.lock:
            run = self._get(selector)
            # Bound IPC payloads independently of internal receipt retention.
            selected = []
            size = 0
            for receipt in reversed(run.receipts):
                row = {**receipt,
                       "commands": [cmd[:512] for cmd in receipt.get("commands", [])[:8]],
                       "variables": {key: str(value)[:128]
                                     for key, value in list(receipt.get("variables", {}).items())[:24]},
                       "captures": {key: str(value)[:128]
                                    for key, value in list(receipt.get("captures", {}).items())[:24]}}
                n = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
                if selected and size + n > 24000:
                    break
                selected.append(row)
                size += n
                if len(selected) >= 12:
                    break
            return {"id": run.id, "trace": list(reversed(selected)),
                    "total": len(run.receipts)}

    def _arm_timeout(self, run: Run):
        if run.machine.status != "running" or run.paused or run.pending is not None:
            run.deadline = None
            return
        state = run.machine.definition.states[run.machine.state]
        run.deadline = self.clock() + state.timeout if state.timeout is not None else None

    def _plan(self, run: Run, decision: Decision, *, immediate: bool = False):
        if run.pending is not None:
            raise MachineError("one transition must finish before another can begin")
        command_list = tuple(decision.commands)
        if len(command_list) > MAX_PENDING_COMMANDS:
            raise MachineError("too many commands for one machine transition")
        # Dry observers still see planned commands, but never send them.
        delay = self.reaction(decision.captures.get("0") if decision.event == "recv" else None,
                              immediate=immediate) if command_list and run.writer else 0.0
        receipt = {"transition_id": "mt-" + uuid.uuid4().hex[:16],
                   "instance_id": run.id, "machine": run.machine.definition.name,
                   "from_state": decision.from_state, "to_state": decision.to_state,
                   "event": decision.event, "source_cursor": decision.cursor,
                   "source_session_id": self.session_id,
                   "captures": dict(decision.captures), "variables": dict(decision.variables),
                   "commands": list(command_list), "notifications": list(decision.notifications),
                   "reaction_delay_ms": round(delay * 1000), "action_id": None,
                   "status": "reserved" if command_list and run.writer else (
                       "observed" if command_list else "no_send")}
        run.receipts.append(receipt)
        run.deadline = None
        if command_list and run.writer or decision.notifications:
            run.pending = Dispatch(receipt["transition_id"], decision,
                                   command_list if run.writer else (),
                                   self.clock() + delay, receipt)
        else:
            self._arm_timeout(run)
        return receipt

    def start(self, name: str, *, observe: bool = False, immediate: bool = False) -> dict:
        with self.lock:
            if name not in self.definitions:
                raise MachineError(f"machine not loaded: {name}")
            if len(self.runs) >= MAX_INSTANCES:
                raise MachineError("machine instance limit reached")
            definition = self.definitions[name]
            writer = self._has_send(definition) and not observe
            if writer and self.character is None:
                raise MachineError("write-capable machines require a character session started with --login")
            if writer and self._writer_taken():
                raise MachineError("another machine owns write control for this character")
            machine = MachineInstance(definition)
            run = Run("sm-" + uuid.uuid4().hex[:12], machine, writer)
            decision = machine.start()
            self.runs[run.id] = run
            self._plan(run, decision, immediate=immediate)
            return self._summary(run)

    def pause(self, selector: str) -> dict:
        with self.lock:
            run = self._get(selector)
            if run.machine.status != "running":
                raise MachineError("only running machines can be paused")
            run.paused = True
            run.deadline = None
            return self._summary(run)

    def resume(self, selector: str) -> dict:
        with self.lock:
            run = self._get(selector)
            if run.machine.status != "running":
                raise MachineError("machine cannot resume without explicit recovery")
            run.paused = False
            run.step_budget = 0
            self._arm_timeout(run)
            return self._summary(run)

    def stop(self, selector: str) -> dict:
        with self.lock:
            run = self._get(selector)
            if run.pending is not None:
                run.pending.receipt["status"] = ("outcome_unknown" if run.pending.index else "cancelled")
                run.pending = None
            run.machine.status = "stopped"
            run.paused = False
            run.step_budget = 0
            run.deadline = None
            return self._summary(run)

    def step(self, selector: str) -> dict:
        """While paused, allow exactly one already planned outbound command."""
        with self.lock:
            run = self._get(selector)
            if run.machine.status != "running" or not run.paused or run.pending is None:
                raise MachineError("step requires a paused running instance with a pending action")
            if run.pending.index >= len(run.pending.commands):
                raise MachineError("no pending MUD command to step")
            run.step_budget = 1
            return self._summary(run)

    def feed(self, text: str, *, cursor: int | None = None):
        """Only the real receiver loop may call feed; client events never enter here."""
        if not text:
            return
        with self.lock:
            buffer = (self.partial + text).replace("\r\n", "\n").replace("\r", "\n")
            lines = buffer.split("\n")
            self.partial = lines.pop()[-MAX_LINE:]
            for raw in lines:
                line = strip_ansi(raw)[:MAX_LINE]
                if not line:
                    continue
                for run in tuple(self.runs.values()):
                    if run.machine.status != "running" or run.paused or run.pending is not None:
                        continue
                    try:
                        decision = run.machine.receive_line(line, cursor=cursor)
                        if decision is not None:
                            self._plan(run, decision)
                    except MachineError as exc:
                        run.machine.status = "recovery_required"
                        run.last_error = f"transition rejected: {exc}"
                        run.deadline = None

    def on_disconnect(self):
        with self.lock:
            self.partial = ""
            for run in self.runs.values():
                if run.machine.status != "running" and run.pending is None:
                    continue
                had_pending = run.pending is not None
                if had_pending:
                    run.pending.receipt["status"] = (
                        "outcome_unknown" if run.pending.index else "cancelled")
                    run.pending.receipt["error"] = "session disconnected"
                    run.pending = None
                if run.machine.status == "running" or had_pending:
                    run.machine.status = "recovery_required"
                    run.last_error = "disconnected; no automatic replay or resume"
                run.deadline = None
                run.paused = False

    def drain(self, controller):
        """Foreground-only one command or one timeout per call."""
        with self.lock:
            if controller.current_connection() is None or not getattr(controller, "trigger_ready", True):
                return
            for run in list(self.runs.values()):
                if run.machine.status == "recovery_required" or run.machine.status == "stopped":
                    continue
                if run.paused and run.step_budget == 0:
                    continue
                if run.deadline is not None and self.clock() >= run.deadline and run.pending is None:
                    run.deadline = None
                    try:
                        decision = run.machine.timeout()
                        if decision is not None:
                            self._plan(run, decision)
                    except MachineError as exc:
                        run.machine.status = "recovery_required"
                        run.last_error = f"timeout transition rejected: {exc}"
                    return
                pending = run.pending
                if pending is None or (pending.due > self.clock() and run.step_budget == 0):
                    continue
                if pending.index == 0 and pending.commands and run.writer and pending.action_id is None:
                    journal = self.journal
                    if journal is not None:
                        try:
                            with controller.transcript.lock:
                                transcript = controller.transcript
                                path = transcript.path if transcript.mode in {"text", "plain"} else None
                                row = journal.start_action(
                                    list(pending.commands), durable=path is not None, log_path=path,
                                    origin="machine", machine_id=run.id,
                                    machine_name=run.machine.definition.name,
                                    transition_id=pending.transition_id,
                                    source_cursor=pending.decision.cursor,
                                    source_session_id=self.session_id,
                                    captures=pending.decision.captures,
                                    matched_text=pending.decision.captures.get("0"))
                            pending.action_id = row["action_id"]
                            pending.receipt["action_id"] = pending.action_id
                        except (OSError, ValueError, RuntimeError) as exc:
                            pending.receipt.update(status="failed", error=f"journal unavailable: {exc}")
                            run.pending = None
                            run.machine.status = "recovery_required"
                            run.last_error = "journal could not reserve dispatch"
                            return
                if pending.index < len(pending.commands):
                    command = pending.commands[pending.index]
                    # Bypass all local // dispatch; controller sends only a MUD line.
                    try:
                        controller.send_line(command, secret=False, remember=False)
                    except (OSError, RuntimeError, ValueError) as exc:
                        pending.receipt.update(status="outcome_unknown", error=str(exc))
                        run.machine.status = "recovery_required"
                        run.last_error = str(exc)
                        run.pending = None
                        return
                    pending.index += 1
                    pending.receipt["status"] = (
                        "sent" if pending.index == len(pending.commands) else "sending")
                    if pending.index < len(pending.commands):
                        pending.due = self.clock() + max(0, controller.profile.paste_line_delay)
                if run.paused and run.step_budget:
                    run.step_budget -= 1
                if pending.index == len(pending.commands):
                    if pending.decision.notifications:
                        # Notifications are safe local status messages, never server input.
                        controller.ui.set_status("; ".join(pending.decision.notifications)[:256])
                    run.pending = None
                    self._arm_timeout(run)
                return

    def execute(self, operation: str, args: list[str]) -> dict:
        """Shared local + Puppet socket command contract (no direct network sends)."""
        if len(args) > 4 or any(not isinstance(a, str) or len(a) > 512 for a in args):
            raise MachineError("invalid machine operation arguments")
        if operation == "load" and len(args) == 1:
            return self.load(args[0])
        if operation == "list" and not args:
            return self.list()
        if operation == "start" and args:
            name = args[0]
            options = args[1:]
            if len(set(options)) != len(options) or any(x not in {"--observe", "--immediate"} for x in options):
                raise MachineError("start supports --observe and --immediate only")
            return self.start(name, observe="--observe" in options, immediate="--immediate" in options)
        if operation in {"status", "inspect", "trace"} and len(args) <= 1:
            return getattr(self, operation)(*args)
        if operation in {"pause", "resume", "stop", "step"} and len(args) == 1:
            return getattr(self, operation)(args[0])
        raise MachineError("usage: //machine load FILE | list | start NAME [--observe] [--immediate] | status [ID] | inspect [ID] | trace [ID] | pause/resume/step/stop ID")


def handle_machine_command(line: str, bridge: MachineBridge | None) -> str | None:
    if line != "//machine" and not line.startswith("//machine "):
        return None
    try:
        words = shlex.split(line[len("//machine"):].strip())
        if len(words) == 2 and words[0] == "validate":
            definition = load_machine(words[1])
            return f"Machine valid: {definition.name} ({len(definition.states)} states, initial={definition.initial})"
        if bridge is None:
            raise MachineError("machine bridge unavailable")
        if not words:
            return json.dumps(bridge.list(), ensure_ascii=False)
        return json.dumps(bridge.execute(words[0], words[1:]), ensure_ascii=False)
    except (MachineError, OSError, ValueError) as exc:
        return f"Machine error: {exc}"
