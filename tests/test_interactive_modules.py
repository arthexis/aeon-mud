"""Public module boundaries after interactive-client extraction."""

import unittest
from unittest import mock

import aeon_mud_client as legacy
from aeon_mud import commands, input as keyboard, session, terminal


class InteractiveModuleTests(unittest.TestCase):
    def test_legacy_exports_exact_package_objects(self):
        symbols = [
            (keyboard, "Key"),
            (keyboard, "KeyReader"),
            (keyboard, "layout_input_buffer"),
            (keyboard, "key_from_ansi_escape"),
            (terminal, "MudUi"),
            (terminal, "WatchRule"),
            (commands, "handle_local_command"),
            (commands, "send_composed_input"),
            (commands, "get_clipboard_text"),
            (session, "SessionController"),
            (session, "session_loop"),
            (session, "receiver_loop"),
            (session, "open_session_transport"),
        ]
        for module, symbol in symbols:
            with self.subTest(symbol=symbol):
                self.assertIs(getattr(legacy, symbol), getattr(module, symbol))

    def test_keyboard_and_terminal_do_not_depend_on_session(self):
        self.assertNotIn("aeon_mud.session", keyboard.__dict__.get("__package__", ""))
        self.assertFalse(hasattr(terminal, "SessionController"))

    def test_two_composers_survive_import_boundary(self):
        profile = legacy.Profile(name="test", host="127.0.0.1")
        with mock.patch.object(terminal.HistoryStore, "list_for", return_value=[]):
            ui = terminal.MudUi(profile)
        ui.insert_text("look")
        ui.switch_input()
        ui.insert_text("north")
        self.assertEqual(ui.input_buffer, "north")
        ui.switch_input()
        self.assertEqual(ui.input_buffer, "look")

    def test_local_commands_stay_local(self):
        profile = legacy.Profile(name="test", host="127.0.0.1")
        with mock.patch.object(terminal.HistoryStore, "list_for", return_value=[]):
            ui = terminal.MudUi(profile)
        transcript = mock.Mock()
        controller = mock.Mock()
        self.assertTrue(commands.send_composed_input(
            "/watch list", False, transcript, ui, controller
        ))
        controller.send_line.assert_not_called()


if __name__ == "__main__":
    unittest.main()
