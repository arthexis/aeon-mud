"""Hermetic regression tests for memory bounds and resource cleanup (#34)."""
import argparse
import base64
import gc
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import tracemalloc
import unittest
from unittest import mock

from aeon_mud.commands import coerce_runtime_setting
from aeon_mud.config import (
    MAX_ACTIVE_SGR_CHARS, MAX_DISPLAY_CACHE_BYTES, MAX_DISPLAY_CACHE_ROWS, MAX_HISTORY_ENTRIES,
    MAX_HISTORY_COMMAND_CHARS, MAX_HISTORY_FILE_BYTES,
    MAX_SCROLLBACK_LINE_CHARS, MAX_SCROLLBACK_LINES, MAX_SCROLLBACK_TOTAL_CHARS,
)
from aeon_mud.profiles import Profile, HistoryStore
from aeon_mud.session import SessionController, receiver_loop
from aeon_mud import session as session_module
from aeon_mud.telnet import TelnetCodec
from aeon_mud.terminal import MudUi, update_sgr_state, wrap_ansi
from aeon_mud.terminal_text import strip_ansi
from aeon_mud.terminal_text import iter_outbound_buffers, split_outbound_buffers
from aeon_mud.transcript import decrypt_transcript
from aeon_mud.security import Dpapi
from aeon_mud.transport import ProcessTransport, SocketTransport


class MemoryBoundsTests(unittest.TestCase):
    def profile(self, **kwargs):
        return Profile(name="memory", host="127.0.0.1", **kwargs)

    def test_saved_and_runtime_limits_fail_closed(self):
        for field, maximum in (("max_history", MAX_HISTORY_ENTRIES),
                               ("max_scrollback", MAX_SCROLLBACK_LINES)):
            for bad in (0, -1, True, 1.5, "200", maximum + 1):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValueError):
                        Profile.from_dict("memory", {"host": "127.0.0.1", field: bad})
                    with self.assertRaises(ValueError):
                        coerce_runtime_setting(field, bad)
            self.assertEqual(coerce_runtime_setting(field, maximum), maximum)

    def test_scrollback_bounds_physical_lines_and_total_bytes(self):
        ui = MudUi(self.profile())
        ui.render = lambda *args, **kwargs: None
        for _ in range(110):
            ui.append_output("Z" * (MAX_SCROLLBACK_LINE_CHARS + 4000) + "\n")
        self.assertTrue(ui.lines)
        self.assertTrue(all(len(line) <= MAX_SCROLLBACK_LINE_CHARS for line in ui.lines))
        self.assertLessEqual(sum(map(len, ui.lines)), MAX_SCROLLBACK_TOTAL_CHARS)
        self.assertLessEqual(len(ui.lines), ui.profile.max_scrollback)

    def test_wrapped_display_cache_has_fixed_row_budget(self):
        ui = MudUi(self.profile())
        ui.lines = ["X" * MAX_SCROLLBACK_LINE_CHARS for _ in range(6)]
        rows = ui._display_lines(1)
        self.assertLessEqual(len(rows), MAX_DISPLAY_CACHE_ROWS)
        self.assertIs(rows, ui._display_lines(1))

    def test_ansi_state_does_not_grow_quadratically(self):
        state = ""
        for _ in range(10000):
            state = update_sgr_state(state, "\x1b[31m")
            self.assertLessEqual(len(state), MAX_ACTIVE_SGR_CHARS)
        wrapped = wrap_ansi("\x1b[31m" * 5000 + "HELLO", 2)
        self.assertTrue(wrapped)
        self.assertLess(max(map(len, wrapped)), 100000)

    def test_outbound_iterator_preserves_legacy_and_is_lazy(self):
        line = "set description=" + ("a long word " * 500)
        self.assertEqual(list(iter_outbound_buffers(line)), split_outbound_buffers(line))
        # An almost-full prefix previously made a huge eager list in send_line.
        prefix = "x" * 1020 + "="
        huge = prefix + "Z" * 16000
        tracemalloc.start()
        try:
            n = 0
            for chunk in iter_outbound_buffers(huge):
                self.assertLessEqual(len(chunk), 1024)
                self.assertTrue(chunk.startswith(prefix))
                n += 1
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertGreater(n, 1000)
        self.assertLess(peak, 8 * 1024 * 1024)

    def test_proxy_close_reaps_killed_child_and_closes_pipes(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("proxy", 2), 0]
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO()
        transport = ProcessTransport.__new__(ProcessTransport)
        transport.process = process
        transport.close()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)

    @unittest.skipUnless(os.path.exists("/proc/self/fd"), "Linux-only FD smoke")
    def test_real_proxy_lifecycle_does_not_leak_fds(self):
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(6):
            transport = ProcessTransport([sys.executable, "-c", "pass"])
            transport.close()
            self.assertIsNotNone(transport.process.poll())
        gc.collect()
        self.assertLessEqual(len(os.listdir("/proc/self/fd")), before + 2)

    def test_receiver_unblocks_after_disconnect(self):
        a, b = socket.socketpair()
        try:
            profile = self.profile(reconnect=False)
            ui = mock.Mock()
            transcript = mock.Mock()
            controller = SessionController(
                profile, SocketTransport(a), TelnetCodec(), transcript, ui, None
            )
            stop = threading.Event()
            receiver = threading.Thread(
                target=receiver_loop, args=(stop, transcript, controller), daemon=True
            )
            receiver.start()
            stop.set()
            controller.close()
            receiver.join(timeout=2.0)
            self.assertFalse(receiver.is_alive(), "receiver should not survive close")
        finally:
            b.close()
            a.close()

    def test_reconnect_releases_old_transport_and_worker(self):
        stop = threading.Event()

        class FakeTransport:
            def __init__(self, outcomes):
                self.outcomes = iter(outcomes)
                self.closed = False

            def recv(self, _size):
                outcome = next(self.outcomes)
                if outcome is None:
                    stop.set()
                    return b""
                return outcome

            def sendall(self, _data):
                pass

            def close(self):
                self.closed = True

        original = FakeTransport([b""])
        recovered = FakeTransport([b"back online\n", None])
        profile = self.profile(
            reconnect=True, reconnect_initial_delay=0.0, reconnect_max_delay=0.0
        )
        ui = mock.Mock()
        transcript = mock.Mock()
        controller = SessionController(
            profile, original, TelnetCodec(), transcript, ui, None
        )
        with mock.patch(
            "aeon_mud.session.open_session_transport",
            return_value=(recovered, TelnetCodec(), ""),
        ) as opener, mock.patch("aeon_mud.session.run_sanity_checks"):
            receiver = threading.Thread(
                target=receiver_loop, args=(stop, transcript, controller), daemon=True
            )
            receiver.start()
            receiver.join(timeout=2.0)
        self.assertFalse(receiver.is_alive())
        self.assertTrue(original.closed)
        self.assertTrue(recovered.closed)
        opener.assert_called_once()
        ui.append_output.assert_any_call("back online\n")

    def test_ansi_carry_preserves_independent_styles_after_adversarial_repeats(self):
        text = "\x1b[1m" + "\x1b[31m" * 5000 + "ABCD"
        rows = wrap_ansi(text, 2)
        self.assertEqual([strip_ansi(row) for row in rows], ["AB", "CD"])
        self.assertIn("\x1b[1m", rows[1])  # bold is not lost when color repeats
        self.assertIn("\x1b[31m", rows[1])
        self.assertLessEqual(len(update_sgr_state("", "\x1b[1;31m")), MAX_ACTIVE_SGR_CHARS)
        truecolor = wrap_ansi("\x1b[1;4m\x1b[38;2;12;34;56mXY", 1)
        self.assertIn("\x1b[1m", truecolor[1])
        self.assertIn("\x1b[4m", truecolor[1])
        self.assertIn("\x1b[38;2;12;34;56m", truecolor[1])
        cleared = wrap_ansi("\x1b[1;31m\x1b[22;39mXY", 1)
        self.assertNotIn("\x1b[1m", cleared[1])
        self.assertNotIn("\x1b[31m", cleared[1])

    def test_cache_has_encoded_byte_budget(self):
        ui = MudUi(self.profile())
        # UTF-8 output occupies more memory than the character count suggests.
        ui.lines = ["é" * 12000 for _ in range(120)]
        rows = ui._display_lines(1024)
        self.assertTrue(rows)
        self.assertLessEqual(len(rows), MAX_DISPLAY_CACHE_ROWS)
        self.assertLessEqual(
            sum(len(row.encode("utf-8", "replace")) for row in rows),
            MAX_DISPLAY_CACHE_BYTES,
        )
        tracemalloc.start()
        try:
            ui._invalidate_display_cache()
            ui._display_lines(1024)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # Payload budget is not a strict Python heap or physical RSS bound.
        self.assertLess(peak, 32 * 1024 * 1024)

    def test_startup_failures_close_transport_before_main_loop(self):
        profile = self.profile(allow_plaintext=True, require_private_host=False)
        args = argparse.Namespace(
            login=None, accept_host_change=False, yes=True, no_alt_screen=True
        )
        for failed in ("Transcript", "MudUi", "KeyReader"):
            with self.subTest(failed=failed):
                transport = mock.Mock()
                ui = mock.Mock()
                ui.started = False
                transcript = mock.Mock()
                transcript.path = None
                with (
                    mock.patch.object(session_module, "open_session_transport",
                                      return_value=(transport, TelnetCodec(), "")),
                    mock.patch.object(session_module, "run_sanity_checks"),
                    mock.patch.object(session_module, "set_connection_tab_title"),
                    mock.patch.object(session_module, "Transcript", return_value=transcript),
                    mock.patch.object(session_module, "MudUi", return_value=ui),
                    mock.patch.object(session_module, "KeyReader") as reader,
                ):
                    if failed == "Transcript":
                        session_module.Transcript.side_effect = RuntimeError("transcript")
                    elif failed == "MudUi":
                        session_module.MudUi.side_effect = RuntimeError("terminal")
                    else:
                        reader.side_effect = RuntimeError("reader")
                    with self.assertRaises(RuntimeError):
                        session_module.session_loop(profile, args)
                transport.close.assert_called_once()

    def test_registered_login_workers_prune_and_join_on_close(self):
        controller = SessionController(
            self.profile(), mock.Mock(), TelnetCodec(), mock.Mock(), mock.Mock(), None,
        )
        controller.character_login = ("Player", "secret")
        entered = threading.Event()
        release = threading.Event()

        def fake_login(_controller):
            entered.set()
            self.assertTrue(release.wait(timeout=2.0))

        with mock.patch.object(session_module, "send_character_login_after_ready", side_effect=fake_login):
            controller.start_login_worker()
            self.assertTrue(entered.wait(timeout=2.0))
            self.assertEqual(len(controller._login_workers), 1)
            controller.close()
            release.set()
            controller.join_login_workers()
            self.assertTrue(all(not worker.is_alive() for worker in controller._login_workers))
            controller.start_login_worker()
            self.assertEqual(len(controller._login_workers), 1)

    def test_decrypt_rejects_same_source_symlink_and_hardlink(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.dpapi.jsonl"
            source.write_bytes(b"untouched\n")
            self.assertEqual(source.read_bytes(), b"untouched\n")
            for alias in (source, source.parent / "." / source.name):
                with self.subTest(alias=str(alias)):
                    with self.assertRaises(ValueError):
                        decrypt_transcript(source, alias)
                    self.assertEqual(source.read_bytes(), b"untouched\n")
            hardlink = Path(directory) / "hardlink"
            os.link(source, hardlink)
            with self.assertRaises(ValueError):
                decrypt_transcript(source, hardlink)
            self.assertEqual(source.read_bytes(), b"untouched\n")
            symlink = Path(directory) / "symlink"
            try:
                symlink.symlink_to(source)
            except (OSError, NotImplementedError):
                return  # symlinks require elevated permissions on some Windows runners
            with self.assertRaises(ValueError):
                decrypt_transcript(source, symlink)
            self.assertEqual(source.read_bytes(), b"untouched\n")

    def test_decrypt_error_is_atomic_for_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.dpapi.jsonl"
            destination = Path(directory) / "decoded.jsonl"
            encoded = base64.b64encode(b'{"payload":"test"}')
            original = (encoded + b"\n") * 3
            source.write_bytes(original)
            destination.write_text("previous output\n", encoding="utf-8")
            attempts = 0

            def fail_second(data):
                nonlocal attempts
                attempts += 1
                if attempts == 2:
                    raise RuntimeError("bad encrypted record")
                return data

            with mock.patch.object(Dpapi, "unprotect", new=staticmethod(fail_second)):
                with self.assertRaises(RuntimeError):
                    decrypt_transcript(source, destination)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(destination.read_text(encoding="utf-8"), "previous output\n")
            self.assertEqual(list(Path(directory).glob(".decoded.jsonl.*.tmp")), [])
            with mock.patch.object(Dpapi, "unprotect", new=staticmethod(lambda data: data)):
                decrypt_transcript(source, destination)
            self.assertEqual(destination.read_text(encoding="utf-8"),
                             '{"payload":"test"}\n' * 3)
            self.assertEqual(source.read_bytes(), original)

    def test_decrypt_transcript_streams_large_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jsonl"
            output = Path(directory) / "decoded.jsonl"
            record = base64.b64encode(b'{"text":"' + b"x" * 1000 + b'"}') + b"\n"
            with source.open("wb") as handle:
                for _ in range(10000):
                    handle.write(record)
            tracemalloc.start()
            try:
                with mock.patch.object(Dpapi, "unprotect", new=staticmethod(lambda data: data)):
                    decrypt_transcript(source, output)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertGreater(output.stat().st_size, 10000000)
            self.assertLess(peak, 8 * 1024 * 1024)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux-only RSS estimate")
    def test_linux_rss_peak_delta_under_bounded_output(self):
        import resource

        # ru_maxrss is a process high-water mark in KiB on Linux, not a
        # per-object measurement. This detects gross regressions only.
        before_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        ui = MudUi(self.profile())
        ui.render = lambda *args, **kwargs: None
        for _ in range(64):
            ui.append_output("é" * 8192 + "\n")
        ui._display_lines(80)
        after_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.assertLess(after_kib - before_kib, 96 * 1024)

    def test_cli_rejects_same_input_output_without_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "encrypted.dpapi.jsonl"
            source.write_bytes(b"not encrypted but still precious\n")
            completed = subprocess.run(
                [sys.executable, "-m", "aeon_mud", "transcript", "decrypt",
                 str(source), "--out", str(source)],
                capture_output=True, text=True, timeout=15, check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("different files", completed.stderr)
            self.assertEqual(source.read_bytes(), b"not encrypted but still precious\n")

    def test_persisted_history_reads_only_recent_bounded_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            expected = [f"cmd_{index}" for index in range(4000)]
            contents = {"profiles": {"memory": expected, "unrelated": ["do not touch"]}}
            path.write_text(json.dumps(contents), encoding="utf-8")
            original = path.read_bytes()
            profile = self.profile(max_history=3)
            store = HistoryStore(path)
            self.assertEqual(store.list_for(profile), expected[-3:])
            self.assertEqual(store.data["profiles"]["memory"], expected[-3:])
            # Loading has no side effect on the persisted history or other profiles.
            self.assertEqual(path.read_bytes(), original)
            store.add(profile, "next")
            self.assertEqual(HistoryStore(path).list_for(profile), expected[-2:] + ["next"])
            self.assertEqual(HistoryStore(path).data["profiles"]["unrelated"], ["do not touch"])
            oversized_command = "X" * (MAX_HISTORY_COMMAND_CHARS + 1)
            size_before = path.stat().st_size
            HistoryStore(path).add(profile, oversized_command)
            self.assertEqual(path.stat().st_size, size_before)

    def test_oversized_history_is_read_only_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            # A sparse file avoids allocating the oversized payload in tests.
            with path.open("wb") as writer:
                writer.seek(MAX_HISTORY_FILE_BYTES + 10)
                writer.write(b"!")
            original_size = path.stat().st_size
            store = HistoryStore(path)
            self.assertTrue(store.read_only_oversized)
            self.assertEqual(store.list_for(self.profile()), [])
            store.add(self.profile(), "look")
            self.assertEqual(path.stat().st_size, original_size)
            with self.assertRaises(ValueError):
                store.clear("memory")
            self.assertEqual(path.stat().st_size, original_size)

    def test_reconnect_storm_coalesces_login_to_latest_connection(self):
        profile = self.profile(reconnect=True, reconnect_initial_delay=0.0)
        initial = mock.Mock(name="initial_transport")
        controller = SessionController(profile, initial, TelnetCodec(),
                                       mock.Mock(), mock.Mock(), None)
        controller.character_login = ("Hero", "synthetic-secret")
        entered = threading.Event()
        release = threading.Event()
        seen: list[object] = []

        def controlled_send(expected_transport=None):
            if expected_transport is initial:
                entered.set()
                self.assertTrue(release.wait(timeout=2.0))
                raise session_module.ConnectionChanged("old generation")
            seen.append(expected_transport)

        with mock.patch.object(controller, "send_character_login", side_effect=controlled_send):
            controller.start_login_worker()
            self.assertTrue(entered.wait(timeout=2.0))
            newest = None
            for i in range(80):
                newest = mock.Mock(name=f"generation_{i}")
                controller.install_connection(newest, TelnetCodec())
                controller.start_login_worker()
                self.assertLessEqual(len(controller._login_workers), 1)
            self.assertEqual(seen, [])
            release.set()
            controller.join_login_workers(timeout=2.0)
            self.assertEqual(seen, [newest], "latest generation must not lose login")
            self.assertFalse(any(w.is_alive() for w in controller._login_workers))
        controller.close()

    def test_long_ansi_line_retains_compound_style_without_partial_escape(self):
        ui = MudUi(self.profile())
        ui.render = lambda *args, **kwargs: None
        # The cut passes through a truecolor SGR; clipping must preserve it
        # and reconstruct the previous independent bold attribute.
        seq = "\x1b[38;2;100;80;20m"
        trailing = "b" * (MAX_SCROLLBACK_LINE_CHARS - MAX_ACTIVE_SGR_CHARS - len(seq) + 3)
        incoming = "\x1b[1m" + ("a" * 1700) + seq + trailing
        ui.append_output(incoming)
        retained = ui.lines[-1]
        self.assertLessEqual(len(retained), MAX_SCROLLBACK_LINE_CHARS)
        self.assertTrue(retained.startswith("\x1b[1m" + seq), repr(retained[:90]))
        self.assertTrue(retained.endswith(trailing))
        self.assertEqual(len(retained), len("\x1b[1m" + seq) + len(trailing))
        ui.append_output("postfix")
        self.assertIn("\x1b[1m", ui.lines[-1])
        self.assertIn(seq, ui.lines[-1])
        rows = wrap_ansi(ui.lines[-1], 40)
        self.assertIn("\x1b[1m", rows[-1])
        self.assertIn(seq, rows[-1])
        self.assertLessEqual(sum(map(len, ui.lines)), MAX_SCROLLBACK_TOTAL_CHARS)

    def test_python_cli_help_smoke(self):
        completed = subprocess.run(
            [sys.executable, "-m", "aeon_mud", "--help"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("connect", completed.stdout)


if __name__ == "__main__":
    unittest.main()
