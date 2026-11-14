"""Hermetic regression tests for memory bounds and resource cleanup (#34)."""
import base64
import gc
import io
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
    MAX_ACTIVE_SGR_CHARS, MAX_DISPLAY_CACHE_ROWS, MAX_HISTORY_ENTRIES,
    MAX_SCROLLBACK_LINE_CHARS, MAX_SCROLLBACK_LINES, MAX_SCROLLBACK_TOTAL_CHARS,
)
from aeon_mud.profiles import Profile
from aeon_mud.session import SessionController, receiver_loop
from aeon_mud.telnet import TelnetCodec
from aeon_mud.terminal import MudUi, update_sgr_state, wrap_ansi
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
                with mock.patch.object(Dpapi, "unprotect", side_effect=lambda data: data):
                    decrypt_transcript(source, output)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertGreater(output.stat().st_size, 10000000)
            self.assertLess(peak, 8 * 1024 * 1024)

    def test_python_cli_help_smoke(self):
        completed = subprocess.run(
            [sys.executable, "-m", "aeon_mud", "--help"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("connect", completed.stdout)


if __name__ == "__main__":
    unittest.main()
