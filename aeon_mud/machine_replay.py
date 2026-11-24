"""Read-only, deterministic historical machine replay and Mermaid diagrams.

This module has no live controller, Puppet socket, scheduler or persistence
access. Replay interprets a recorded stream only; it NEVER sends a MUD command
or updates a live instance. Its commands are proposals, not historical facts.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Iterator

from .machine_persistence import public_value, sanitized_receipt
from .machines import Definition, Decision, MachineError, MachineInstance, load_machine

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 50000
MAX_RECORD_BYTES = 65536
MAX_DECISIONS = 1024
MAX_DISPLAY = 24000
FORMAT_CHOICES = ("auto", "jsonl", "text")


@dataclass(frozen=True)
class ReplayEvent:
    kind: str
    text: str
    cursor: int
    session_id: str
    timestamp: str | None
    source_line: int


def _bounded_file(source: str | Path) -> Path:
    path = Path(source).expanduser()
    if path.is_symlink() or not path.is_file():
        raise MachineError("replay input must be a regular file (symlinks rejected)")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise MachineError("replay input exceeds 8 MiB")
    if path.name.endswith(".dpapi.jsonl"):
        raise MachineError("encrypted transcripts require explicit offline decryption first")
    return path


def _format(path: Path, value: str) -> str:
    if value not in FORMAT_CHOICES:
        raise MachineError(f"replay format must be one of {', '.join(FORMAT_CHOICES)}")
    if value != "auto":
        return value
    if path.suffix.lower() == ".jsonl":
        return "jsonl"
    raise MachineError("ambiguous text log: specify --format text explicitly")


def _validate_timestamp(value, *, line: int):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise MachineError(f"invalid replay timestamp at line {line}")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MachineError(f"invalid replay timestamp at line {line}") from exc
    return value


def _events_jsonl(path: Path) -> Iterator[ReplayEvent]:
    last_by_session: dict[str, int] = {}
    seen_sessions: set[str] = set()
    current_session = None
    with path.open("rb") as file:
        for line_number, raw in enumerate(file, 1):
            if line_number > MAX_RECORDS:
                raise MachineError("replay exceeds 50000 records")
            if len(raw) > MAX_RECORD_BYTES:
                raise MachineError(f"oversized replay record at line {line_number}")
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise MachineError(f"invalid JSONL at line {line_number}") from exc
            if not isinstance(record, dict):
                raise MachineError(f"JSONL line {line_number} must be an object")
            kind = record.get("kind", "event")
            if kind not in ("event", "action", "timeout"):
                raise MachineError(f"unsupported replay record kind at line {line_number}")
            session = record.get("session_id", "file")
            if not isinstance(session, str) or not 1 <= len(session) <= 128:
                raise MachineError(f"invalid session ID at line {line_number}")
            if session != current_session:
                if session in seen_sessions:
                    raise MachineError("interleaved or repeated session segments are ambiguous")
                seen_sessions.add(session)
                current_session = session
            sequence = record.get("seq", line_number)
            if type(sequence) is not int or not 1 <= sequence <= 2**53:
                raise MachineError(f"invalid cursor at line {line_number}")
            if sequence <= last_by_session.get(session, 0):
                raise MachineError(f"replay cursor not increasing at line {line_number}")
            last_by_session[session] = sequence
            timestamp = _validate_timestamp(record.get("ts"), line=line_number)
            if kind == "action":
                continue
            if kind == "timeout":
                if record.get("event", "timeout") != "timeout":
                    raise MachineError(f"invalid timeout event at line {line_number}")
                yield ReplayEvent("timeout", "", sequence, session, timestamp, line_number)
                continue
            direction = record.get("direction")
            if direction not in ("recv", "send", "client"):
                raise MachineError(f"invalid event direction at line {line_number}")
            value = record.get("text")
            if not isinstance(value, str) or len(value) > MAX_RECORD_BYTES:
                raise MachineError(f"invalid event text at line {line_number}")
            if type(record.get("secret", False)) is not bool:
                raise MachineError(f"invalid secret flag at line {line_number}")
            if record.get("secret", False) or direction != "recv":
                continue
            # Local confirmations intentionally use recv in current transcripts,
            # but are *never* MUD input to the live machine. Exclude them here.
            if value.startswith("[client] ") or value == "[redacted]":
                continue
            yield ReplayEvent("recv", value, sequence, session, timestamp, line_number)


def _events_text(path: Path) -> Iterator[ReplayEvent]:
    """Best-effort compatibility: text logs have NO trustworthy event cursors."""
    with path.open("rb") as file:
        for number, raw in enumerate(file, 1):
            if number > MAX_RECORDS:
                raise MachineError("text replay exceeds 50000 lines")
            if len(raw) > MAX_RECORD_BYTES:
                raise MachineError(f"text log line {number} exceeds size limit")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MachineError(f"invalid UTF-8 in text log line {number}") from exc
            if text.startswith("> ") or text.startswith("[client] ") or re.match(
                r"^\s*--- log started \d{4}-\d\d-\d\d", text
            ):
                continue
            if text.strip() == "":
                continue
            yield ReplayEvent("recv", text, number, "text", None, number)


def read_events(path: str | Path, *, format: str = "auto") -> tuple[Iterator[ReplayEvent], str]:
    source = _bounded_file(path)
    fmt = _format(source, format)
    return (_events_jsonl(source) if fmt == "jsonl" else _events_text(source), fmt)


def _public_decision(decision: Decision, session_id: str, source_line: int) -> dict:
    # A preview is NOT a proof of delivery. Redact raw command bodies and
    # secret-named captures by default, like the Stage 4 durable ledger.
    row = {
        "from_state": decision.from_state,
        "to_state": decision.to_state,
        "event": decision.event,
        "cursor": decision.cursor,
        "session_id": session_id,
        "source_line": source_line,
        "transition_index": decision.transition_index,
        "transition_source": decision.transition_source,
        "stack_depth": decision.stack_depth,
        "retry_attempt": decision.retry_attempt,
        "captures": decision.captures,
        "variables": decision.variables,
        "commands": list(decision.commands),
        "notifications": list(decision.notifications),
    }
    return sanitized_receipt(row)


def replay(definition: Definition, source: str | Path, *, format: str = "auto") -> dict:
    iterator, fmt = read_events(source, format=format)
    instance = MachineInstance(definition)
    trace: list[dict] = []
    visited = Counter()
    processed = 0
    received = 0
    sessions = 0
    current_session = None
    stopped_at_session_boundary = False

    def collect(decision: Decision, sid: str, line: int):
        if len(trace) >= MAX_DECISIONS:
            raise MachineError("replay exceeds 1024 transitions")
        row = _public_decision(decision, sid, line)
        trace.append(row)
        if decision.from_state is not None:
            visited[(decision.transition_source or decision.from_state,
                     decision.transition_index,
                     decision.event, decision.to_state)] += 1

    collect(instance.start(), "replay", 0)
    for event in iterator:
        processed += 1
        if event.session_id != current_session:
            sessions += 1
            if current_session is not None:
                # Live machines enter recovery_required on disconnect; offline
                # replays must never automatically carry a running state into
                # a new session or assemble cross-session partial lines.
                instance.partial = ""
                stopped_at_session_boundary = True
                if instance.status == "running":
                    instance.status = "recovery_required"
            current_session = event.session_id
        if instance.status != "running":
            continue
        if event.kind == "timeout":
            decision = instance.timeout(cursor=event.cursor)
            if decision is not None:
                collect(decision, event.session_id, event.source_line)
            continue
        received += 1
        for decision in instance.feed(event.text, cursor=event.cursor):
            collect(decision, event.session_id, event.source_line)
    return {
        "machine": definition.name,
        "format": fmt,
        "approximate": fmt == "text",
        "offline": True,
        "effects_sent": False,
        "timeouts": "explicit-events-only",
        "processed_events": processed,
        "received_events": received,
        "session_count": sessions,
        "stopped_at_session_boundary": stopped_at_session_boundary,
        "state": instance.state,
        "status": instance.status,
        "variables": public_value(instance.variables),
        "transition_count": len(trace) - 1,
        "trace": trace,
    }


def mermaid(definition: Definition, report: dict | None = None) -> str:
    """State diagram with hierarchy, calls/returns, global edges and replay counts."""
    counts = Counter()
    visited_states = set()
    current = None
    if report is not None:
        current = report.get("state")
        for row in report.get("trace", []):
            prev, next_state = row.get("from_state"), row.get("to_state")
            if prev is not None:
                counts[(row.get("transition_source") or prev, row.get("transition_index"),
                        row.get("event"), next_state)] += 1
            if isinstance(next_state, str):
                visited_states.add(next_state)
    lines = ["stateDiagram-v2", f"    [*] --> {definition.initial}"]
    def visited(source, index, kind, dest):
        count = counts[(source, index, kind, dest)]
        return f" (visited {count}x)" if count else ""

    for name, state in definition.states.items():
        if state.parent is not None:
            # Use safe compiler-validated identifiers, not untrusted match text.
            lines.append(f'    state "{name} (inherits {state.parent})" as {name}')
        for number, transition in enumerate(state.transitions):
            target = (definition.submachines[transition.call].initial if transition.call
                      else transition.goto)
            label = "call " + transition.call if transition.call else f"match {number + 1}"
            lines.append(f"    {name} --> {target}: {label}{visited(name, number, 'recv', target)}")
            if transition.call is not None:
                for child_name, child in definition.states.items():
                    if (child.final and
                        child_name.startswith(transition.call + "__")):
                        lines.append(f"    {child_name} --> {transition.return_to}: return")
        if state.on_timeout is not None:
            target = state.on_timeout.goto
            if state.retry_limit:
                lines.append(f"    {name} --> {name}: retry max {state.retry_limit}")
            lines.append(f"    {name} --> {target}: timeout{visited(name, None, 'timeout', target)}")
        if state.final and "__" not in name:
            lines.append(f"    {name} --> [*]")
    for index, transition in enumerate(definition.global_transitions):
        lines.append(f"    global_guard --> {transition.goto}: global {index + 1}")
    if definition.global_transitions:
        lines.append('    state "GLOBAL" as global_guard')
    for event, transition in definition.events.items():
        lines.append(f"    global_guard --> {transition.goto}: {event}")
    if visited_states:
        lines += [
            "    classDef replayVisited fill:#dbeafe,stroke:#2563eb,stroke-width:2px",
            f"    class {','.join(sorted(visited_states))} replayVisited",
        ]
    if current in definition.states:
        lines += [
            "    classDef replayCurrent fill:#fef3c7,stroke:#b45309,stroke-width:3px",
            f"    class {current} replayCurrent",
        ]
    return "\n".join(lines) + "\n"


def summary(report: dict) -> dict:
    return {k: v for k, v in report.items() if k != "trace"}


def local_replay(args: list[str], bridge=None) -> str:
    """Read-only local //machine replay command with bounded UI output."""
    if not args:
        raise MachineError("usage: //machine replay MACHINE.yaml EVENTS.jsonl [--format jsonl|text] [--diagram]")
    fmt = "auto"
    diagram = False
    paths = []
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--diagram":
            diagram = True
            i += 1
        elif token == "--format" and i + 1 < len(args):
            fmt = args[i + 1]
            i += 2
        elif token.startswith("--"):
            raise MachineError(f"invalid replay option: {token}")
        else:
            paths.append(token)
            i += 1
    if len(paths) == 2:
        machine_path, events_path = paths
    elif len(paths) == 1 and bridge is not None and len(bridge.definition_paths) == 1:
        name = next(iter(bridge.definition_paths))
        machine_path = bridge.definition_paths[name]["path"]
        events_path = paths[0]
    else:
        raise MachineError("replay needs MACHINE.yaml EVENTS (or one loaded machine and EVENTS)")
    definition = load_machine(machine_path)
    result = replay(definition, events_path, format=fmt)
    if diagram:
        value = mermaid(definition, result)
        if len(value) > MAX_DISPLAY:
            raise MachineError("Mermaid output exceeds local display limit")
        return value
    result = summary(result)
    value = json.dumps(result, ensure_ascii=False)
    if len(value) > MAX_DISPLAY:
        raise MachineError("replay summary exceeds local display limit")
    return value
