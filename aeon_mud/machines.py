"""Versioned, deterministic MUD state machines. No socket writes or arbitrary code.

Stage 1: compile strict YAML; run a pure event/timeout interpreter; return effects
for a future, separately accepted foreground scheduler to dispatch.
"""
from __future__ import annotations

import argparse
import copy
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
MAX_STATES = 64
MAX_SUBMACHINES = 8
MAX_CALL_DEPTH = 4
MAX_GLOBAL_TRANSITIONS = 16
MAX_RETRIES = 3
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
    call: str | None = None
    return_to: str | None = None
    inputs: dict[str, str] = field(default_factory=dict)
    exports: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class State:
    name: str
    final: bool = False
    on_enter: tuple[Action, ...] = ()
    transitions: tuple[Transition, ...] = ()
    timeout: float | None = None
    on_timeout: Transition | None = None
    parent: str | None = None
    retry_limit: int = 0
    retry_backoff: float = 0.0
    retry_allow_send: bool = False


@dataclass(frozen=True)
class Submachine:
    name: str
    initial: str
    variables: dict[str, Any]


@dataclass(frozen=True)
class Definition:
    name: str
    initial: str
    variables: dict[str, Any]
    states: dict[str, State]
    scope: str = "character"
    version: int = 1
    global_transitions: tuple[Transition, ...] = ()
    events: dict[str, Transition] = field(default_factory=dict)
    submachines: dict[str, Submachine] = field(default_factory=dict)


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


def _variables(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or len(raw) > MAX_VARIABLES:
        raise MachineError(f"{where} must be a mapping with at most {MAX_VARIABLES} variables")
    for key, val in raw.items():
        _name(key, f"{where} variable")
        _scalar(val, f"{where}.{key}")
    return dict(raw)


def _templates(raw: Any, where: str) -> dict[str, str]:
    if not isinstance(raw, dict) or len(raw) > MAX_VARIABLES:
        raise MachineError(f"{where} must be a bounded mapping")
    for key, value in raw.items():
        _name(key, f"{where} key")
        if not isinstance(value, str) or len(value) > 256 or "\r" in value or "\n" in value:
            raise MachineError(f"{where} templates must be single-line text")
    return dict(raw)


def _transition(raw: Any, where: str, *, timeout=False, prefix="") -> Transition:
    allowed = ({"goto", "actions"} if timeout else
               {"match", "re", "search", "when", "goto", "actions",
                "call", "return_to", "with", "export"})
    value = _mapping(raw, where, allowed)
    call = value.get("call")
    if call is not None and timeout:
        raise MachineError("timeout transitions cannot call submachines")
    if call is not None:
        _name(call, f"{where}.call")
        if value.get("goto") is not None:
            raise MachineError("call transitions use return_to, not goto")
        target = _name(value.get("return_to"), f"{where}.return_to")
        goto = prefix + target
    else:
        if any(k in value for k in ("return_to", "with", "export")):
            raise MachineError("return_to/with/export require call")
        goto = prefix + _name(value.get("goto"), f"{where}.goto")
    pattern = None
    regex = None
    if not timeout:
        pattern = value.get("match")
        if not isinstance(pattern, str) or not 0 < len(pattern) <= 512:
            raise MachineError(f"{where}.match must be bounded nonempty text")
        if type(value.get("re", False)) is not bool or type(value.get("search", False)) is not bool:
            raise MachineError("re and search must be boolean")
        try:
            regex = compile_watch_pattern(pattern, use_regex=value.get("re", False))
        except ValueError as exc:
            raise MachineError(str(exc)) from exc
    return Transition(
        goto=goto, pattern=pattern, regex=regex,
        search=value.get("search", False), use_regex=value.get("re", False),
        when=compile_condition(value.get("when")) if not timeout else None,
        actions=_actions(value.get("actions"), f"{where}.actions"),
        call=call,
        return_to=goto if call else None,
        inputs=_templates(value.get("with", {}), f"{where}.with") if call else {},
        exports=_templates(value.get("export", {}), f"{where}.export") if call else {},
    )


def _parse_states(raw: Any, *, prefix: str = "") -> dict[str, State]:
    if not isinstance(raw, dict) or not 1 <= len(raw) <= MAX_STATES:
        raise MachineError(f"states requires 1-{MAX_STATES} items")
    states = {}
    for label, raw_state in raw.items():
        _name(label, "state")
        name = prefix + label
        obj = _mapping(raw_state, f"state {name}",
                       {"final", "parent", "on_enter", "transitions",
                        "timeout", "on_timeout", "retry"})
        final = obj.get("final", False)
        if type(final) is not bool:
            raise MachineError(f"state {name}.final must be boolean")
        parent = obj.get("parent")
        if parent is not None:
            parent = prefix + _name(parent, f"state {name}.parent")
            if parent == name:
                raise MachineError("a state cannot be its own parent")
        raw_transitions = obj.get("transitions", [])
        if not isinstance(raw_transitions, list) or len(raw_transitions) > MAX_TRANSITIONS:
            raise MachineError(f"state {name} has too many transitions")
        transitions = tuple(
            _transition(x, f"{name}.transition[{i}]", prefix=prefix)
            for i, x in enumerate(raw_transitions))
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
        on_timeout = (_transition(timeout_step, f"{name}.on_timeout",
                                  timeout=True, prefix=prefix)
                      if timeout_step is not None else None)
        actions = _actions(obj.get("on_enter"), f"{name}.on_enter")
        raw_retry = obj.get("retry")
        retry_limit = 0
        retry_backoff = 0.0
        allow_send = False
        if raw_retry is not None:
            retry = _mapping(raw_retry, f"{name}.retry",
                             {"max", "backoff", "allow_repeat_send"})
            retry_limit = retry.get("max")
            if type(retry_limit) is not int or not 1 <= retry_limit <= MAX_RETRIES:
                raise MachineError(f"{name}.retry.max must be 1-{MAX_RETRIES}")
            if timeout_seconds is None:
                raise MachineError(f"{name}.retry requires timeout/on_timeout")
            backoff = retry.get("backoff", "0s")
            if not isinstance(backoff, str):
                raise MachineError("retry.backoff must be a duration")
            retry_backoff = duration(backoff)
            if retry_backoff < 0 or retry_backoff > 60:
                raise MachineError("retry.backoff must be 0-60s")
            allow_send = retry.get("allow_repeat_send", False)
            if type(allow_send) is not bool:
                raise MachineError("retry.allow_repeat_send must be boolean")
            if any(a.kind == "send" for a in actions) and not allow_send:
                raise MachineError("retry of on_enter sends requires explicit allow_repeat_send")
        if final and (transitions or timeout_seconds is not None or retry_limit):
            raise MachineError(f"final state {name} cannot have transitions or timeout")
        states[name] = State(name, final, actions, transitions, timeout_seconds,
                             on_timeout, parent, retry_limit, retry_backoff, allow_send)
    return states


def compile_machine(raw: Any) -> Definition:
    root = _mapping(raw, "machine", {
        "name", "version", "scope", "initial", "variables", "states",
        "global_transitions", "events", "submachines",
    })
    if type(root.get("version")) is not int or root["version"] != 1:
        raise MachineError("machine version must be 1")
    name = _name(root.get("name"), "machine", MACHINE_NAME)
    if root.get("scope", "character") != "character":
        raise MachineError("only character scope is supported in v1")
    initial = _name(root.get("initial"), "initial state")
    values = _variables(root.get("variables", {}), "variables")
    states = _parse_states(root.get("states"))
    if initial not in states:
        raise MachineError("initial state does not exist")
    machines_raw = root.get("submachines", {})
    if not isinstance(machines_raw, dict) or len(machines_raw) > MAX_SUBMACHINES:
        raise MachineError("too many submachines")
    subs = {}
    for subname, spec in machines_raw.items():
        _name(subname, "submachine name")
        obj = _mapping(spec, f"submachine {subname}", {"initial", "variables", "states"})
        prefix = subname + "__"
        sub_initial = prefix + _name(obj.get("initial"), f"{subname}.initial")
        sub_states = _parse_states(obj.get("states"), prefix=prefix)
        if sub_initial not in sub_states:
            raise MachineError(f"submachine {subname} initial state missing")
        if not any(x.final for x in sub_states.values()):
            raise MachineError(f"submachine {subname} needs an explicit final return state")
        if any(x in states for x in sub_states):
            raise MachineError("duplicate submachine state key")
        states.update(sub_states)
        subs[subname] = Submachine(subname, sub_initial,
                                   _variables(obj.get("variables", {}), f"{subname}.variables"))
    if len(states) > MAX_STATES:
        raise MachineError(f"compiled machine exceeds {MAX_STATES} states")
    globals_raw = root.get("global_transitions", [])
    if not isinstance(globals_raw, list) or len(globals_raw) > MAX_GLOBAL_TRANSITIONS:
        raise MachineError("global_transitions must be bounded list")
    globals_ = tuple(_transition(t, f"global_transitions[{i}]")
                     for i, t in enumerate(globals_raw))
    events_raw = root.get("events", {})
    if not isinstance(events_raw, dict) or len(events_raw) > 3:
        raise MachineError("events must be a small mapping")
    if set(events_raw) - {"death", "disconnect", "stop"}:
        raise MachineError("supported events are death, disconnect and stop")
    events = {event: _transition(value, f"events.{event}", timeout=True)
              for event, value in events_raw.items()}
    # Hierarchies cannot escape into a different machine namespace.
    for state in states.values():
        seen = {state.name}
        node = state
        while node.parent is not None:
            if node.parent not in states or node.parent in seen:
                raise MachineError(f"invalid parent hierarchy for {state.name}")
            seen.add(node.parent)
            node = states[node.parent]
        for t in (*state.transitions, *((state.on_timeout,) if state.on_timeout else ())):
            if t.goto not in states or (not t.call and
                    state.name.split("__")[0] != t.goto.split("__")[0]
                    and ("__" in state.name or "__" in t.goto)):
                raise MachineError(f"{state.name} refers to invalid state {t.goto}")
            if t.call is not None:
                if t.call not in subs:
                    raise MachineError(f"unknown submachine: {t.call}")
                target = subs[t.call]
                if set(t.inputs) - set(target.variables):
                    raise MachineError("submachine call inputs must be declared variables")
                if set(t.exports) - set(target.variables):
                    raise MachineError("submachine exports must be declared local variables")
                for target_name in t.exports.values():
                    _name(target_name, "export destination")
        if state.retry_limit and state.parent is not None and state.timeout is None:
            raise MachineError("retry requires state-local timeout")
    for t in globals_:
        if t.goto not in root["states"] and t.goto not in states:
            raise MachineError(f"unknown global transition state: {t.goto}")
        if "__" in t.goto:
            raise MachineError("global transitions must target root states")
        if t.call is not None:
            raise MachineError("global transitions cannot call submachines")
    for event, transition in events.items():
        if "__" in transition.goto or transition.goto not in states:
            raise MachineError("system events must target root states")
        if any(a.kind == "send" for a in (
            *transition.actions, *states[transition.goto].on_enter)):
            raise MachineError("system events cannot dispatch MUD commands")
    return Definition(name, initial, values, states, global_transitions=globals_,
                      events=events, submachines=subs)


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
    transition_source: str | None = None
    stack_depth: int = 0
    retry_attempt: int = 0


class MachineInstance:
    """Pure, transactional interpreter with bounded hierarchical call frames."""

    def __init__(self, definition: Definition):
        self.definition = definition
        self.state: str | None = None
        self.variables = dict(definition.variables)
        self.frames: list[dict[str, Any]] = []
        self.retry_counts: dict[str, int] = {}
        self.status = "created"
        self.trace: list[Decision] = []
        self.partial = ""

    def _context(self, captures: dict[str, Any] | None = None) -> dict[str, Any]:
        values = dict(self.variables)
        if self.frames:
            values.update(self.frames[-1]["locals"])
        values.update(captures or {})
        return values

    def timeout_state(self) -> State | None:
        if self.state is None:
            return None
        node = self.definition.states[self.state]
        while True:
            if node.timeout is not None:
                return node
            if node.parent is None:
                return None
            node = self.definition.states[node.parent]

    def _commit(self, destination: str, actions: tuple[Action, ...], captures: dict[str, Any],
                *, event: str, cursor: int | None, transition_index: int | None = None,
                transition: Transition | None = None,
                transition_source: str | None = None,
                retry_attempt: int = 0) -> Decision:
        old_state = self.state
        root = dict(self.variables)
        frames = copy.deepcopy(self.frames)
        counters = dict(self.retry_counts)
        if transition_source == "*" and frames:
            # Global safety transitions always exit nested submachines.
            frames.clear()
        if frames:
            frames[-1]["locals"].update(captures)
        else:
            root.update(captures)
        commands: list[str] = []
        notifications: list[str] = []

        def context() -> dict[str, Any]:
            combined = dict(root)
            if frames:
                combined.update(frames[-1]["locals"])
            return combined

        def apply_actions(batch: tuple[Action, ...]) -> None:
            for action in batch:
                if action.kind == "set":
                    key, template = next(iter(action.value.items()))
                    value = expand(template, context(), send=False)
                    if frames and (key in frames[-1]["locals"] or key not in root):
                        frames[-1]["locals"][key] = value
                    else:
                        root[key] = value
                elif action.kind == "send":
                    commands.append(expand(action.value, context(), send=True))
                else:
                    notifications.append(expand(action.value, context(), send=False))
                if len(commands) > MAX_ACTIONS:
                    raise MachineError("too many commands in one transition")
                if len(root) > MAX_VARIABLES or any(
                    len(frame["locals"]) > MAX_VARIABLES for frame in frames
                ):
                    raise MachineError("machine variable bound exceeded")

        apply_actions(actions)
        if transition is not None and transition.call is not None:
            if len(frames) >= MAX_CALL_DEPTH:
                raise MachineError("submachine recursion limit exceeded")
            sub = self.definition.submachines[transition.call]
            args = {key: expand(value, context(), send=False)
                    for key, value in transition.inputs.items()}
            child_vars = {**sub.variables, **args}
            frames.append({"name": sub.name, "return_to": transition.return_to,
                           "locals": child_vars, "exports": dict(transition.exports)})
            destination = sub.initial
        elif frames and "__" not in destination:
            # Explicit transition out of a submachine abandons its call stack.
            frames.clear()

        # A final submachine state returns to its named parent continuation.
        # Automatic joins are bounded by the call stack; no recursive send.
        while True:
            target = self.definition.states[destination]
            apply_actions(target.on_enter)
            if not target.final or not frames:
                break
            finished = frames.pop()
            values = finished["locals"]
            for source, target_name in finished["exports"].items():
                if source not in values:
                    raise MachineError(f"missing submachine export: {source}")
                if frames and (target_name in frames[-1]["locals"] or target_name not in root):
                    frames[-1]["locals"][target_name] = values[source]
                else:
                    root[target_name] = values[source]
            destination = finished["return_to"]
            if destination not in self.definition.states:
                raise MachineError("invalid submachine return target")
        if old_state != destination:
            counters.pop(old_state, None)
            counters.pop(destination, None)
        if retry_attempt:
            counters[destination] = retry_attempt
        if len(commands) > MAX_ACTIONS:
            raise MachineError("too many actions after submachine return")
        if len(root) > MAX_VARIABLES or any(len(f["locals"]) > MAX_VARIABLES for f in frames):
            raise MachineError("machine variable bound exceeded")
        result = Decision(old_state, destination, event, dict(captures),
                          {**root, **(frames[-1]["locals"] if frames else {})},
                          tuple(commands), tuple(notifications), cursor,
                          transition_index, transition_source, len(frames),
                          retry_attempt)
        self.state = destination
        self.variables = root
        self.frames = frames
        self.retry_counts = counters
        self.status = "completed" if target.final and not frames else "running"
        self.trace.append(result)
        if len(self.trace) > MAX_TRACE:
            del self.trace[:-MAX_TRACE]
        return result

    def start(self) -> Decision:
        if self.status != "created":
            raise MachineError("machine instance already started")
        return self._commit(self.definition.initial, (), {}, event="start", cursor=None)

    def receive_line(self, line: str, *, cursor: int | None = None) -> Decision | None:
        if self.status != "running":
            return None
        clean = strip_ansi(line)[:MAX_LINE]
        hierarchy = []
        node = self.definition.states[self.state]
        while True:
            hierarchy.append((node.name, node.transitions))
            if node.parent is None:
                break
            node = self.definition.states[node.parent]
        hierarchy.append(("*", self.definition.global_transitions))
        for source, transitions in hierarchy:
            for index, transition in enumerate(transitions):
                match = (transition.regex.search(clean) if transition.search
                         else transition.regex.match(clean))
                if match is None:
                    continue
                captures = {
                    **{str(i): value for i, value in enumerate(match.groups(), 1)
                       if value is not None},
                    **{key: value for key, value in match.groupdict().items()
                       if value is not None},
                    "0": match.group(0),
                }
                if not evaluate_condition(transition.when, self._context(captures)):
                    continue
                return self._commit(
                    transition.goto, transition.actions, captures, event="recv",
                    cursor=cursor, transition_index=index, transition=transition,
                    transition_source=source)
        return None

    def timeout(self, *, cursor: int | None = None) -> Decision | None:
        if self.status != "running":
            return None
        state = self.timeout_state()
        if state is None or state.on_timeout is None:
            return None
        attempts = self.retry_counts.get(self.state, 0)
        if state.name == self.state and attempts < state.retry_limit:
            return self._commit(self.state, (), {}, event="retry",
                                cursor=cursor, transition_source=state.name,
                                retry_attempt=attempts + 1)
        return self._commit(state.on_timeout.goto, state.on_timeout.actions, {},
                            event="timeout", cursor=cursor,
                            transition_source=state.name)

    def signal(self, event: str, *, cursor: int | None = None) -> Decision | None:
        """Explicit system event; send-free by compile-time contract."""
        if self.status != "running":
            return None
        transition = self.definition.events.get(event)
        if transition is None:
            return None
        return self._commit(transition.goto, transition.actions, {}, event=event,
                            cursor=cursor, transition_source="event:" + event)

    def feed(self, chunk: str, *, cursor: int | None = None) -> list[Decision]:
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
        "diagnose", "set", "signal",
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
