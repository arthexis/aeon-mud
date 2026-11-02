"""Copy/paste keyboard behavior and safe multiline clipboard handling."""

import unittest
from unittest import mock

from aeon_mud import commands, input as keyboard


class ClipboardShortcutTests(unittest.TestCase):
    def test_keyboard_has_paste_and_explicit_quit_shortcuts(self):
        self.assertEqual(keyboard.key_from_control_char("\x16").name, "paste")
        self.assertEqual(keyboard.key_from_control_char("\x1d").name, "quit")

    def test_single_line_paste_edits_draft_without_sending(self):
        ui, controller = mock.Mock(), mock.Mock()
        with mock.patch.object(commands, "get_clipboard_text", return_value="look around"):
            commands.handle_paste(ui, controller)
        ui.insert_text.assert_called_once_with("look around")
        controller.send_line.assert_not_called()

    def test_multiline_paste_can_be_cancelled(self):
        ui, controller = mock.Mock(), mock.Mock()
        reader = mock.Mock()
        reader.read_key.return_value = keyboard.Key("char", "n")
        with (
            mock.patch.object(commands, "get_clipboard_text", return_value="north\r\nsouth\r\n"),
            mock.patch.object(commands, "KeyReader", return_value=reader),
        ):
            commands.handle_paste(ui, controller)
        ui.set_modal.assert_called_once()
        ui.clear_modal.assert_called_once()
        ui.set_status.assert_called_with("Paste cancelled")
        controller.send_line.assert_not_called()

    def test_multiline_paste_requires_confirmation_before_sending(self):
        ui, controller = mock.Mock(), mock.Mock()
        ui.input_buffer = "say "
        controller.profile.paste_line_delay = 0
        reader = mock.Mock()
        reader.read_key.return_value = keyboard.Key("char", "y")
        with (
            mock.patch.object(commands, "get_clipboard_text", return_value="hello\r\nworld\r\n"),
            mock.patch.object(commands, "KeyReader", return_value=reader),
            mock.patch.object(commands.time, "sleep"),
        ):
            commands.handle_paste(ui, controller)
        controller.send_line.assert_has_calls([
            mock.call("say hello", secret=False, remember=True),
            mock.call("world", secret=False, remember=True),
        ])
        self.assertEqual(controller.send_line.call_count, 2)
        ui.clear_modal.assert_called_once()


if __name__ == "__main__":
    unittest.main()
