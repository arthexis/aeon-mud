"""Collapsible F1 status bar help regression tests."""
import contextlib
import io
import os
import unittest
from unittest import mock

import aeon_mud_client as mud


class F1HelpTests(unittest.TestCase):
    def make_ui(self):
        with mock.patch.object(mud.HistoryStore, "list_for", return_value=[]):
            ui = mud.MudUi(mud.Profile(name="tapestries", host="tapestries.fur.com",
                                       port=2069, allow_plaintext=True))
        return ui

    def test_f1_recognized_in_windows_and_ansi(self):
        self.assertEqual(mud.KeyReader.EXTENDED_KEYS[";"], "f1")
        self.assertEqual(mud.key_from_ansi_escape("\x1bOP").name, "f1")
        self.assertEqual(mud.key_from_ansi_escape("\x1b[11~").name, "f1")

    def test_bar_collapses_and_expands_without_touching_drafts(self):
        ui = self.make_ui()
        ui.insert_text("first")
        ui.switch_input()
        ui.insert_text("second")
        with mock.patch.object(mud.shutil, "get_terminal_size", return_value=os.terminal_size((90, 30))):
            short = ui._header_rows(90, 30, 2)
            self.assertEqual(len(short), 1)
            self.assertIn("F1 help", short[0])
            body_short = ui._body_height()
            ui.toggle_help()
            full = ui._header_rows(90, 30, 2)
            self.assertGreater(len(full), len(short))
            self.assertIn("Tab switch input", " ".join(full))
            self.assertIn("Ctrl+K", " ".join(full))
            self.assertLess(ui._body_height(), body_short)
            ui.toggle_help()
            self.assertEqual(len(ui._header_rows(90, 30, 2)), 1)
        self.assertEqual((ui.input_buffer, ui.other_input_buffer), ("second", "first"))

    def test_help_clips_on_small_terminals_leaving_output_and_prompts(self):
        ui = self.make_ui()
        ui.toggle_help()
        for width, height in ((40, 9), (60, 12), (20, 5)):
            with self.subTest(size=(width, height)):
                with mock.patch.object(mud.shutil, "get_terminal_size",
                                       return_value=os.terminal_size((width, height))):
                    inputs, _, _ = ui._input_window(width, height)
                    header = ui._header_rows(width, height, len(inputs))
                    self.assertGreaterEqual(ui._body_height(), 1)
                    self.assertLessEqual(len(header) + len(inputs) + ui._body_height(), height)

    def test_render_positions_cursor_below_expanded_help(self):
        ui = self.make_ui()
        ui.started = True
        ui.append_output("welcome")
        with mock.patch.object(mud.shutil, "get_terminal_size",
                               return_value=os.terminal_size((80, 24))):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                ui.render()
            self.assertIn("F1 help", out.getvalue())
            with contextlib.redirect_stdout(io.StringIO()) as out:
                ui.toggle_help()
            expanded = out.getvalue()
            self.assertIn("F1 close", expanded)
            self.assertIn("Ctrl+K", expanded)
            self.assertIn("welcome", expanded)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                ui.toggle_help()
            self.assertNotIn("Ctrl+K delete", out.getvalue())


if __name__ == "__main__":
    unittest.main()
