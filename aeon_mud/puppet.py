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
from dataclasses import dataclass, field

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


class PuppetServer:
    """Accept socket requests in a worker; send on the foreground UI thread."""

    def __init__(self, character: str) -> None:
        self.path = puppet_socket_path(character)
        self.pending: queue.Queue[PuppetRequest] = queue.Queue(maxsize=32)
        self.stop_event = threading.Event()
        self.listener: socket.socket | None = None
        self.worker: threading.Thread | None = None

    def start(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Never remove another process's socket, even if it seems stale.
            listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            listener.listen(8)
            listener.settimeout(0.2)
        except BaseException:
            listener.close()
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
                    commands = body.get("commands") if isinstance(body, dict) else None
                    if not isinstance(commands, list) or not 1 <= len(commands) <= MAX_COMMANDS:
                        raise ValueError("expected 1-32 commands")
                    if any(not isinstance(cmd, str) or not cmd or "\n" in cmd or "\r" in cmd or len(cmd) > 4096 for cmd in commands):
                        raise ValueError("commands must be nonempty single lines (max 4096 characters)")
                    request = PuppetRequest(commands)
                    self.pending.put_nowait(request)
                    if not request.done.wait(15):
                        raise ValueError("session did not process request in time")
                    result = {"ok": request.error is None, "error": request.error}
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
                if controller.current_connection() is None:
                    raise RuntimeError("MUD session disconnected")
                for idx, command in enumerate(request.commands):
                    if idx:
                        # Mirror normal multiline paste pacing.
                        import time
                        time.sleep(controller.profile.paste_line_delay)
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
        try:
            if self.path.is_socket():
                self.path.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aeon-puppet", description="Send commands to an enabled local character session")
    parser.add_argument("--prefix", default="", help="Prepend this literal text to every command")
    parser.add_argument("character", help="Character selected by aeon-mud connect --login")
    parser.add_argument("command", nargs="*", help="Command words; if omitted, read lines from stdin")
    args = parser.parse_args(argv)
    try:
        path = puppet_socket_path(args.character)
        lines = [" ".join(args.command)] if args.command else [line.rstrip("\n") for line in sys.stdin]
        commands = [args.prefix + line for line in lines if line]
        if not commands:
            parser.error("no commands supplied")
        if len(commands) > MAX_COMMANDS:
            parser.error(f"maximum {MAX_COMMANDS} commands per request")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(18)
            sock.connect(str(path))
            sock.sendall((json.dumps({"commands": commands}) + "\n").encode())
            response = bytearray()
            while b"\n" not in response and len(response) <= MAX_REQUEST:
                chunk = sock.recv(4096)
                if not chunk:
                    raise RuntimeError("session closed without acknowledgement")
                response.extend(chunk)
        reply = json.loads(bytes(response).split(b"\n", 1)[0])
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error") or "command rejected")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"aeon-puppet: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
