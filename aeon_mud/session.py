"""Connection setup, session lifecycle, receiver and reconnect orchestration."""
from __future__ import annotations
import argparse
import getpass
import hashlib
import os
import re
import select
import shutil
import sys
import threading
import time
from typing import Callable
from .config import *
from .profiles import *
from .security import *
from .telnet import *
from .transport import *
from .terminal_text import TerminalStreamDecoder, sanitize_terminal_text, strip_ansi
from .input import KeyReader, fast_page_scroll_amount
from .terminal import MudUi
from .commands import *
from .transcript import Transcript

def prompt_yes_no(
    message: str,
    default: bool = False,
    assume_yes: bool = False,
    input_func: Callable[[str], str] = input,
) -> bool:
    if assume_yes:
        return True
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input_func(message + suffix).strip().lower()
    except EOFError:
        print()
        return default
    if not answer:
        return default
    return answer in {"y", "yes"}


def pre_read_banner(transport: Transport, codec: TelnetCodec, timeout: float) -> str:
    if not isinstance(transport, SocketTransport) or timeout <= 0:
        return ""
    sock = transport.sock
    sock.setblocking(False)
    deadline = time.monotonic() + timeout
    raw = bytearray()
    try:
        while time.monotonic() < deadline and len(raw) < MAX_INITIAL_BANNER_BYTES:
            remaining = max(0.0, deadline - time.monotonic())
            readable, _, _ = select.select([sock], [], [], min(0.1, remaining))
            if not readable:
                continue
            chunk = sock.recv(4096)
            if not chunk:
                break
            output, response = codec.feed(chunk)
            if response:
                sock.sendall(response)
            raw.extend(output)
    finally:
        sock.setblocking(True)
    text = raw.decode("utf-8", "replace")
    return sanitize_terminal_text(text)


def open_session_transport(
    profile: Profile, *, require_private_peer: bool = False,
) -> tuple[Transport, TelnetCodec, str]:
    transport = make_transport(profile, require_private_peer=require_private_peer)
    codec = TelnetCodec(profile.terminal_type)
    try:
        width, height = shutil.get_terminal_size((100, 30))
        # Do not send unsolicited NAWS (IAC SB ...) before the MUD has
        # negotiated DO NAWS. Only respond after the server requests it.
        codec.width = max(1, min(width, 65535))
        codec.height = max(1, min(height, 65535))
        initial_banner = pre_read_banner(transport, codec, profile.sanity_timeout)
    except Exception:
        transport.close()
        raise
    return transport, codec, initial_banner


def run_sanity_checks(
    profile: Profile, banner: str, accept_host_change: bool, assume_yes: bool,
    *, interactive: bool = True,
) -> None:
    if profile.expected_banner and not banner and profile.sanity_timeout > 0 and not interactive:
        raise ValueError("Expected banner was absent on reconnect")
    if profile.expected_banner and banner:
        try:
            matched = re.search(profile.expected_banner, strip_ansi(banner), re.MULTILINE) is not None
        except re.error as exc:
            raise SystemExit(f"Invalid expected_banner regex for {profile.label()}: {exc}") from exc
        if not matched:
            if not interactive:
                raise ValueError("Expected banner did not match on reconnect")
            sample = strip_ansi(banner).replace("\n", " ")[:300]
            ok = prompt_yes_no(
                f"Expected banner did not match for {profile.target_key()}. Saw: {sample!r}. Continue?",
                default=False,
                assume_yes=assume_yes,
            )
            if not ok:
                raise SystemExit("Connection aborted by banner policy")

    if not banner:
        return
    # Use the legacy path resolution hook for existing callers/tests.
    known = KnownHosts(known_hosts_path())
    key = profile.label()
    current_hash = hashlib.sha256(banner.encode("utf-8", "replace")).hexdigest()
    existing = known.get(key)
    if existing and existing.get("banner_sha256") != current_hash and not accept_host_change:
        if not interactive:
            raise ValueError("Known host banner changed on reconnect")
        ok = prompt_yes_no(
            f"Initial banner changed for profile {profile.label()}. Continue and trust this banner?",
            default=False,
            assume_yes=assume_yes,
        )
        if not ok:
            raise SystemExit("Connection aborted because banner changed")
    known.set(key, banner)


class SessionController:
    def __init__(
        self,
        profile: Profile,
        transport: Transport,
        codec: TelnetCodec,
        transcript: Transcript,
        ui: MudUi,
        password: str | None,
        private_peer_required: bool = False,
    ) -> None:
        self.profile = profile
        self.private_peer_required = private_peer_required
        self.transport = transport
        self.codec = codec
        self.transcript = transcript
        self.ui = ui
        self.password = password
        self.character_login: tuple[str, str] | None = None
        self.tail = ""
        self.sent_username = False
        self.sent_password = False
        self.lock = threading.RLock()
        self.reconnect_lock = threading.Lock()
        self.last_io = time.monotonic()
        self.last_keepalive = 0.0
        self.ready_for_commands = threading.Event()

    def current_connection(self) -> tuple[Transport, TelnetCodec] | None:
        with self.lock:
            if self.transport is None:
                return None
            return self.transport, self.codec

    def install_connection(self, transport: Transport, codec: TelnetCodec) -> None:
        old_transport: Transport | None
        with self.lock:
            old_transport = self.transport
            self.transport = transport
            self.codec = codec
            self.tail = ""
            self.sent_username = False
            self.sent_password = False
            self.last_io = time.monotonic()
            self.last_keepalive = 0.0
            self.ready_for_commands = threading.Event()
        if old_transport is not None and old_transport is not transport:
            old_transport.close()
        self.ui.set_connected(True)

    def mark_disconnected(self, transport: Transport | None, message: str) -> bool:
        with self.lock:
            if transport is not None and self.transport is not transport:
                return False
            old_transport = self.transport
            self.transport = None
            self.codec = None
        if old_transport is not None:
            old_transport.close()
        self.ui.set_connected(False)
        if message:
            self.ui.set_status(message)
        return True

    def close(self) -> None:
        self.mark_disconnected(None, "")

    def send_raw(self, data: bytes, expected_transport: Transport | None = None) -> None:
        failed_transport: Transport | None = None
        failed_message = ""
        with self.lock:
            if self.transport is None:
                raise ConnectionUnavailable("not connected; reconnecting")
            if expected_transport is not None and self.transport is not expected_transport:
                raise ConnectionChanged("connection changed")
            try:
                self.transport.sendall(data)
            except OSError as exc:
                failed_transport = self.transport
                failed_message = str(exc)
                self.transport = None
                self.codec = None
            else:
                self.last_io = time.monotonic()
                return
        if failed_transport is not None:
            failed_transport.close()
            self.ui.set_connected(False)
            self.ui.set_status(f"Send failed: {failed_message}")
        raise OSError(failed_message)

    def maybe_send_keepalive(self) -> None:
        interval = self.profile.keepalive_interval
        if interval <= 0:
            return
        now = time.monotonic()
        with self.lock:
            if self.transport is None:
                return
            if now - self.last_io < interval or now - self.last_keepalive < interval:
                return
            self.last_keepalive = now
        try:
            self.send_raw(bytes([IAC, NOP]))
        except OSError:
            pass

    def mark_server_output(self) -> None:
        self.ready_for_commands.set()

    def send_line(
        self, line: str, secret: bool = False, remember: bool = True,
        expected_transport: Transport | None = None,
    ) -> None:
        # A number of MUDs drop application commands received while their
        # initial Telnet/banner handshake is underway. Wait for first visible
        # server output; bounded fallback keeps silent servers usable.
        self.ready_for_commands.wait(timeout=2.0)
        outbound_lines = [line] if secret else split_outbound_buffers(line)
        for index, outbound_line in enumerate(outbound_lines):
            data = (outbound_line + self.profile.newline).encode(self.profile.encoding, "replace")
            self.send_raw(data, expected_transport=expected_transport)
            self.transcript.write("send", outbound_line, secret=secret)
            if index < len(outbound_lines) - 1:
                time.sleep(self.profile.paste_line_delay)
        if remember and not secret:
            self.ui.remember(line)
        if len(outbound_lines) > 1 and not secret:
            self.ui.set_status(f"Sent {len(outbound_lines)} buffers; continuations used spoof")

    def send_character_login(self, expected_transport: Transport | None = None) -> None:
        if self.character_login is None:
            return
        character, password = self.character_login
        # This contains a password: never add it to history or transcript.
        self.send_line(
            f"connect {character} {password}", secret=True, remember=False,
            expected_transport=expected_transport,
        )
        self.ui.set_status(f"Login command sent for {character}")

    def observe_output(self, text: str) -> None:
        if self.character_login is not None:
            return
        self.tail = (self.tail + strip_ansi(text))[-2000:]
        if self._matches(self.profile.password_prompt) and not self.sent_password:
            self.ui.set_secret_entry(True)
            if self.profile.auto_login and self.password is not None:
                try:
                    self.send_line(self.password, secret=True, remember=False)
                except OSError as exc:
                    self.ui.set_status(f"Auto-login send failed: {exc}")
                    return
                self.sent_password = True
                self.ui.set_secret_entry(False)
                self.ui.set_status("Auto-login sent password")
            return
        if self._matches(self.profile.login_prompt) and not self.sent_username:
            if self.profile.auto_login and self.profile.username:
                try:
                    self.send_line(self.profile.username, secret=False, remember=False)
                except OSError as exc:
                    self.ui.set_status(f"Auto-login send failed: {exc}")
                    return
                self.sent_username = True
                self.ui.set_status("Auto-login sent username")

    def mark_manual_secret_sent(self) -> None:
        self.sent_password = True

    def _matches(self, pattern: str) -> bool:
        if not pattern:
            return False
        try:
            return re.search(pattern, self.tail) is not None
        except re.error:
            return False


def sleep_until_stop(stop: threading.Event, seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, seconds)
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.2, remaining))
    return True


def reconnect_session(
    stop: threading.Event,
    controller: SessionController,
    transcript: Transcript,
    reason: str,
) -> bool:
    profile = controller.profile
    ui = controller.ui
    if not profile.reconnect:
        append_client_event(transcript, ui, f"Disconnected: {reason}. Reconnect is disabled.")
        return False

    with controller.reconnect_lock:
        if stop.is_set():
            return False
        if controller.current_connection() is not None:
            return True
        target = connection_target_label(profile)
        append_client_event(transcript, ui, f"Disconnected: {reason}. Reconnecting to {target}.")
        delay = max(0.0, profile.reconnect_initial_delay)
        max_delay = max(delay, profile.reconnect_max_delay)
        attempt = 1
        while not stop.is_set():
            if delay > 0:
                ui.set_status(f"Disconnected; reconnecting in {delay:g}s")
                if sleep_until_stop(stop, delay):
                    return False
            ui.set_status(f"Reconnecting to {target} (attempt {attempt})")
            transport = None
            try:
                transport, codec, initial_banner = open_session_transport(
                    profile, require_private_peer=controller.private_peer_required
                )
                run_sanity_checks(profile, initial_banner, False, False, interactive=False)
            except Exception as exc:
                if transport is not None:
                    transport.close()
                ui.set_status(f"Reconnect failed: {exc}")
                attempt += 1
                delay = min(max_delay, max(1.0, delay * 2 if delay else 1.0))
                continue
            if stop.is_set():
                transport.close()
                return False
            controller.install_connection(transport, codec)
            if initial_banner:
                controller.mark_server_output()
            append_client_event(transcript, ui, f"Reconnected to {target}.")
            if controller.character_login is not None:
                # Login must not block the receiver: it depends on the
                # receiver processing the server's first output.
                threading.Thread(
                    target=send_character_login_after_ready,
                    args=(controller,), daemon=True,
                ).start()
            if initial_banner:
                transcript.write("recv", initial_banner)
                ui.append_output(initial_banner)
                controller.observe_output(initial_banner)
            return True
    return False


def send_character_login_after_ready(controller: SessionController) -> None:
    current = controller.current_connection()
    if current is None:
        return
    transport, _ = current
    try:
        controller.send_character_login(expected_transport=transport)
    except ConnectionChanged:
        # A reconnect superseded this worker; the new connection owns login.
        return
    except (OSError, ValueError) as exc:
        controller.ui.set_status(f"Character login failed: {exc}")


def receiver_loop(
    stop: threading.Event,
    transcript: Transcript,
    controller: SessionController,
) -> None:
    decoder: TerminalStreamDecoder | None = None
    active_transport: Transport | None = None
    while not stop.is_set():
        connection = controller.current_connection()
        if connection is None:
            if not reconnect_session(stop, controller, transcript, "connection unavailable"):
                break
            continue
        transport, codec = connection
        if transport is not active_transport:
            active_transport = transport
            decoder = TerminalStreamDecoder(controller.profile.encoding)
        try:
            data = transport.recv(4096)
        except OSError as exc:
            reason = f"Receive error: {exc}"
            controller.mark_disconnected(transport, reason)
            if not reconnect_session(stop, controller, transcript, reason):
                break
            continue
        if not data:
            reason = "Remote closed the connection"
            controller.mark_disconnected(transport, reason)
            if not reconnect_session(stop, controller, transcript, reason):
                break
            continue
        output, response = codec.feed(data)
        if response:
            try:
                controller.send_raw(response, expected_transport=transport)
            except ConnectionChanged:
                continue
            except OSError as exc:
                reason = f"Negotiation send error: {exc}"
                controller.mark_disconnected(transport, reason)
                if not reconnect_session(stop, controller, transcript, reason):
                    break
                continue
        if output:
            controller.mark_server_output()
            assert decoder is not None
            text = decoder.feed(output)
            transcript.write("recv", text)
            controller.ui.append_output(text)
            controller.observe_output(text)
    if not stop.is_set():
        stop.set()
    controller.ui.set_connected(False)


def session_loop(
    profile: Profile, args: argparse.Namespace,
    on_connected: Callable[[Profile], None] | None = None,
) -> None:
    if not profile.allow_plaintext:
        raise SystemExit("Refusing plaintext Telnet until --allow-plaintext is set on the profile or command.")
    private_peer_required = False
    public_approval_given = False
    if profile.require_private_host and not profile.proxy_command and not profile.socks5_host:
        private_peer_required = is_private_target(profile.host)
        if not private_peer_required:
            # Approval is for this exact host and TCP port, not the domain suffix.
            endpoint = f"{profile.host.rstrip('.').casefold()}:{profile.port}"
            if profile.approved_public_endpoint != endpoint:
                ok = prompt_yes_no(
                    f"{profile.host} does not resolve only to private/link-local addresses. Continue with plaintext Telnet?",
                    default=False,
                    assume_yes=args.yes,
                )
                if not ok:
                    raise SystemExit("Connection aborted by private-host policy")
                public_approval_given = True

    character = getattr(args, "login", None)
    character_password: str | None = None
    if character is not None:
        character = validate_character_name(character)
    set_connection_tab_title(profile, character)
    if character is not None:
        key = character_secret_key(profile, character)
        if Dpapi.available():
            character_password = SecretStore().get_password(key)
        if character_password is None:
            character_password = getpass.getpass(f"Password for {character} on {profile.host}: ")
        if not character_password or "\r" in character_password or "\n" in character_password:
            raise SystemExit("Password must not be empty or contain a newline")
    password: str | None = None
    if profile.auto_login and character is None:
        password = SecretStore().get_password(profile.label())
        if profile.username and password is None:
            password = getpass.getpass(f"Password for {profile.label()} auto-login: ")

    try:
        transport, codec, initial_banner = open_session_transport(
            profile, require_private_peer=private_peer_required
        )
    except OSError as exc:
        raise SystemExit(f"Could not connect to {connection_target_label(profile)}: {exc}") from exc
    try:
        run_sanity_checks(profile, initial_banner, args.accept_host_change, args.yes)
        if public_approval_given:
            profile.approved_public_endpoint = f"{profile.host.rstrip('.').casefold()}:{profile.port}"
        if character is not None:
            if character not in profile.characters:
                profile.characters.append(character)
            if Dpapi.available():
                # Persist a reversible Windows-user-bound encrypted secret.
                SecretStore().set_password(character_secret_key(profile, character), character_password)
        if on_connected is not None:
            on_connected(profile)
    except BaseException:
        transport.close()
        raise

    transcript = Transcript(profile)
    ui = MudUi(profile, no_alt_screen=args.no_alt_screen)
    controller = SessionController(
        profile, transport, codec, transcript, ui, password,
        private_peer_required=private_peer_required,
    )
    if character is not None:
        controller.character_login = (character, character_password)
    stop = threading.Event()
    receiver = threading.Thread(
        target=receiver_loop,
        args=(stop, transcript, controller),
        daemon=True,
    )
    reader: KeyReader | None = None

    try:
        reader = KeyReader()
        ui.start()
        if initial_banner:
            controller.mark_server_output()
            transcript.write("recv", initial_banner)
            ui.append_output(initial_banner)
            controller.observe_output(initial_banner)
        receiver.start()
        if character is not None:
            threading.Thread(
                target=send_character_login_after_ready,
                args=(controller,), daemon=True,
            ).start()
        while not stop.is_set():
            controller.maybe_send_keepalive()
            key = reader.read_key(timeout=0.1)
            if key is None:
                continue
            if key.name == "quit":
                stop.set()
                break
            if key.name == "redraw":
                ui.render(full_clear=True)
            elif key.name == "up":
                ui.scroll(1)
            elif key.name == "down":
                ui.scroll(-1)
            elif key.name == "page_up":
                ui.scroll(fast_page_scroll_amount(ui._body_height()))
            elif key.name == "page_down":
                ui.scroll(-fast_page_scroll_amount(ui._body_height()))
            elif key.name == "switch_input":
                ui.switch_input()
            elif key.name == "ctrl_left":
                ui.move_word(-1)
            elif key.name == "ctrl_right":
                ui.move_word(1)
            elif key.name == "delete_to_end":
                ui.delete_to_end()
            elif key.name == "ctrl_up":
                ui.history_prev()
            elif key.name == "ctrl_down":
                ui.history_next()
            elif key.name == "left":
                ui.move_cursor(-1)
            elif key.name == "right":
                ui.move_cursor(1)
            elif key.name == "home":
                ui.cursor = 0
                ui.render()
            elif key.name == "end":
                ui.cursor = len(ui.input_buffer)
                ui.render()
            elif key.name == "delete":
                ui.delete()
            elif key.name == "backspace":
                ui.backspace()
            elif key.name == "clear_line":
                ui.clear_input()
            elif key.name == "delete_word":
                ui.delete_word()
            elif key.name == "paste":
                handle_paste(ui, controller)
            elif key.name == "f2":
                ui.set_status(f"Log: {transcript.path or 'off'}")
            elif key.name == "f3":
                if ui.history:
                    try:
                        controller.send_line(ui.history[-1], secret=False, remember=False)
                    except OSError as exc:
                        ui.set_status(f"Repeat skipped while reconnecting: {exc}")
                    else:
                        ui.set_status("Repeated last command")
            elif key.name == "f4":
                ui.bottom()
            elif key.name == "f5":
                ui.set_status(
                    "Keys: TAB switch buffer, CTRL+LEFT/RIGHT move words, CTRL+K delete to end, "
                    "UP/DOWN scroll, CTRL+UP/DOWN history, CTRL+N newline, CTRL+V paste, "
                    "/save FILE, /load [FILE], CTRL+] quit"
                )
            elif key.name == "newline":
                if ui.secret_entry:
                    ui.set_status("Newline is disabled for secret entry")
                else:
                    ui.insert_text("\n")
            elif key.name == "enter":
                line = ui.input_buffer
                secret = ui.secret_entry
                ui.bottom()
                if line or secret:
                    if not send_composed_input(line, secret, transcript, ui, controller):
                        continue
                    ui.clear_input()
                if secret:
                    controller.mark_manual_secret_sent()
                    ui.set_secret_entry(False)
            elif key.name == "char":
                ui.insert_text(key.char)
    finally:
        stop.set()
        controller.close()
        if reader is not None:
            reader.close()
        ui.close()
        if transcript.path:
            print(f"Log: {transcript.path}")


