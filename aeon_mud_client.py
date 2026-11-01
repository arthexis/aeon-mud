#!/usr/bin/env python3
"""Local line-oriented Telnet client for MUDs.

The client intentionally treats Telnet as insecure transport. It adds guard
rails around plaintext sessions, but it does not make Telnet cryptographically
safe. For secure hops, use a SOCKS5 proxy or a profile proxy_command such as
OpenSSH's "-W host:port" mode.
"""

from __future__ import annotations

import argparse
import base64
import codecs
import ctypes
import getpass
import hashlib
import ipaddress
import json
import os
import queue
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
from unittest import mock
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Increment 2: retain the legacy import/patch surface while implementation
# infrastructure lives in independent package modules. Session/UI extraction
# will follow in increment 3.
from aeon_mud.config import *
from aeon_mud.profiles import *
from aeon_mud.security import *
from aeon_mud.terminal_text import *
from aeon_mud.telnet import *
from aeon_mud.transport import *
from aeon_mud.transcript import *
from aeon_mud.input import *
from aeon_mud.terminal import *
from aeon_mud.commands import *
from aeon_mud.session import *


def apply_connect_overrides(profile: Profile, args: argparse.Namespace) -> Profile:
    for field_name in (
        "expected_banner",
        "username",
        "transcript",
        "terminal_type",
        "sanity_timeout",
        "keepalive_interval",
        "reconnect_initial_delay",
        "reconnect_max_delay",
    ):
        value = getattr(args, field_name, None)
        if value is not None:
            setattr(profile, field_name, value)
    host = getattr(args, "host", None)
    port = getattr(args, "port", None)
    if host is not None or port is not None:
        try:
            profile.host, profile.port = normalize_host_port(
                host if host is not None else profile.host,
                port if port is not None else profile.port,
                explicit_port=port is not None,
            )
        except HostPortError as exc:
            raise SystemExit(str(exc)) from exc
    if getattr(args, "allow_plaintext", False):
        profile.allow_plaintext = True
    if getattr(args, "allow_public", False):
        profile.require_private_host = False
    if getattr(args, "auto_login", False):
        profile.auto_login = True
    if getattr(args, "no_auto_login", False):
        profile.auto_login = False
    if getattr(args, "no_reconnect", False):
        profile.reconnect = False
    if getattr(args, "socks5", None):
        try:
            host, port = parse_host_port(args.socks5)
        except HostPortError as exc:
            raise SystemExit(f"Invalid --socks5 value: {exc}") from exc
        profile.socks5_host = host
        profile.socks5_port = port
    if getattr(args, "proxy_command", None):
        profile.proxy_command = args.proxy_command
    return profile


def profile_from_args(args: argparse.Namespace, store: ProfileStore | None = None) -> Profile:
    store = store or ProfileStore()
    if args.profile:
        if args.profile in store.data["profiles"]:
            profile = store.get(args.profile)
        else:
            # A unique hostname can also access a saved profile by FQDN.
            profile = store.find_host(args.profile)
            if profile is None and "." not in args.profile and ":" not in args.profile:
                profile = store.find_host(args.profile, short=True)
            if profile is None:
                raise SystemExit(f"Profile not found or ambiguous host alias: {args.profile}")
    else:
        if not args.host:
            raise SystemExit("connect requires a profile name or --host")
        try:
            host, port = normalize_host_port(args.host, args.port, explicit_port=args.port is not None)
        except HostPortError as exc:
            raise SystemExit(str(exc)) from exc
        specified_port = args.port is not None or split_optional_host_port(args.host)[1] is not None
        profile = store.find_host(host, port if specified_port else None)
        if profile is None:
            name = store.auto_profile_name(host, port)
            profile = Profile(name=name, host=host, port=port)
    return apply_connect_overrides(profile, args)


def command_connect(args: argparse.Namespace) -> None:
    store = ProfileStore()
    profile = profile_from_args(args, store)
    original = store.data["profiles"].get(profile.name)

    # Successful sessions persist their hostname profile. Never overwrite a
    # manually named profile when --host/--port temporarily overrides its target.
    def remember(connected: Profile) -> None:
        if original is not None:
            old_host = original.get("host", "").rstrip(".").casefold()
            if old_host != connected.host.rstrip(".").casefold() or original.get("port", DEFAULT_PORT) != connected.port:
                return
        if getattr(args, "allow_public", False) and not connected.proxy_command and not connected.socks5_host:
            # A one-off --allow-public acknowledgement must not disable future
            # checks for unrelated endpoints when saved automatically.
            connected.approved_public_endpoint = f"{connected.host.rstrip('.').casefold()}:{connected.port}"
            if original is None:
                connected.require_private_host = True
        store.set(connected)

    session_loop(profile, args, on_connected=remember)


def command_profile_list(_args: argparse.Namespace) -> None:
    store = ProfileStore()
    if not store.names():
        print(f"No profiles yet. Config path: {store.path}")
        return
    for name in store.names():
        profile = store.get(name)
        wrapper = "proxy" if profile.proxy_command else ("socks5" if profile.socks5_host else "direct")
        private = "private-only" if profile.require_private_host else "public-allowed"
        print(f"{name}\t{profile.host}:{profile.port}\t{wrapper}\t{private}\tlog:{profile.transcript}")


def command_profile_show(args: argparse.Namespace) -> None:
    profile = ProfileStore().get(args.name)
    print(compact_json(profile.to_stored_dict()))


def command_profile_remove(args: argparse.Namespace) -> None:
    ProfileStore().remove(args.name)
    print(f"Removed profile {args.name}")


def command_profile_add(args: argparse.Namespace) -> None:
    if not args.allow_plaintext:
        raise SystemExit("Use --allow-plaintext to acknowledge that this profile uses insecure Telnet.")
    try:
        host, port = normalize_host_port(args.host, args.port, explicit_port=args.port is not None)
    except HostPortError as exc:
        raise SystemExit(str(exc)) from exc
    profile = Profile(
        name=args.name,
        host=host,
        port=port,
        allow_plaintext=True,
        require_private_host=not args.allow_public,
        expected_banner=args.expected_banner or "",
        username=args.username or "",
        auto_login=args.auto_login,
        transcript=args.transcript,
        terminal_type=args.terminal_type,
        keepalive_interval=args.keepalive_interval,
        reconnect=not args.no_reconnect,
        reconnect_initial_delay=args.reconnect_initial_delay,
        reconnect_max_delay=args.reconnect_max_delay,
    )
    if args.socks5:
        try:
            profile.socks5_host, profile.socks5_port = parse_host_port(args.socks5)
        except HostPortError as exc:
            raise SystemExit(f"Invalid --socks5 value: {exc}") from exc
    if args.proxy_command:
        profile.proxy_command = args.proxy_command
    ProfileStore().set(profile)
    print(f"Saved profile {profile.name} in {profile_path()}")


def resolve_secret_profile(name: str) -> Profile:
    store = ProfileStore()
    if name in store.data["profiles"]:
        return store.get(name)
    profile = store.find_host(name)
    if profile is None and "." not in name and ":" not in name:
        profile = store.find_host(name, short=True)
    if profile is None:
        raise SystemExit(f"Profile not found or ambiguous host alias: {name}")
    return profile


def command_secret_set(args: argparse.Namespace) -> None:
    if not Dpapi.available():
        raise SystemExit("Stored secrets require Windows DPAPI")
    if args.character:
        profile = resolve_secret_profile(args.profile)
        character = validate_character_name(args.character)
        password = args.password or getpass.getpass(f"Password for {character} on {profile.host}: ")
        if not password or "\r" in password or "\n" in password:
            raise SystemExit("Password must not be empty or contain a newline")
        SecretStore().set_password(character_secret_key(profile, character), password)
        if character not in profile.characters:
            profile.characters.append(character)
            ProfileStore().set(profile)
        print(f"Stored encrypted password for {character} on {profile.host}")
        return
    password = args.password or getpass.getpass(f"Password for {args.profile}: ")
    SecretStore().set_password(args.profile, password)
    print(f"Stored DPAPI-protected password for {args.profile}")


def command_secret_clear(args: argparse.Namespace) -> None:
    if args.character:
        profile = resolve_secret_profile(args.profile)
        character = validate_character_name(args.character)
        SecretStore().clear_password(character_secret_key(profile, character))
        print(f"Cleared saved password for {character} on {profile.host}")
        return
    SecretStore().clear_password(args.profile)
    print(f"Cleared password for {args.profile}")


def command_history_clear(args: argparse.Namespace) -> None:
    HistoryStore().clear(args.profile)
    print(f"Cleared history for {args.profile}")


def command_transcript_decrypt(args: argparse.Namespace) -> None:
    decrypt_transcript(Path(args.file), Path(args.out) if args.out else None)


def command_paths(_args: argparse.Namespace) -> None:
    print(f"config_dir: {config_dir()}")
    print(f"profiles: {profile_path()}")
    print(f"known_hosts: {known_hosts_path()}")
    print(f"history: {history_path()}")
    print(f"secrets: {secrets_path()}")
    print(f"transcripts: {transcript_dir()}")


def command_self_test(_args: argparse.Namespace) -> None:
    codec = TelnetCodec()
    output, response = codec.feed(bytes([IAC, DO, TELNET_NAWS]) + b"hello" + bytes([IAC, WILL, TELNET_ECHO]))
    assert output == b"hello", output
    assert bytes([IAC, WILL, TELNET_NAWS]) in response
    assert bytes([IAC, DO, TELNET_ECHO]) in response

    codec = TelnetCodec("XTERM")
    _output, response = codec.feed(bytes([IAC, SB, TELNET_TTYPE, TTYPE_SEND, IAC, SE]))
    assert b"XTERM" in response

    dangerous = "\x1b]52;c;AAAA\x07safe \x1b[31mred\x1b[0m \x1b[2Jclear"
    sanitized = sanitize_terminal_text(dangerous)
    assert "\x1b]52" not in sanitized
    assert "\x1b[31m" in sanitized
    assert "\x1b[2J" not in sanitized


    # Fragmentation must not leak terminal controls or corrupt UTF-8.
    for split in range(1, len(b"hello\x1b]52;c;secret\x1b\\world")):
        payload = b"hello\x1b]52;c;secret\x1b\\world"
        dec = TerminalStreamDecoder("utf-8")
        assert dec.feed(payload[:split]) + dec.feed(payload[split:]) == "helloworld"
    dec = TerminalStreamDecoder("utf-8")
    unicode_bytes = "café".encode("utf-8")
    assert dec.feed(unicode_bytes[:-1]) == "caf"
    assert dec.feed(unicode_bytes[-1:]) == "é"
    dec = TerminalStreamDecoder("utf-8")
    assert dec.feed(b"red\x1b[31") == "red"
    assert dec.feed(b"mtext\x1b[2J") == "\x1b[31mtext"
    codec = TelnetCodec()
    payload = bytes([IAC, SB, TELNET_TTYPE, TTYPE_SEND]) + b"x" * (MAX_TELNET_SUBNEGOTIATION_BYTES + 1)
    for part in (payload[:7], payload[7:], bytes([IAC, SE]) + b"ok"):
        output, response = codec.feed(part)
    assert output == b"ok" and response == b""
    assert codec.sb_data == bytearray() and not codec.sb_overflow
    codec = TelnetCodec()
    output, response = codec.feed(bytes([IAC, SB, TELNET_TTYPE, TTYPE_SEND, IAC, SE]))
    assert b"ANSI" in response and output == b""

    wrapped = wrap_ansi("abcdef", 3)
    assert wrapped == ["abc", "def"], wrapped
    colored = wrap_ansi("\x1b[31mred words\x1b[0m", 5)
    assert [strip_ansi(line) for line in colored] == ["red w", "ords"], colored
    assert colored[1].startswith("\x1b[31m"), colored
    assert key_from_control_char("\n").name == "enter"
    assert key_from_control_char("\x7f").name == "backspace"
    assert key_from_control_char("\x0e").name == "newline"
    assert key_from_control_char("\x16").name == "paste"
    assert key_from_ansi_escape("\x1b[A").name == "up"
    assert key_from_ansi_escape("\x1b[1;5A").name == "ctrl_up"
    assert key_from_ansi_escape("\x1b[5~").name == "page_up"
    assert key_from_ansi_escape("\x1bOQ").name == "f2"
    assert fast_page_scroll_amount(20) == 57
    assert fast_page_scroll_amount(1) == 1
    input_layout = layout_input_buffer("abcdef", 6, 6, "> ")
    assert input_layout.rows == ["> abcd", "  ef"], input_layout
    assert (input_layout.cursor_row, input_layout.cursor_col) == (1, 5), input_layout
    boundary_layout = layout_input_buffer("abcde", 4, 6, "> ")
    assert boundary_layout.rows == ["> abcd", "  e"], boundary_layout
    assert (boundary_layout.cursor_row, boundary_layout.cursor_col) == (1, 3), boundary_layout
    multiline_layout = layout_input_buffer("look\nsay hi", 5, 20, "> ")
    assert multiline_layout.rows == ["> look", "  say hi"], multiline_layout
    assert (multiline_layout.cursor_row, multiline_layout.cursor_col) == (1, 3), multiline_layout
    assert split_composed_input("one\ntwo\n") == ["one", "two"]
    assert split_outbound_buffers("a" * OUTBOUND_BUFFER_CHARS) == ["a" * OUTBOUND_BUFFER_CHARS]
    assert split_outbound_buffers("a" * (OUTBOUND_BUFFER_CHARS + 1)) == [
        "a" * OUTBOUND_BUFFER_CHARS,
        OUTBOUND_SPOOF_PREFIX + "a",
    ]
    long_buffers = split_outbound_buffers("a" * (OUTBOUND_BUFFER_CHARS + 1019))
    assert len(long_buffers) == 3
    assert all(len(buffer) <= OUTBOUND_BUFFER_CHARS for buffer in long_buffers)
    assert long_buffers[1] == OUTBOUND_SPOOF_PREFIX + ("a" * 1018)
    assert long_buffers[2] == OUTBOUND_SPOOF_PREFIX + "a"
    plain_watch = make_watch_rule("You", "cyan")
    assert plain_watch.matches("You see a terminal")
    assert plain_watch.matches("You")
    assert not plain_watch.matches("Then You see a terminal")
    assert apply_watch_rules("You see a terminal", [plain_watch]).startswith("\x1b[36m")
    wildcard_watch = make_watch_rule("You*door", "bright-yellow")
    assert wildcard_watch.matches("You see a door")
    assert wildcard_watch.matches("You door")
    assert not wildcard_watch.matches("They say You see a door")
    assert parse_watch_arguments('"You say" red') == ("You say", "red")
    try:
        make_watch_rule("", "red")
        raise AssertionError("empty watch pattern should fail")
    except ValueError:
        pass
    try:
        make_watch_rule("You", "not-a-color")
        raise AssertionError("unknown watch color should fail")
    except ValueError:
        pass
    cache_profile = Profile(name="cache-test", host="127.0.0.1", allow_plaintext=True)
    cache_ui = MudUi(cache_profile)
    cache_ui.lines = ["abcdef"]
    cached_rows = cache_ui._display_lines(3)
    assert cached_rows == ["abc", "def"], cached_rows
    assert cache_ui._display_lines(3) is cached_rows
    cache_ui.append_output("ghi")
    refreshed_rows = cache_ui._display_lines(3)
    assert refreshed_rows is not cached_rows
    assert refreshed_rows == ["abc", "def", "ghi"], refreshed_rows
    watched_cache_ui = MudUi(cache_profile)
    watched_cache_ui.lines = ["You see a terminal"]
    watched_cache_ui._display_lines(40)
    watched_cache_ui.add_watch("You", "cyan")
    assert watched_cache_ui._display_lines(40)[0].startswith("\x1b[36m")

    profile = Profile(name="test", host="127.0.0.1", allow_plaintext=True)
    encoded = profile.to_stored_dict()
    decoded = Profile.from_dict("test", encoded)
    assert decoded.host == profile.host
    assert decoded.allow_plaintext is True
    assert decoded.transcript == "off"
    assert decoded.sanity_timeout == 0.0
    assert decoded.keepalive_interval == DEFAULT_KEEPALIVE_INTERVAL
    assert decoded.reconnect is True
    assert decoded.reconnect_initial_delay == DEFAULT_RECONNECT_INITIAL_DELAY
    assert decoded.reconnect_max_delay == DEFAULT_RECONNECT_MAX_DELAY


    strict_profile = Profile(name="strict", host="127.0.0.1", expected_banner="login", sanity_timeout=0.1)
    with tempfile.TemporaryDirectory() as sanity_dir:
        with mock.patch(__name__ + ".known_hosts_path", return_value=Path(sanity_dir) / "known.json"):
            run_sanity_checks(strict_profile, "login", False, False, interactive=False)
            for bad_banner in ("", "wrong"):
                try:
                    run_sanity_checks(strict_profile, bad_banner, False, False, interactive=False)
                    raise AssertionError("Reconnect accepted an invalid banner")
                except ValueError:
                    pass
            try:
                run_sanity_checks(strict_profile, "login changed", False, False, interactive=False)
                raise AssertionError("Reconnect accepted changed host banner")
            except ValueError:
                pass

    assert prompt_yes_no("continue", default=True, input_func=lambda _message: "") is True

    def raise_eof(_message: str) -> str:
        raise EOFError

    assert prompt_yes_no("continue", default=False, input_func=raise_eof) is False

    assert resolve_log_path(None).name == DEFAULT_LOG_FILE
    assert resolve_log_path('"named file.log"').name == "named file.log"
    with tempfile.TemporaryDirectory() as temp_dir:
        previous_cwd = os.getcwd()
        os.chdir(temp_dir)
        try:
            log_profile = Profile(name="log-test", host="127.0.0.1", allow_plaintext=True)
            transcript = Transcript(log_profile)
            assert transcript.path is None
            transcript.write("recv", "ignored")
            assert not Path(DEFAULT_LOG_FILE).exists()
            path = transcript.enable_text_log(None)
            assert path == Path(DEFAULT_LOG_FILE)
            transcript.write("recv", "\x1b[31mhello\x1b[0m\n")
            transcript.write("send", "look")
            text = path.read_text(encoding="utf-8")
            assert "hello" in text
            assert "\x1b[31m" not in text
            assert "> look" in text
            transcript.disable()
            assert log_profile.transcript == "off"

            class FakeUi:
                def __init__(self, profile: Profile = log_profile) -> None:
                    self.profile = profile
                    self.status = ""
                    self.connected = True
                    self.remembered: list[str] = []
                    self.watch_rules: list[WatchRule] = []
                    self.profile_config_path: Path | None = None
                    self.lines = [""]
                    self.lock = threading.RLock()

                def set_status(self, text: str) -> None:
                    self.status = text

                def set_connected(self, connected: bool) -> None:
                    self.connected = connected

                def remember(self, line: str) -> None:
                    self.remembered.append(line)

                def append_output(self, text: str) -> None:
                    self.lines.append(text)

                def add_watch(self, pattern: str, color: str) -> WatchRule:
                    rule = make_watch_rule(pattern, color)
                    self.watch_rules = [existing for existing in self.watch_rules if existing.pattern != rule.pattern]
                    self.watch_rules.append(rule)
                    return rule

                def remove_watch(self, pattern: str) -> int:
                    clean_pattern = strip_outer_quotes(pattern)
                    original_count = len(self.watch_rules)
                    self.watch_rules = [rule for rule in self.watch_rules if rule.pattern != clean_pattern]
                    return original_count - len(self.watch_rules)

                def clear_watches(self) -> int:
                    removed = len(self.watch_rules)
                    self.watch_rules = []
                    return removed

                def replace_watches(self, rules: list[WatchRule]) -> None:
                    self.watch_rules = list(rules)

                def describe_watches(self) -> str:
                    return describe_watch_rules(self.watch_rules)

            ui = FakeUi()
            assert handle_local_command("/look", transcript, ui) is False
            assert handle_local_command("/watch You say cyan", transcript, ui) is True
            assert len(ui.watch_rules) == 1
            assert ui.watch_rules[0].pattern == "You say"
            assert apply_watch_rules("You say hello", ui.watch_rules).startswith("\x1b[36m")
            assert handle_local_command("/watch", transcript, ui) is True
            assert "You say=cyan" in ui.status
            assert any("[client] Watches: You say=cyan" in line for line in ui.lines)
            assert handle_local_command("/watch remove You say", transcript, ui) is True
            assert not ui.watch_rules
            assert handle_local_command("/watch * red", transcript, ui) is True
            assert apply_watch_rules("anything", ui.watch_rules).startswith("\x1b[31m")
            assert handle_local_command("/watch clear", transcript, ui) is True
            assert not ui.watch_rules
            assert handle_local_command("/save", transcript, ui) is True
            assert "usage" in ui.status
            assert handle_local_command("/watch You cyan", transcript, ui) is True
            ui.profile.paste_line_delay = 0.25
            assert handle_local_command("/save session-profile.json", transcript, ui) is True
            assert ui.profile_config_path == Path("session-profile.json")
            saved = load_json(Path("session-profile.json"), {})
            assert saved["format"] == SESSION_PROFILE_FORMAT
            assert saved["watch_rules"] == [{"color": "cyan", "pattern": "You"}]
            assert saved["runtime_settings"]["paste_line_delay"] == 0.25
            ui.clear_watches()
            ui.profile.paste_line_delay = 0.01
            assert handle_local_command("/load", transcript, ui) is True
            assert ui.profile.paste_line_delay == 0.25
            assert len(ui.watch_rules) == 1
            assert ui.watch_rules[0].matches("You see a gate")
            assert handle_local_command("/load missing-profile.json", transcript, ui) is True
            assert ui.status.startswith("Load error:")
            assert handle_local_command("/log named.log", transcript, ui) is True
            assert transcript.path == Path("named.log")
            transcript.write("recv", "named")
            assert "named" in Path("named.log").read_text(encoding="utf-8")
            assert handle_local_command("/log off", transcript, ui) is True
            assert transcript.path is None

            class CaptureTransport(Transport):
                def __init__(self) -> None:
                    self.sent: list[bytes] = []

                def sendall(self, data: bytes) -> None:
                    self.sent.append(data)

                def recv(self, size: int) -> bytes:
                    return b""

                def close(self) -> None:
                    pass

            split_profile = Profile(name="split-test", host="127.0.0.1", allow_plaintext=True)
            split_profile.paste_line_delay = 0
            split_transcript = Transcript(split_profile)
            split_ui = FakeUi(split_profile)
            split_transport = CaptureTransport()
            split_controller = SessionController(
                split_profile,
                split_transport,
                TelnetCodec(split_profile.terminal_type),
                split_transcript,
                split_ui,
                None,
            )
            split_controller.send_line("a" * (OUTBOUND_BUFFER_CHARS + 1), secret=False, remember=True)
            assert split_transport.sent == [
                (("a" * OUTBOUND_BUFFER_CHARS) + split_profile.newline).encode(split_profile.encoding),
                (OUTBOUND_SPOOF_PREFIX + "a" + split_profile.newline).encode(split_profile.encoding),
            ]
            assert split_ui.remembered == ["a" * (OUTBOUND_BUFFER_CHARS + 1)]
            assert "2 buffers" in split_ui.status

            class FailingController:
                profile = log_profile

                def send_line(self, line: str, secret: bool = False, remember: bool = True) -> None:
                    raise OSError("offline")

            assert send_composed_input("look", False, transcript, ui, FailingController()) is False
            assert "reconnecting" in ui.status
        finally:
            os.chdir(previous_cwd)

    host, port = normalize_host_port("tapestries.fur.com:2069", None)
    assert host == "tapestries.fur.com"
    assert port == 2069
    host, port = normalize_host_port("[::1]:4000", None)
    assert host == "::1"
    assert port == 4000
    host, port = normalize_host_port("::1", 2069, explicit_port=True)
    assert host == "::1"
    assert port == 2069
    print("Self-test passed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="aeon-mud line-oriented Telnet client")
    subparsers = parser.add_subparsers(dest="command", required=True)

    connect = subparsers.add_parser("connect", help="connect to a profile or host")
    connect.add_argument("profile", nargs="?", help="saved profile name")
    connect.add_argument("--host", help="direct host when no profile is supplied")
    connect.add_argument("--port", type=int, default=None)
    connect.add_argument("--allow-plaintext", action="store_true")
    connect.add_argument("--allow-public", action="store_true", help="allow plaintext target outside private/link-local ranges")
    connect.add_argument("--expected-banner")
    connect.add_argument("--username")
    connect.add_argument("--login", metavar="CHARACTER", help="send 'connect CHARACTER PASSWORD' after connection; save an encrypted per-character password on Windows")
    connect.add_argument("--auto-login", action="store_true")
    connect.add_argument("--no-auto-login", action="store_true")
    connect.add_argument("--transcript", choices=["off", "plain", "encrypted"])
    connect.add_argument("--terminal-type")
    connect.add_argument("--sanity-timeout", type=float)
    connect.add_argument("--keepalive-interval", type=float, help="seconds between idle Telnet NOP keepalives; 0 disables")
    connect.add_argument("--no-reconnect", action="store_true", help="exit instead of reconnecting after disconnect")
    connect.add_argument("--reconnect-initial-delay", type=float, default=None)
    connect.add_argument("--reconnect-max-delay", type=float, default=None)
    connect.add_argument("--socks5", help="SOCKS5 proxy as HOST:PORT")
    connect.add_argument("--proxy-command", nargs=argparse.REMAINDER, help="command that connects stdin/stdout to the remote Telnet stream")
    connect.add_argument("--accept-host-change", action="store_true")
    connect.add_argument("--yes", action="store_true", help="answer yes to safety prompts")
    connect.add_argument("--no-alt-screen", action="store_true")
    connect.set_defaults(func=command_connect)

    profile = subparsers.add_parser("profile", help="manage saved connection profiles")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)

    profile_list = profile_sub.add_parser("list", help="list profiles")
    profile_list.set_defaults(func=command_profile_list)

    profile_show = profile_sub.add_parser("show", help="show one profile")
    profile_show.add_argument("name")
    profile_show.set_defaults(func=command_profile_show)

    profile_remove = profile_sub.add_parser("remove", help="remove one profile")
    profile_remove.add_argument("name")
    profile_remove.set_defaults(func=command_profile_remove)

    profile_add = profile_sub.add_parser("add", help="add or replace a profile")
    profile_add.add_argument("name")
    profile_add.add_argument("--host", required=True)
    profile_add.add_argument("--port", type=int, default=None)
    profile_add.add_argument("--allow-plaintext", action="store_true")
    profile_add.add_argument("--allow-public", action="store_true")
    profile_add.add_argument("--expected-banner")
    profile_add.add_argument("--username")
    profile_add.add_argument("--auto-login", action="store_true")
    profile_add.add_argument("--transcript", choices=["off", "plain", "encrypted"], default="off")
    profile_add.add_argument("--terminal-type", default="ANSI")
    profile_add.add_argument(
        "--keepalive-interval",
        type=float,
        default=DEFAULT_KEEPALIVE_INTERVAL,
        help="seconds between idle Telnet NOP keepalives; 0 disables",
    )
    profile_add.add_argument("--no-reconnect", action="store_true")
    profile_add.add_argument("--reconnect-initial-delay", type=float, default=DEFAULT_RECONNECT_INITIAL_DELAY)
    profile_add.add_argument("--reconnect-max-delay", type=float, default=DEFAULT_RECONNECT_MAX_DELAY)
    profile_add.add_argument("--socks5", help="SOCKS5 proxy as HOST:PORT")
    profile_add.add_argument("--proxy-command", nargs=argparse.REMAINDER)
    profile_add.set_defaults(func=command_profile_add)

    secret = subparsers.add_parser("secret", help="manage DPAPI-protected saved passwords")
    secret_sub = secret.add_subparsers(dest="secret_command", required=True)
    secret_set = secret_sub.add_parser("set", help="store a password")
    secret_set.add_argument("profile")
    secret_set.add_argument("--character", help="character on this server (use with --login)")
    secret_set.add_argument("--password", help="avoid this on shared shells; omit to prompt")
    secret_set.set_defaults(func=command_secret_set)
    secret_clear = secret_sub.add_parser("clear", help="remove a stored password")
    secret_clear.add_argument("profile")
    secret_clear.add_argument("--character", help="clear only this character's password")
    secret_clear.set_defaults(func=command_secret_clear)

    history = subparsers.add_parser("history", help="manage command history")
    history_sub = history.add_subparsers(dest="history_command", required=True)
    history_clear = history_sub.add_parser("clear")
    history_clear.add_argument("profile")
    history_clear.set_defaults(func=command_history_clear)

    transcript = subparsers.add_parser("transcript", help="manage transcripts")
    transcript_sub = transcript.add_subparsers(dest="transcript_command", required=True)
    transcript_decrypt = transcript_sub.add_parser("decrypt", help="decrypt a DPAPI transcript")
    transcript_decrypt.add_argument("file")
    transcript_decrypt.add_argument("--out")
    transcript_decrypt.set_defaults(func=command_transcript_decrypt)

    paths = subparsers.add_parser("paths", help="show local config paths")
    paths.set_defaults(func=command_paths)

    self_test = subparsers.add_parser("self-test", help="run built-in parser checks")
    self_test.set_defaults(func=command_self_test)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nDisconnected (Ctrl+C).")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
