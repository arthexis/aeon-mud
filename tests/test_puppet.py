"""Real Unix socket puppet integration."""
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from aeon_mud.puppet import PuppetRequest, PuppetServer, main, puppet_socket_path
from aeon_mud.commands import send_composed_input


@unittest.skipUnless(os.name == "posix", "Unix sockets only")
class PuppetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def run_with_pump(self, server, controller, args):
        result = []
        worker = threading.Thread(target=lambda: result.append(main(args)))
        worker.start()
        deadline = time.monotonic() + 3
        while worker.is_alive() and time.monotonic() < deadline:
            server.drain(controller)
            time.sleep(0.01)
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        return result

    def test_dispatch_prefix_and_cleanup(self):
        server = PuppetServer("Hero")
        server.start()
        sent = []
        class Controller:
            profile = type("Profile", (), {"paste_line_delay": 0})()
            def current_connection(self): return object()
            def send_line(self, line, **kwargs): sent.append((line, kwargs))
        try:
            self.assertEqual(self.run_with_pump(server, Controller(), ["--prefix", "say ", "hero", "hello", "world"]), [0])
            self.assertEqual(sent, [("say hello world", {"secret": False, "remember": False})])
            self.assertEqual(server.path.stat().st_mode & 0o777, 0o600)
        finally:
            server.close()
        self.assertFalse(server.path.exists())

    def test_duplicate_never_replaces_live_socket(self):
        first = PuppetServer("Hero")
        first.start()
        try:
            with self.assertRaises(OSError):
                PuppetServer("hero").start()
            self.assertTrue(first.path.is_socket())
        finally:
            first.close()

    def test_invalid_character(self):
        for name in ("../hero", "a/b", "a b", ""):
            with self.assertRaises(ValueError):
                puppet_socket_path(name)

    def test_disconnected_session_rejects(self):
        server = PuppetServer("Hero")
        server.start()
        class Controller:
            def current_connection(self): return None
        try:
            self.assertEqual(self.run_with_pump(server, Controller(), ["Hero", "look"]), [1])
        finally:
            server.close()

    def test_local_on_off(self):
        class Controller:
            character_login = ("Hero", "secret")
            puppet = None
        controller = Controller()
        ui, transcript = mock.Mock(), mock.Mock()
        try:
            self.assertTrue(send_composed_input("//puppet on", False, transcript, ui, controller))
            self.assertTrue(controller.puppet.path.is_socket())
            self.assertTrue(send_composed_input("//puppet off", False, transcript, ui, controller))
            self.assertIsNone(controller.puppet)
        finally:
            if controller.puppet:
                controller.puppet.close()


    def test_expired_pending_request_never_dispatches(self):
        server = PuppetServer("Hero")
        sent = []
        class Controller:
            def current_connection(self): return object()
            def send_line(self, line, **kwargs): sent.append(line)
        request = PuppetRequest(["look"])
        server.pending.put_nowait(request)
        self.assertTrue(request.cancel_if_pending())
        server.drain(Controller())
        self.assertEqual(sent, [])
        self.assertEqual(request.error, "puppet request expired")
        self.assertTrue(request.done.is_set())

    def test_started_request_reports_uncertain_outcome_not_cancelled(self):
        request = PuppetRequest(["look"])
        self.assertTrue(request.begin())
        self.assertFalse(request.cancel_if_pending())

    def test_socket_cleanup_does_not_unlink_replacement(self):
        server = PuppetServer("Hero")
        server.start()
        original = server.path
        original.unlink()
        replacement = original.with_name("replacement.sock")
        import socket
        other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            other.bind(str(replacement))
            replacement.rename(original)
            server.close()
            self.assertTrue(original.is_socket())
        finally:
            other.close()
            original.unlink(missing_ok=True)

    def test_multiline_prefix_and_order(self):
        import io
        server = PuppetServer("Hero")
        server.start()
        sent = []
        class Controller:
            profile = type("Profile", (), {"paste_line_delay": 0})()
            def current_connection(self): return object()
            def send_line(self, line, **kwargs): sent.append(line)
        try:
            with mock.patch("sys.stdin", io.StringIO("first\\nsecond\\n")):
                self.assertEqual(self.run_with_pump(server, Controller(), ["--prefix", "say ", "hero"]), [0])
            self.assertEqual(sent, ["say first", "say second"])
        finally:
            server.close()

    def test_send_failure_is_reported_and_stops_batch(self):
        server = PuppetServer("Hero")
        server.start()
        sent = []
        class Controller:
            profile = type("Profile", (), {"paste_line_delay": 0})()
            def current_connection(self): return object()
            def send_line(self, line, **kwargs):
                sent.append(line)
                raise OSError("connection failed")
        import io
        try:
            with mock.patch("sys.stdin", io.StringIO("first\\nsecond\\n")):
                self.assertEqual(self.run_with_pump(server, Controller(), ["hero"]), [1])
            self.assertEqual(sent, ["first"])
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
