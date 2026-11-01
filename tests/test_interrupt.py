"""Regression tests for clean CLI interruption and session cleanup."""
import contextlib
import io
import unittest
from unittest import mock


import threading
from aeon_mud import cli, input as keyboard, session, telnet, terminal, transcript as transcript_module, transport as transport_module



class InterruptTests(unittest.TestCase):
    def test_ctrl_c_at_cli_boundary_has_no_traceback(self):
        with mock.patch.object(cli, "build_parser") as parser:
            parser.return_value.parse_args.return_value.func.side_effect = KeyboardInterrupt
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = cli.main(["paths"])
        self.assertEqual(status, 130)
        self.assertEqual(output.getvalue(), "\nDisconnected (Ctrl+C).\n")

    def test_ctrl_c_in_keyboard_loop_closes_everything(self):
        transport = mock.MagicMock(spec=transport_module.Transport)
        reader = mock.MagicMock(spec=keyboard.KeyReader)
        reader.read_key.side_effect = KeyboardInterrupt
        ui = mock.MagicMock(spec=terminal.MudUi)
        ui.started = True
        transcript = mock.MagicMock(spec=transcript_module.Transcript)
        transcript.path = None

        with (
            mock.patch.object(session, "is_private_target", return_value=True),
            mock.patch.object(session, "open_session_transport", return_value=(transport, telnet.TelnetCodec(), "")),
            mock.patch.object(session, "run_sanity_checks"),
            mock.patch.object(session, "KeyReader", return_value=reader),
            mock.patch.object(session, "MudUi", return_value=ui),
            mock.patch.object(session, "Transcript", return_value=transcript),
            mock.patch.object(threading, "Thread") as receiver,
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = cli.main(["connect", "--host", "127.0.0.1", "--allow-plaintext"])
        self.assertEqual(status, 130)
        self.assertIn("Disconnected (Ctrl+C).", output.getvalue())
        transport.close.assert_called_once()
        reader.close.assert_called_once()
        ui.close.assert_called_once()
        receiver.return_value.start.assert_called_once()

    def test_ctrl_c_while_validating_closes_socket(self):
        transport = mock.MagicMock(spec=transport_module.Transport)
        with (
            mock.patch.object(session, "is_private_target", return_value=True),
            mock.patch.object(session, "open_session_transport", return_value=(transport, telnet.TelnetCodec(), "")),
            mock.patch.object(session, "run_sanity_checks", side_effect=KeyboardInterrupt),
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = cli.main(["connect", "--host", "127.0.0.1", "--allow-plaintext"])
        self.assertEqual(status, 130)
        transport.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
