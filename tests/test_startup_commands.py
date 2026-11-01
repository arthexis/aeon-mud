"""Startup Telnet negotiation and reliable first-command tests."""
import threading
import unittest
from unittest import mock

import aeon_mud_client as mud
from aeon_mud import session


class StartupCommandTests(unittest.TestCase):
    def profile(self):
        return mud.Profile(name="tapestries", host="tapestries.fur.com",
                           allow_plaintext=True, sanity_timeout=0.0)

    def controller(self):
        profile = self.profile()
        transport = mock.Mock()
        transcript = mock.Mock()
        ui = mock.Mock()
        controller = mud.SessionController(
            profile, transport, mud.TelnetCodec(), transcript, ui, None
        )
        return controller, transport, transcript

    def test_no_unsolicited_naws_on_new_connection(self):
        transport = mock.Mock()
        with mock.patch.object(session, "make_transport", return_value=transport):
            returned, codec, banner = mud.open_session_transport(self.profile())
        self.assertIs(returned, transport)
        transport.sendall.assert_not_called()
        self.assertEqual(banner, "")
        output, response = codec.feed(bytes([mud.IAC, mud.DO, mud.TELNET_NAWS]))
        self.assertEqual(output, b"")
        self.assertIn(bytes([mud.IAC, mud.WILL, mud.TELNET_NAWS]), response)

    def test_first_command_waits_for_server_output(self):
        controller, transport, transcript = self.controller()
        results = []
        with mock.patch.object(controller.ready_for_commands, "wait",
                               side_effect=lambda timeout: results.append(timeout)):
            controller.send_line("look")
        self.assertEqual(results, [2.0])
        transport.sendall.assert_called_once_with(b"look\r\n")
        transcript.write.assert_called_once_with("send", "look", secret=False)

    def test_first_command_waits_until_ready_event(self):
        controller, transport, _ = self.controller()
        finished = threading.Event()
        def send():
            controller.send_line("north")
            finished.set()
        worker = threading.Thread(target=send, daemon=True)
        worker.start()
        self.assertFalse(finished.wait(0.05))
        transport.sendall.assert_not_called()
        controller.mark_server_output()
        self.assertTrue(finished.wait(0.5))
        worker.join(timeout=1)
        transport.sendall.assert_called_once_with(b"north\r\n")

    def test_deferred_login_is_secret_and_uses_same_transport(self):
        controller, transport, transcript = self.controller()
        controller.character_login = ("Merlin", "swordfish")
        with mock.patch.object(controller.ready_for_commands, "wait",
                               side_effect=lambda timeout: None):
            mud.send_character_login_after_ready(controller)
        transport.sendall.assert_called_once_with(b"connect Merlin swordfish\r\n")
        transcript.write.assert_called_once_with(
            "send", "connect Merlin swordfish", secret=True
        )
        controller.ui.remember.assert_not_called()

    def test_stale_login_worker_does_not_send_to_new_connection(self):
        controller, transport, _ = self.controller()
        controller.character_login = ("Merlin", "swordfish")
        newer = mock.Mock()
        def replace(timeout):
            controller.install_connection(newer, mud.TelnetCodec())
        with mock.patch.object(controller.ready_for_commands, "wait", side_effect=replace):
            mud.send_character_login_after_ready(controller)
        transport.sendall.assert_not_called()
        newer.sendall.assert_not_called()


if __name__ == "__main__":
    unittest.main()
