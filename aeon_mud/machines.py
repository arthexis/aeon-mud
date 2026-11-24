"""Versioned, deterministic MUD state machines. No socket writes or arbitrary code.

Stage 1: compile strict YAML; run a pure event/timeout interpreter; return effects
for a future, separately accepted foreground scheduler to dispatch.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
import sys
from typing import Any


from .terminal import compile_watch_pattern
from .terminal_text import strip_ansi
from .triggers import PLACEHOLDER, duration

MAX_DEFINITION_BYTES = 64 * 1024
MAX_STATES = 32
MAX_TRANSITIONS = 32
MAX_ACTIONS = 16
MAX_VARIABLES = 64
MAX_LINE = 4096
MAX_TRACE = 128
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MACHINE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class MachineError(ValueError):
    """Invalid definition or deterministic-machine input."""


def _yaml_loader():
    """Import YAML only when a machine file is loaded (normal client startup is unchanged)."""
    try:
        import yaml
    except ImportError as exc:
        raise MachineError("PyYAML is required for YAML machine definitions; install the package dependencies") from exc

    class _YamlLoader(yaml.SafeLoader):
        """Reject aliases and duplicate keys to avoid ambiguous or cyclic models."""

        def compose_node(self, parent, index):
            if self.check_event(yaml.AliasEvent):
                raise MachineError("YAML aliases are not supported")
            return super().compose_node(parent, index)

        def construct_mapping(self, node, deep=False):
            if not isinstance(node, yaml.MappingNode):
                raise MachineError("expected a mapping")
            mapping = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str):
                    raise MachineError("YAML mapping keys must be strings")
                if key in mapping:
                    raise MachineError(f"duplicate YAML key: {key}")
                mapping[key] = self.construct_object(value_node, deep=deep)
            return mapping

    return yaml, _YamlLoader


def _mapping(value: Any, where: str, allowed: set[str]) -> dict:
    if not isinstance(value, dict):
        raise MachineError(f"{where} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise MachineError(f"unknown {where} keys: {', '.join(sorted(unknown))}")
    return value


def _name(value: Any, where: str, pattern=IDENTIFIER) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise MachineError(f"{where} has an invalid name")
    return value


def _scalar(value: Any, where: str) -> bool:
    if type(value) not in (str, int, float, bool) or isinstance(value, float) and not math.isfinite(value):
        raise MachineError(f"{where} must be a finite scalar")
    if len(str(value)) > 256:
        raise MachineError(f"{where} is too long")
    return True


_ALLOWED_COMPARISONS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)
_ALLOWED_FUNCTIONS = {"int", "float", "str"}


def _check_expression(node: ast.AST) -> None:
    if isinstance(node, ast.Expression):
        _check_expression(node.body)
    elif isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        for item in node.values:
            _check_expression(item)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        _check_expression(node.operand)
    elif isinstance(node, ast.Compare) and all(isinstance(op, _ALLOWED_COMPARISONS) for op in node.ops):
        _check_expression(node.left)
        for part in node.comparators:
            _check_expression(part)
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _ALLOWED_FUNCTIONS and len(node.args) == 1 and not node.keywords:
        _check_expression(node.args[0])
    elif isinstance(node, ast.Name):
        _name(node.id, "condition variable")
        if node.id.startswith("__"):
            raise MachineError("private names are not allowed")
    elif isinstance(node, ast.Constant) and type(node.value) in (int, float, str, bool):
        _scalar(node.value, "condition literal")
    else:
        raise MachineError("unsafe condition syntax (only literals, names, comparisons, and/or/not, int/float/str casts allowed)")


def compile_condition(expr: str | None) -> ast.Expression | None:
    if expr is None:
        return None
    if not isinstance(expr, str) or not expr or len(expr) > 256:
        raise MachineError("condition must be nonempty text of at most 256 characters")
    try:
        node = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise MachineError(f"invalid condition: {exc.msg}") from exc
    if sum(1 for _ in ast.walk(node)) > 64:
        raise MachineError("condition too complex")
    _check_expression(node)
    return node


def _cast(func: str, arg: Any) -> Any:
    if type(arg) not in (str, int, float, bool) or len(str(arg)) > 64:
        raise MachineError("invalid cast operand")
    try:
        result = {"int": int, "float": float, "str": str}[func](arg)
    except (ValueError, TypeError, OverflowError) as exc:
        raise MachineError(f"invalid {func} conversion") from exc
    if type(result) in (int, float) and (not math.isfinite(result) or abs(result) > 1e12):
        raise MachineError("numeric value outside allowed bounds")
    return result


def _evaluate(node: ast.AST, values: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, values)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in values:
            raise MachineError(f"unknown condition variable: {node.id}")
        return values[node.id]
    if isinstance(node, ast.Call):
        return _cast(node.func.id, _evaluate(node.args[0], values))
    if isinstance(node, ast.UnaryOp):
        return not bool(_evaluate(node.operand, values))
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(bool(_evaluate(item, values)) for item in node.values)
        return any(bool(_evaluate(item, values)) for item in node.values)
    if isinstance(node, ast.Compare):
        current = _evaluate(node.left, values)
        for op, right in zip(node.ops, node.comparators):
            nxt = _evaluate(right, values)
            try:
                if isinstance(op, ast.Eq):
                    passed = current == nxt
                elif isinstance(op, ast.NotEq):
                    passed = current != nxt
                elif isinstance(op, ast.Lt):
                    passed = current < nxt
                elif isinstance(op, ast.LtE):
                    passed = current <= nxt
                elif isinstance(op, ast.Gt):
                    passed = current > nxt
                else:
                    passed = current >= nxt
            except TypeError as exc:
                raise MachineError("incompatible condition operands") from exc
            if not passed:
                return False
            current = nxt
        return True
    raise MachineError("unsupported condition node")


def evaluate_condition(condition: ast.Expression | None, values: dict[str, Any]) -> bool:
    return True if condition is None else bool(_evaluate(condition, values))


def expand(template: str, values: dict[str, Any], *, send: bool) -> str:
    def substitute(match: re.Match[str]) -> str:
        key = match.group(1) or match.group(2)
        if key not in values or values[key] is None:
            raise MachineError(f"missing template variable: {key}")
        return str(values[key])
    result = PLACEHOLDER.sub(substitute, template)
    if len(result) > 4096 or "\r" in result or "\n" in result:
        raise MachineError("expanded action must be one line (max 4096 characters)")
    if send and (not result or result.lstrip().startswith("//")):
        raise MachineError("send action cannot be empty or run local // commands")
    return result


@dataclass(frozen=True)
class Action:
    kind: str
    value: Any


@dataclass(frozen=True)
class Transition:
    goto: str
    pattern: str | None = None
    search: bool = False
    use_regex: bool = False
    when: ast.Expression | None = None
    actions: tuple[Action, ...] = ()
    regex: re.Pattern[str] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class State:
    name: str
    final: bool = False
    on_enter: tuple[Action, ...] = ()
    transitions: tuple[Transition, ...] = ()
    timeout: float | None = None
    on_timeout: Transition | None = None


@dataclass(frozen=True)
class Definition:
    name: str
    initial: str
    variables: dict[str, Any]
    states: dict[str, State]
    scope: str = "character"
    version: int = 1


def _actions(data: Any, where: str) -> tuple[Action, ...]:
    if data is None:
        return ()
    if not isinstance(data, list) or len(data) > MAX_ACTIONS:
        raise MachineError(f"{where} requires a list of at most {MAX_ACTIONS} actions")
    actions = []
    for item in data:
        if not isinstance(item, dict) or len(item) != 1:
            raise MachineError(f"{where} actions need exactly one key")
        kind, value = next(iter(item.items()))
        if kind in {"send", "notify"}:
            if not isinstance(value, str) or not value or len(value) > 4096 or "\n" in value or "\r" in value:
                raise MachineError(f"invalid {kind} action")
        elif kind == "set":
            _mapping(value, "set action", set(value) if isinstance(value, dict) else set())
            if len(value) != 1:
                raise MachineError("set action needs exactly one variable")
            var, template = next(iter(value.items()))
            _name(var, "set variable")
            if not isinstance(template, str) or len(template) > 4096:
                raise MachineError("set value must be template text")
        else:
            raise MachineError(f"unsupported action: {kind}")
        actions.append(Action(kind, value))
    return tuple(actions)


def _transition(raw: Any, where: str, *, timeout=False) -> Transition:
    allowed = {"goto", "actions"} if timeout else {"match", "re", "search", "when", "goto", "actions"}
    value = _mapping(raw, where, allowed)
    goto = _name(value.get("goto"), f"{where}.goto")
    pattern = None
    regex = None
    if not timeout:
        pattern = value.get("match")
        if not isinstance(pattern, str) or not pattern:
            raise MachineError(f"{where}.match must be nonempty text")
        if type(value.get("re", False)) is not bool or type(value.get("search", False)) is not bool:
            raise MachineError("re and search must be boolean")
        try:
            regex = compile_watch_pattern(pattern, use_regex=value.get("re", False))
        except ValueError as exc:
            raise MachineError(str(exc)) from exc
    return Transition(goto=goto, pattern=pattern, regex=regex, search=value.get("search", False),
                      use_regex=value.get("re", False),
                      when=compile_condition(value.get("when")) if not timeout else None,
                      actions=_actions(value.get("actions"), f"{where}.actions"))


def compile_machine(raw: Any) -> Definition:
    root = _mapping(raw, "machine", {"name", "version", "scope", "initial", "variables", "states"})
    if type(root.get("version")) is not int or root["version"] != 1:
        raise MachineError("machine version must be 1")
    name = _name(root.get("name"), "machine", MACHINE_NAME)
    if root.get("scope", "character") != "character":
        raise MachineError("only character scope is supported in v1")
    initial = _name(root.get("initial"), "initial state")
    values = root.get("variables", {})
    if not isinstance(values, dict) or len(values) > MAX_VARIABLES:
        raise MachineError("variables must be a small mapping")
    for key, val in values.items():
        _name(key, "variable")
        _scalar(val, f"variable {key}")
    definitions = root.get("states")
    if not isinstance(definitions, dict) or not 1 <= len(definitions) <= MAX_STATES:
        raise MachineError(f"states requires 1-{MAX_STATES} items")
    states = {}
    for state_name, raw_state in definitions.items():
        _name(state_name, "state")
        obj = _mapping(raw_state, f"state {state_name}",
                       {"final", "on_enter", "transitions", "timeout", "on_timeout"})
        final = obj.get("final", False)
        if type(final) is not bool:
            raise MachineError(f"state {state_name}.final must be boolean")
        raw_transitions = obj.get("transitions", [])
        if not isinstance(raw_transitions, list) or len(raw_transitions) > MAX_TRANSITIONS:
            raise MachineError(f"state {state_name} has too many transitions")
        transitions = tuple(_transition(t, f"{state_name}.transition[{i}]")
                            for i, t in enumerate(raw_transitions))
        timeout = obj.get("timeout")
        timeout_seconds = None
        if timeout is not None:
            if not isinstance(timeout, str):
                raise MachineError("timeout must be a duration string such as 5s")
            timeout_seconds = duration(timeout)
            if timeout_seconds <= 0:
                raise MachineError("timeout must be positive")
        timeout_step = obj.get("on_timeout")
        if (timeout_seconds is None) != (timeout_step is None):
            raise MachineError("timeout and on_timeout must be specified together")
        on_timeout = (_transition(timeout_step, f"{state_name}.on_timeout", timeout=True)
                      if timeout_step is not None else None)
        if final and (transitions or timeout_seconds is not None):
            raise MachineError(f"final state {state_name} cannot have transitions or timeout")
        states[state_name] = State(state_name, final, _actions(obj.get("on_enter"), "on_enter"),
                                   transitions, timeout_seconds, on_timeout)
    if initial not in states:
        raise MachineError("initial state does not exist")
    for state in states.values():
        for transition in (*state.transitions, *((state.on_timeout,) if state.on_timeout else ())):
            if transition.goto not in states:
                raise MachineError(f"{state.name} refers to unknown state {transition.goto}")
    return Definition(name, initial, dict(values), states)


def load_machine(path: str | Path) -> Definition:
    source = Path(path).expanduser()
    if not source.is_file() or source.is_symlink():
        raise MachineError("machine definition must be a regular file")
    if source.stat().st_size > MAX_DEFINITION_BYTES:
        raise MachineError("machine definition exceeds 64 KiB")
    data = source.read_bytes()
    if len(data) > MAX_DEFINITION_BYTES:
        raise MachineError("machine definition exceeds 64 KiB")
    yaml, loader = _yaml_loader()
    try:
        raw = yaml.load(data.decode("utf-8"), Loader=loader)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise MachineError(f"invalid YAML: {exc}") from exc
    return compile_machine(raw)


@dataclass(frozen=True)
class Decision:
    from_state: str | None
    to_state: str
    event: str
    captures: dict[str, Any]
    variables: dict[str, Any]
    commands: tuple[str, ...]
    notifications: tuple[str, ...]
    cursor: int | None
    transition_index: int | None = None


class MachineInstance:
    """Pure deterministic interpreter. Does not transmit commands."""

    def __init__(self, definition: Definition):
        self.definition = definition
        self.state: str | None = None
        self.variables = dict(definition.variables)
        self.status = "created"
        self.trace: list[Decision] = []
        self.partial = ""

    def _commit(self, destination: str, actions: tuple[Action, ...], captures: dict[str, Any],
                *, event: str, cursor: int | None,
                transition_index: int | None = None) -> Decision:
        next_state = self.definition.states[destination]
        staged = {**self.variables, **captures}
        commands: list[str] = []
        notifications: list[str] = []
        for action in (*actions, *next_state.on_enter):
            if action.kind == "set":
                variable, template = next(iter(action.value.items()))
                staged[variable] = expand(template, staged, send=False)
            elif action.kind == "send":
                commands.append(expand(action.value, staged, send=True))
            else:
                notifications.append(expand(action.value, staged, send=False))
        if len(commands) > MAX_ACTIONS:
            raise MachineError("too many commands in one transition")
        decision = Decision(self.state, destination, event, dict(captures), dict(staged),
                            tuple(commands), tuple(notifications), cursor, transition_index)
        self.state = destination
        self.variables = staged
        self.status = "completed" if next_state.final else "running"
        self.trace.append(decision)
        if len(self.trace) > MAX_TRACE:
            del self.trace[:-MAX_TRACE]
        return decision

    def start(self) -> Decision:
        if self.status != "created":
            raise MachineError("machine instance already started")
        return self._commit(self.definition.initial, (), {}, event="start", cursor=None)

    def receive_line(self, line: str, *, cursor: int | None = None) -> Decision | None:
        if self.status != "running":
            return None
        clean = strip_ansi(line)[:MAX_LINE]
        current = self.definition.states[self.state]
        for index, transition in enumerate(current.transitions):
            match = transition.regex.search(clean) if transition.search else transition.regex.match(clean)
            if match is None:
                continue
            captures = {**{str(i): value for i, value in enumerate(match.groups(), 1)
                           if value is not None},
                        **{key: value for key, value in match.groupdict().items() if value is not None},
                        "0": match.group(0)}
            context = {**self.variables, **captures}
            if not evaluate_condition(transition.when, context):
                continue
            return self._commit(transition.goto, transition.actions, captures,
                                event="recv", cursor=cursor, transition_index=index)
        return None

    def timeout(self, *, cursor: int | None = None) -> Decision | None:
        if self.status != "running":
            return None
        transition = self.definition.states[self.state].on_timeout
        if transition is None:
            return None
        return self._commit(transition.goto, transition.actions, {}, event="timeout", cursor=cursor)

    def feed(self, chunk: str, *, cursor: int | None = None) -> list[Decision]:
        """Match completed incoming lines only; never interpret partial chunks."""
        if not isinstance(chunk, str):
            raise MachineError("incoming data must be text")
        text = (self.partial + chunk).replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        self.partial = lines.pop()[-MAX_LINE:]
        decisions = []
        for line in lines:
            decision = self.receive_line(line, cursor=cursor)
            if decision is not None:
                decisions.append(decision)
        return decisions


def main(argv: list[str] | None = None) -> int:
    """Validate locally or access an enabled character's owner-only Puppet socket."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in {"replay", "diagram"}:
        from importlib import import_module
        replay_module = import_module(".machine_replay", __package__)
        operation = arguments[0]
        parser = argparse.ArgumentParser(prog="aeon-machine " + operation)
        parser.add_argument("operation", choices=[operation])
        parser.add_argument("definition", help="Validated machine YAML definition")
        if operation == "replay":
            parser.add_argument("events", help="Existing JSONL event journal or explicit text log")
        else:
            parser.add_argument("events", nargs="?", help="Optional replay events for highlighting")
        parser.add_argument("--format", choices=["auto", "jsonl", "text"], default="auto")
        if operation == "replay":
            parser.add_argument("--diagram", action="store_true",
                                help="Output Mermaid instead of JSON trace")
        args = parser.parse_args(arguments)
        try:
            definition = load_machine(args.definition)
            report = (replay_module.replay(definition, args.events, format=args.format)
                      if args.events is not None else None)
            if operation == "diagram" or args.diagram:
                print(replay_module.mermaid(definition, report), end="")
            else:
                print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0
        except (OSError, MachineError, ValueError) as exc:
            print(f"aeon-machine: {exc}", file=sys.stderr)
            return 1
    if arguments and arguments[0] == "validate":
        parser = argparse.ArgumentParser(prog="aeon-machine validate")
        parser.add_argument("operation", choices=["validate"])
        parser.add_argument("file")
        args = parser.parse_args(arguments)
        try:
            definition = load_machine(args.file)
        except (OSError, MachineError) as exc:
            parser.exit(1, f"aeon-machine: {exc}\n")
        print(json.dumps({"valid": True, "name": definition.name,
                          "states": len(definition.states), "initial": definition.initial}))
        return 0
    parser = argparse.ArgumentParser(
        prog="aeon-machine",
        description="Control MUD machines and durable recovery over the owner-only Puppet socket. "
                    "Use 'aeon-machine validate FILE' for offline validation.")
    parser.add_argument("character", help="Active character with //puppet on")
    parser.add_argument("operation", choices=[
        "load", "list", "start", "status", "pause", "resume",
        "stop", "inspect", "trace", "step", "recover",
    ])
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(arguments)
    try:
        from .puppet import PuppetClient
        result = PuppetClient(args.character).machine(args.operation, *args.args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"aeon-machine: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
