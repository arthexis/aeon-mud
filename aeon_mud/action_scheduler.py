"""One foreground-only, bounded dispatcher shared by Puppet, Trigger and Machine.

Producers decide *when* and *what*; the scheduler is the single authority for
ordering, journal reservation, command pacing and delivery-outcome receipts.
No background worker ever calls controller.send_line().
"""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
import uuid
from typing import Callable, Any

MAX_BATCHES = 128
MAX_COMMANDS = 32
MAX_COMMAND_SIZE = 4096


@dataclass
class ScheduledAction:
    origin: str
    commands: tuple[str, ...]
    due: float
    journal: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    on_change: Callable[..., None] | None = None
    can_send: Callable[[], bool] | None = None
    before: Callable[[], bool] | None = None
    key: str = field(default_factory=lambda: uuid.uuid4().hex)
    index: int = 0
    action: dict | None = None
    status: str = "reserved"
    error: str | None = None
    order: int = 0
    inflight: bool = False
    cancel_reason: str | None = None

    @property
    def done(self) -> bool:
        return self.status in {"sent", "failed", "cancelled", "outcome_unknown"}


class ActionScheduler:
    """Shared per-session queue. A drain performs one outbound command per step."""

    def __init__(self, *, clock=None, limit: int = MAX_BATCHES):
        self.clock = clock or time.monotonic
        self.limit = limit
        self.lock = threading.RLock()
        self.jobs: dict[str, ScheduledAction] = {}
        self.order = 0
        self.closed = False

    def enqueue(self, origin: str, commands: list[str] | tuple[str, ...], *,
                due: float | None = None, journal=None, metadata=None,
                on_change=None, can_send=None, before=None) -> ScheduledAction:
        if origin not in {"puppet", "trigger", "machine"}:
            raise ValueError("unsupported action origin")
        if not isinstance(commands, (list, tuple)) or not 1 <= len(commands) <= MAX_COMMANDS:
            raise ValueError("action batch must contain 1-32 commands")
        if any(not isinstance(c, str) or not c or len(c) > MAX_COMMAND_SIZE or
               "\n" in c or "\r" in c or (origin != "puppet" and c.lstrip().startswith("//")) for c in commands):
            raise ValueError("actions must be single MUD lines, never local // commands")
        with self.lock:
            if self.closed:
                raise RuntimeError("action scheduler closed")
            if len(self.jobs) >= self.limit:
                raise ValueError("action scheduler queue full")
            self.order += 1
            job = ScheduledAction(origin=origin, commands=tuple(commands),
                                  due=self.clock() if due is None else due,
                                  journal=journal, metadata=dict(metadata or {}),
                                  on_change=on_change, can_send=can_send,
                                  before=before, order=self.order)
            self.jobs[job.key] = job
            return job

    @staticmethod
    def _inform(job: ScheduledAction):
        if job.on_change is not None:
            job.on_change(job)

    def cancel(self, *, origin: str | None = None, predicate=None,
               reason: str = "action cancelled"):
        """Cancel pending work; dispatch-in-progress always has unknown outcome."""
        notifications = []
        with self.lock:
            for job in tuple(self.jobs.values()):
                if origin is not None and job.origin != origin:
                    continue
                if predicate is not None and not predicate(job):
                    continue
                if job.inflight:
                    job.cancel_reason = reason
                    continue
                job.status = "outcome_unknown" if job.index else "cancelled"
                job.error = reason
                self.jobs.pop(job.key, None)
                notifications.append(job)
        for job in notifications:
            self._inform(job)
        return len(notifications)

    def close(self):
        self.cancel(reason="scheduler closed")
        with self.lock:
            self.closed = True

    def _reserve(self, controller, job: ScheduledAction):
        if job.journal is None:
            return
        transcript = getattr(controller, "transcript", None)
        if transcript is None:
            job.action = job.journal.start_action(
                list(job.commands), origin=job.origin, **job.metadata)
            return
        with transcript.lock:
            path = transcript.path if transcript.mode in {"text", "plain"} else None
            job.action = job.journal.start_action(
                list(job.commands), origin=job.origin, durable=path is not None,
                log_path=path, **job.metadata)

    def _pick(self, origin=None, excluded=None):
        with self.lock:
            available = [j for j in self.jobs.values()
                         if not j.inflight and j.key not in (excluded or set())
                         and (origin is None or j.origin == origin)
                         and j.due <= self.clock()]
            if not available:
                return None
            chosen = min(available, key=lambda j: (j.due, j.order))
            chosen.inflight = True
            return chosen

    def drain(self, controller, *, origin=None, max_steps=1):
        """Call only on foreground session thread; never blocks for reaction/pacing."""
        processed = 0
        excluded = set()
        for _ in range(min(max_steps, MAX_COMMANDS)):
            job = self._pick(origin=origin, excluded=excluded)
            if job is None:
                break
            if job.can_send is not None and not job.can_send():
                with self.lock:
                    job.inflight = False
                excluded.add(job.key)
                continue
            status = None
            error = None
            attempted = False
            try:
                # Check cancellation again after the producer's gate, before side effects.
                if job.cancel_reason:
                    raise RuntimeError(job.cancel_reason)
                if job.index == 0:
                    if job.before is not None and not job.before():
                        raise ValueError("action expired before dispatch")
                    if controller.current_connection() is None:
                        raise ValueError("session disconnected before dispatch")
                    if not getattr(controller, "trigger_ready", True):
                        # An active login handshake must not receive gameplay commands.
                        with self.lock:
                            job.inflight = False
                        excluded.add(job.key)
                        continue
                    self._reserve(controller, job)
                if job.cancel_reason:
                    raise RuntimeError(job.cancel_reason)
                if controller.current_connection() is None:
                    raise RuntimeError("session disconnected")
                attempted = True
                controller.send_line(job.commands[job.index], secret=False, remember=False)
                job.index += 1
                status = "sent" if job.index == len(job.commands) else "sending"
            except (OSError, ValueError, RuntimeError) as exc:
                error = str(exc)
                # A started request, including a journaled command, has an
                # uncertain outcome if interrupted during its send path.
                status = ("outcome_unknown" if attempted or job.action is not None or
                          job.index > 0 or job.cancel_reason else "failed")
            with self.lock:
                if job.cancel_reason:
                    status = "outcome_unknown"
                    error = job.cancel_reason
                job.status = status
                job.error = error
                job.inflight = False
                if job.done:
                    self.jobs.pop(job.key, None)
                else:
                    pace = max(0.0, float(controller.profile.paste_line_delay))
                    job.due = self.clock() + pace
                    self.order += 1
                    job.order = self.order  # fair ordering among due batches
            self._inform(job)
            processed += 1
        return processed
