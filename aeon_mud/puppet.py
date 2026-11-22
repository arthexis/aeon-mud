"""Owner-only Unix socket bridge from shell processes into a live MUD session."""
from __future__ import annotations

import argparse
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
        self.stop_event = threading.Event()
        self.listener: socket.socket | None = None
        self.worker: threading.Thread | None = None
        self.bound_inode: int | None = None
        self.journal: PuppetJournal | None = None

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
                        continue
                    if operation != "send":
                        raise ValueError("unknown operation")
                    commands = body.get("commands")
                    if not isinstance(commands, list) or not 1 <= len(commands) <= MAX_COMMANDS:
                        raise ValueError("expected 1-32 commands")
                    if any(not isinstance(cmd, str) or not cmd or "\n" in cmd or "\r" in cmd or len(cmd) > 4096 for cmd in commands):
                        raise ValueError("commands must be nonempty single lines (max 4096 characters)")
                    request = PuppetRequest(commands)
                    self.pending.put_nowait(request)
                    if not request.done.wait(15):
                        if request.cancel_if_pending():
                            raise ValueError("request expired before dispatch")
                        raise ValueError("request was dispatched; delivery outcome unknown")
                    result = {"ok": request.error is None, "error": request.error,
                              "action_id": request.action["action_id"] if request.action else None,
                              "cursor_start": request.action["seq"] if request.action else None,
                              "session_id": request.action["session_id"] if request.action else None}
                except (ValueError, TypeError, KeyError, json.JSONDecodeError, queue.Full, socket.timeout) as exc:
                    result = {"ok": False, "error": str(exc) or "queue full"}
                try:
                    conn.sendall((json.dumps(result) + "\n").encode())
                except OSError:
                    pass

    def drain(self, controller: object) -> None:
        while True:
            try:
                request = self.pending.get_nowait()
            except queue.Empty:
                return
            try:
                if self.stop_event.is_set():
                    raise RuntimeError("puppet disabled")
                if not request.begin():
                    raise RuntimeError("puppet request expired")
                if controller.current_connection() is None:
                    raise RuntimeError("MUD session disconnected")
                if self.journal is not None:
                    transcript = getattr(controller, "transcript", None)
                    path = getattr(transcript, "path", None) if getattr(transcript, "mode", "off") != "off" else None
                    request.action = self.journal.start_action(request.commands, durable=path is not None, log_path=path)
                for idx, command in enumerate(request.commands):
                    if idx:
                        # Mirror normal multiline paste pacing.
                        time.sleep(controller.profile.paste_line_delay)
                    if self.stop_event.is_set():
                        raise RuntimeError("puppet disabled")
                    if controller.current_connection() is None:
                        raise RuntimeError("MUD session disconnected")
                    controller.send_line(command, secret=False, remember=False)
            except (OSError, RuntimeError, ValueError) as exc:
                request.error = str(exc)
            finally:
                request.done.set()

    def close(self) -> None:
        self.stop_event.set()
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

    def attach(self, transcript) -> None:
        if self.journal is not None:
            raise RuntimeError("Puppet already attached")
        journal = PuppetJournal(self.path.stem.removeprefix("puppet-"))
        self.journal = journal
        transcript.observers.append(journal.record)

    def detach(self, transcript) -> None:
        if self.journal is not None:
            try:
                transcript.observers.remove(self.journal.record)
            except ValueError:
                pass
            self.journal = None

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


class PuppetClient:
    """Reusable structured client for a character's local Puppet socket."""

    def __init__(self, character: str):
        self.path = puppet_socket_path(character)

    def request(self, payload: dict):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(18)
            sock.connect(str(self.path))
            sock.sendall((json.dumps(payload) + "\n").encode())
            response = bytearray()
            while b"\n" not in response and len(response) <= MAX_REQUEST:
                part = sock.recv(4096)
                if not part:
                    raise RuntimeError("session closed without acknowledgement")
                response.extend(part)
        result = json.loads(bytes(response).split(b"\n", 1)[0])
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "Puppet request rejected")
        return result

    def send(self, command: str, prefix: str = ""):
        return self.request({"op": "send", "commands": [prefix + command]})

    def read(self, action_id: str | None = None, *, last: bool = False,
             after: int | None = None, wait: float = 0, idle: float = 0,
             until_next: bool = False):
        return self.request({"op": "read_last" if last else "read",
            "action_id": action_id, "after": after, "wait": wait,
            "idle": idle, "until_next": until_next})

    def follow(self, action_id: str | None = None, *, last: bool = False):
        cursor = None
        while True:
            result = self.read(action_id, last=last and cursor is None,
                               after=cursor, wait=2)
            for event in result["events"]:
                yield event
            cursor = result["cursor_end"]
            action_id = None
            last = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aeon-puppet", description="Send commands and observe MUD output")
    parser.add_argument("--prefix", default="")
    parser.add_argument("--capture", action="store_true", help="Print action ID and starting cursor")
    parser.add_argument("--read", metavar="ACTION_ID", help="Read output since a Puppet action")
    parser.add_argument("--read-last", action="store_true", help="Read output since the last Puppet action")
    parser.add_argument("--follow", action="store_true", help="Stream new received output")
    parser.add_argument("--wait", type=float, default=0, metavar="SECONDS")
    parser.add_argument("--until-idle", type=float, default=0, metavar="SECONDS")
    parser.add_argument("--until-next-action", action="store_true")
    parser.add_argument("--after-cursor", type=int)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("character")
    parser.add_argument("command", nargs="*")
    args = parser.parse_args(argv)
    try:
        client = PuppetClient(args.character)
        observation = args.read is not None or args.read_last or args.after_cursor is not None
        if observation:
            if args.command or args.prefix or (args.read and args.read_last):
                parser.error("read operations cannot include commands, prefix or conflicting read modes")
            result = client.read(args.read, last=args.read_last, after=args.after_cursor,
                                 wait=args.wait, idle=args.until_idle,
                                 until_next=args.until_next_action)
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                for event in result["events"]:
                    print(event["text"], end="")
            if args.follow:
                cursor = result["cursor_end"]
                while True:
                    result = client.read(after=cursor, wait=2)
                    for event in result["events"]:
                        print(event["text"], end="", flush=True)
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
        result = client.request({"op": "send", "commands": commands})
        if args.capture or args.json:
            print(json.dumps(result, ensure_ascii=False) if args.json else result.get("action_id") or "")
        if args.wait or args.until_idle:
            if not result.get("action_id"):
                raise RuntimeError("action has no capture ID")
            data = client.read(result["action_id"], wait=args.wait, idle=args.until_idle,
                               until_next=args.until_next_action)
            if args.json:
                print(json.dumps(data, ensure_ascii=False))
            else:
                for event in data["events"]:
                    print(event["text"], end="")
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"aeon-puppet: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
