"""Owner-only Unix socket bridge from shell processes into a live MUD session."""
from __future__ import annotations

import argparse
from collections import deque
import json
import os
from pathlib import Path
import queue
import re
import socket
import stat
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from .puppet_journal import PuppetJournal
from .reaction import reaction_delay
from .action_scheduler import ActionScheduler

MAX_REQUEST = 65536
MAX_COMMANDS = 32
CHARACTER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def puppet_socket_path(character: str) -> Path:
    if not CHARACTER_RE.fullmatch(character):
        raise ValueError("character must start with a letter and contain only letters, numbers, _ or -")
    if os.name != "posix" or not hasattr(socket, "AF_UNIX"):
        raise RuntimeError("puppet sockets require Unix")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        root = Path(runtime)
        if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid():
            raise RuntimeError("XDG_RUNTIME_DIR is not an owner-controlled directory")
        if root.stat().st_mode & 0o022:
            raise RuntimeError("XDG_RUNTIME_DIR must not be group/world writable")
    else:
        root = Path(tempfile.gettempdir()) / f"aeon-mud-{os.getuid()}"
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid():
            raise RuntimeError("unsafe puppet runtime directory")
        if stat.S_IMODE(root.stat().st_mode) != 0o700:
            raise RuntimeError("puppet runtime directory must have permissions 0700")
    folder = root / "aeon-mud"
    try:
        folder.mkdir(mode=0o700)
    except FileExistsError:
        pass
    if folder.is_symlink() or not folder.is_dir() or folder.stat().st_uid != os.getuid():
        raise RuntimeError("unsafe puppet socket directory")
    if stat.S_IMODE(folder.stat().st_mode) != 0o700:
        raise RuntimeError("puppet socket directory must have permissions 0700")
    path = folder / f"puppet-{character.casefold()}.sock"
    if len(os.fsencode(path)) >= 104:
        raise RuntimeError("puppet socket path too long")
    return path


@dataclass
class PuppetRequest:
    commands: list[str]
    done: threading.Event = field(default_factory=threading.Event)
    error: str | None = None
    action: dict | None = None
    immediate: bool = False
    due: float = 0.0
    reaction_seconds: float = 0.0
    scheduled_action: object | None = None
    started: bool = False
    cancelled: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def begin(self) -> bool:
        with self.lock:
            if self.cancelled:
                return False
            self.started = True
            return True

    def cancel_if_pending(self) -> bool:
        with self.lock:
            if self.started:
                return False
            self.cancelled = True
            return True


class PuppetServer:
    """Accept socket requests in a worker; send on the foreground UI thread."""

    def __init__(self, character: str) -> None:
        self.path = puppet_socket_path(character)
        self.pending: queue.Queue[PuppetRequest] = queue.Queue(maxsize=32)
        self.scheduled: deque[PuppetRequest] = deque()  # compatibility; work is owned by scheduler
        self.scheduler = ActionScheduler()
        self.stop_event = threading.Event()
        self.listener: socket.socket | None = None
        self.worker: threading.Thread | None = None
        self.bound_inode: int | None = None
        self.journal: PuppetJournal | None = None
        self.owns_journal = False
        self.machine_bridge = None
        self.connection_slots = threading.BoundedSemaphore(8)

    def start(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Never remove another process's socket, even if it seems stale.
            listener.bind(str(self.path))
            self.bound_inode = self.path.lstat().st_ino
            os.chmod(self.path, 0o600)
            listener.listen(8)
            listener.settimeout(0.2)
        except BaseException:
            listener.close()
            self._unlink_owned_socket()
            raise
        self.listener = listener
        self.worker = threading.Thread(target=self._serve, name="aeon-puppet", daemon=True)
        self.worker.start()

    def _serve(self) -> None:
        assert self.listener is not None
        while not self.stop_event.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self.connection_slots.acquire(blocking=False):
                with conn:
                    try:
                        conn.sendall(b'{"ok": false, "error": "too many active Puppet clients"}\n')
                    except OSError:
                        pass
                continue
            worker = threading.Thread(target=self._serve_connection,
                                      args=(conn,), name="aeon-puppet-client", daemon=True)
            try:
                worker.start()
            except BaseException:
                self.connection_slots.release()
                conn.close()
                raise

    def _serve_connection(self, conn: socket.socket) -> None:
        try:
            with conn:
                conn.settimeout(5.0)
                try:
                    payload = bytearray()
                    while b"\n" not in payload:
                        part = conn.recv(min(4096, MAX_REQUEST + 1 - len(payload)))
                        if not part or len(payload) + len(part) > MAX_REQUEST:
                            raise ValueError("request too large or incomplete")
                        payload.extend(part)
                    body = json.loads(bytes(payload).split(b"\n", 1)[0])
                    if not isinstance(body, dict):
                        raise ValueError("expected JSON object")
                    operation = body.get("op", "send")
                    if operation in {"read", "read_last", "subscribe"}:
                        if self.journal is None:
                            raise ValueError("Puppet observation not enabled")
                        result = {"ok": True, **self.journal.read(
                            action_id=body.get("action_id"), last=operation == "read_last",
                            after=body.get("after"), until_next=bool(body.get("until_next", False)),
                            wait=float(body.get("wait", 0)), idle=float(body.get("idle", 0)))}
                        conn.sendall((json.dumps(result) + "\n").encode())
                        return
                    if operation == "machine":
                        bridge = self.machine_bridge
                        if bridge is None:
                            raise ValueError("machine bridge unavailable")
                        command = body.get("command")
                        arguments = body.get("args", [])
                        if not isinstance(command, str) or not isinstance(arguments, list):
                            raise ValueError("machine requires command and args array")
                        result = {"ok": True, "machine": bridge.execute(command, arguments)}
                        encoded = (json.dumps(result, ensure_ascii=False) + "\n").encode()
                        if len(encoded) > MAX_REQUEST:
                            raise ValueError("machine response too large; narrow the trace")
                        conn.sendall(encoded)
                        return
                    if operation != "send":
                        raise ValueError("unknown operation")
                    commands = body.get("commands")
                    if not isinstance(commands, list) or not 1 <= len(commands) <= MAX_COMMANDS:
                        raise ValueError("expected 1-32 commands")
                    if any(not isinstance(cmd, str) or not cmd or "\n" in cmd or "\r" in cmd or len(cmd) > 4096 for cmd in commands):
                        raise ValueError("commands must be nonempty single lines (max 4096 characters)")
                    immediate = body.get("immediate", False)
                    if not isinstance(immediate, bool):
                        raise ValueError("immediate must be boolean")
                    request = PuppetRequest(commands, immediate=immediate)
                    request.reaction_seconds = reaction_delay(immediate=immediate)
                    request.due = time.monotonic() + request.reaction_seconds
                    self.pending.put_nowait(request)
                    if not request.done.wait(15):
                        if request.cancel_if_pending():
                            raise ValueError("request expired before dispatch")
                        raise ValueError("request was dispatched; delivery outcome unknown")
                    result = {"ok": request.error is None, "error": request.error,
                              "action_id": request.action["action_id"] if request.action else None,
                              "cursor_start": request.action["seq"] if request.action else None,
                              "session_id": request.action["session_id"] if request.action else None,
                              "reaction_delay_ms": round(request.reaction_seconds * 1000)}
                except (ValueError, TypeError, KeyError, json.JSONDecodeError, queue.Full, socket.timeout) as exc:
                    result = {"ok": False, "error": str(exc) or "queue full"}
                try:
                    conn.sendall((json.dumps(result) + "\n").encode())
                except OSError:
                    pass


        finally:
            self.connection_slots.release()

    def prepare(self) -> None:
        """Transfer received socket requests into the shared bounded queue."""
        while True:
            try:
                request = self.pending.get_nowait()
            except queue.Empty:
                break
            if self.stop_event.is_set() or request.cancelled:
                request.error = "puppet disabled" if self.stop_event.is_set() else "puppet request expired"
                request.done.set()
                continue

            def changed(job, target=request):
                target.action = job.action
                if job.done:
                    target.error = (
                        "puppet request expired" if job.error == "action expired before dispatch"
                        else job.error if job.status != "sent" else None)
                    target.done.set()

            try:
                request.scheduled_action = self.scheduler.enqueue(
                    "puppet", request.commands, due=request.due, journal=self.journal,
                    on_change=changed,
                    before=lambda r=request: not self.stop_event.is_set() and r.begin())
                request.scheduled_action.legacy_request = request
            except (ValueError, RuntimeError) as exc:
                request.error = str(exc)
                request.done.set()

    def drain(self, controller: object) -> None:
        """Legacy adapter; live sessions call the shared scheduler once per tick."""
        self.prepare()
        for job in tuple(self.scheduler.jobs.values()):
            req = getattr(job, "legacy_request", None)
            if job.origin == "puppet" and req is not None and not req.started:
                job.due = req.due
        self.scheduler.drain(controller, origin="puppet", max_steps=MAX_COMMANDS)

    def on_disconnect(self) -> None:
        self.scheduler.cancel(origin="puppet", reason="session disconnected")
        while True:
            try:
                request = self.pending.get_nowait()
            except queue.Empty:
                break
            request.error = "session disconnected"
            request.done.set()

    def close(self) -> None:
        self.stop_event.set()
        self.scheduler.cancel(origin="puppet", reason="puppet disabled")
        if self.listener is not None:
            self.listener.close()
        while True:
            try:
                request = self.pending.get_nowait()
            except queue.Empty:
                break
            request.error = "puppet disabled"
            request.done.set()
        if self.worker is not None and self.worker is not threading.current_thread():
            self.worker.join(timeout=1.0)
        self._unlink_owned_socket()

    def attach(self, transcript, journal=None) -> None:
        if self.journal is not None:
            raise RuntimeError("Puppet already attached")
        self.owns_journal = journal is None
        self.journal = journal if journal is not None else PuppetJournal(
            self.path.stem.removeprefix("puppet-"))
        if self.owns_journal:
            transcript.observers.append(self.journal.record)

    def detach(self, transcript) -> None:
        if self.journal is not None:
            if self.owns_journal:
                try:
                    transcript.observers.remove(self.journal.record)
                except ValueError:
                    pass
            self.journal = None
            self.owns_journal = False

    def _unlink_owned_socket(self) -> None:
        # Never unlink another process's replacement or a symlink.
        if self.bound_inode is None:
            return
        try:
            info = self.path.lstat()
            if stat.S_ISSOCK(info.st_mode) and info.st_ino == self.bound_inode:
                self.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self.bound_inode = None


def parse_duration(value: str) -> float:
    """Accept seconds, e.g. 2, 2s, 500ms, or 1m."""
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m)?", value.strip())
    if not match:
        raise argparse.ArgumentTypeError("duration must be a nonnegative number with ms, s, or m suffix")
    amount = float(match.group(1))
    suffix = match.group(2)
    seconds = amount / 1000 if suffix == "ms" else amount * 60 if suffix == "m" else amount
    if seconds > 10:
        raise argparse.ArgumentTypeError("maximum observation wait is 10 seconds per request")
    return seconds


class PuppetClient:
    """Reusable structured client for a character's local Puppet socket."""

    def __init__(self, character: str):
        self.path = puppet_socket_path(character)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def request(self, payload: dict):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(18)
            sock.connect(str(self.path))
            sock.sendall((json.dumps(payload) + "\n").encode())
            response = bytearray()
            while b"\n" not in response:
                if len(response) > MAX_REQUEST:
                    raise RuntimeError("Puppet response exceeded maximum size")
                part = sock.recv(4096)
                if not part:
                    raise RuntimeError("session closed without acknowledgement")
                response.extend(part)
        result = json.loads(bytes(response).split(b"\n", 1)[0])
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(result.get("error") if isinstance(result, dict) else "invalid Puppet response")
        return result

    def machine(self, command: str, *args: str) -> dict:
        return self.request({"op": "machine", "command": command, "args": list(args)})["machine"]

    def send(self, command: str, prefix: str = "", *, immediate: bool = False):
        return self.request({"op": "send", "commands": [prefix + command],
                             "immediate": immediate})

    def read(self, action_id: str | None = None, *, last: bool = False,
             after: int | None = None, wait: float = 0, idle: float = 0,
             until_next: bool = False):
        return self.request({"op": "read_last" if last else "read",
            "action_id": action_id, "after": after, "wait": wait,
            "idle": idle, "until_next": until_next})

    def read_all(self, action_id: str | None = None, *, last: bool = False,
                 after: int | None = None, wait: float = 0, idle: float = 0,
                 until_next: bool = False):
        """Collect all available pages without losing the original action boundary."""
        current = after
        events = []
        pages = 0
        while True:
            result = self.read(action_id, last=last, after=current,
                               wait=wait if pages == 0 else 0,
                               idle=idle if pages == 0 else 0,
                               until_next=until_next)
            events.extend(result["events"])
            action_id = result.get("action_id") or action_id
            last = False
            if not result.get("more"):
                return {**result, "events": events}
            if result["cursor_end"] == current:
                raise RuntimeError("Puppet observation cursor did not advance")
            current = result["cursor_end"]
            pages += 1
            if pages > 256:
                raise RuntimeError("Puppet observation pagination limit exceeded")

    def follow(self, action_id: str | None = None, *, last: bool = False):
        result = self.read_all(action_id, last=last)
        for event in result["events"]:
            yield event
        cursor = result["cursor_end"]
        while True:
            result = self.read_all(after=cursor, wait=2)
            for event in result["events"]:
                yield event
            cursor = result["cursor_end"]


def _print_observation(result: dict, *, structured: bool):
    if structured:
        print(json.dumps(result, ensure_ascii=False))
    else:
        for event in result["events"]:
            print(event["text"], end="", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aeon-puppet", description="Send commands and observe MUD output")
    parser.add_argument("--prefix", default="")
    parser.add_argument("--immediate", action="store_true", help="Skip the randomized first-command reaction delay")
    parser.add_argument("--capture", action="store_true", help="Print action ID and starting cursor")
    parser.add_argument("--read", metavar="ACTION_ID", help="Read output since a Puppet action")
    parser.add_argument("--read-last", action="store_true", help="Read output since the last Puppet action")
    parser.add_argument("--follow", action="store_true", help="Stream new received output")
    parser.add_argument("--wait", type=parse_duration, default=0, metavar="DURATION")
    parser.add_argument("--until-idle", type=parse_duration, default=0, metavar="DURATION")
    parser.add_argument("--until-next-action", action="store_true")
    parser.add_argument("--after-cursor", type=int)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("character")
    parser.add_argument("command", nargs="*")
    args = parser.parse_intermixed_args(argv)
    try:
        client = PuppetClient(args.character)
        observation = args.read is not None or args.read_last or args.after_cursor is not None
        wait = args.wait or (10.0 if args.until_idle else 0.0)
        if observation:
            if args.command or args.prefix or args.immediate or (args.read and args.read_last):
                parser.error("read operations cannot include commands, prefix or conflicting read modes")
            result = client.read_all(args.read, last=args.read_last, after=args.after_cursor,
                                     wait=wait, idle=args.until_idle,
                                     until_next=args.until_next_action)
            _print_observation(result, structured=args.json)
            if args.follow:
                cursor = result["cursor_end"]
                while True:
                    result = client.read_all(after=cursor, wait=2)
                    _print_observation(result, structured=args.json)
                    cursor = result["cursor_end"]
            return 0
        if args.follow:
            parser.error("--follow requires --read, --read-last or --after-cursor")
        lines = [" ".join(args.command)] if args.command else [line.rstrip("\n") for line in sys.stdin]
        commands = [args.prefix + line for line in lines if line]
        if not commands:
            parser.error("no commands supplied")
        if len(commands) > MAX_COMMANDS:
            parser.error(f"maximum {MAX_COMMANDS} commands per request")
        result = client.request({"op": "send", "commands": commands, "immediate": args.immediate})
        if wait:
            if not result.get("action_id"):
                raise RuntimeError("action has no capture ID")
            observed = client.read_all(result["action_id"], wait=wait, idle=args.until_idle,
                                       until_next=args.until_next_action)
            if args.json:
                print(json.dumps({"action": result, "observation": observed}, ensure_ascii=False))
            else:
                if args.capture:
                    print(result["action_id"])
                _print_observation(observed, structured=False)
        elif args.json:
            print(json.dumps(result, ensure_ascii=False))
        elif args.capture:
            print(result.get("action_id") or "")
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"aeon-puppet: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
