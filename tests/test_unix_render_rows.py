"""Regression coverage for Unix raw-mode output (ONLCR disabled).

A bare LF advances to the next screen row *without returning to column 1*.
Every painted row must therefore carry its own absolute cursor address.
"""
import io
import os
import re
import unittest
from unittest import mock

from aeon_mud import profiles, terminal
from support import make_profile


class RenderRowsTests(unittest.TestCase):
    def _render(self, *, width=12, height=9, full_clear=False):
        with mock.patch.object(profiles.HistoryStore, "list_for", return_value=[]):
            ui = terminal.MudUi(make_profile(name="raw", host="127.0.0.1"))
        ui.started = True
        ui.lines = ["ABCDEFGHIJKLMNO", "\x1b[31mred text\x1b[0m"]
        ui.input_buffer = "go"
        ui.cursor = 2
        ui.other_input_buffer = "say hi"
        ui.other_cursor = 6
        output = io.StringIO()
        with mock.patch.object(terminal.shutil, "get_terminal_size",
                               return_value=os.terminal_size((width, height))), \
             mock.patch.object(terminal.sys, "stdout", output):
            ui.render(full_clear=full_clear)
        return output.getvalue()

    def test_each_row_is_positioned_at_column_one_without_linefeeds(self):
        text = self._render()
        # LF is not a carriage return when stdin's raw tty mode disables ONLCR.
        self.assertNotIn("\n", text)
        self.assertTrue(text.startswith("\x1b[?25l"))
        self.assertTrue(text.endswith("\x1b[?25h"))
        positions = re.findall(r"\x1b\[(\d+);1H\x1b\[2K", text)
        self.assertEqual([int(row) for row in positions], list(range(1, 10)))
        self.assertIn("\x1b[1;1H\x1b[2K\x1b[7m", text)
        self.assertIn("\x1b[2;1H\x1b[2KABCDEFGHIJKL", text)
        self.assertIn("\x1b[3;1H\x1b[2KMNO", text)
        self.assertIn("\x1b[8;1H\x1b[2K> go", text)
        self.assertIn("\x1b[9;1H\x1b[2K> say hi", text)
        self.assertIn("\x1b[8;5H\x1b[?25h", text)

    def test_full_clear_and_redraw_do_not_depend_on_newline_mode(self):
        full = self._render(full_clear=True)
        self.assertIn("\x1b[2J", full)
        self.assertEqual(full.count("\x1b[2J"), 1)
        self.assertEqual(full.count("\x1b[2K"), 9)
        self.assertNotIn("\r", full)
        self.assertNotIn("\n", full)

    def test_terminal_resize_reanchors_both_prompts(self):
        text = self._render(width=20, height=12)
        self.assertIn("\x1b[11;1H\x1b[2K> go", text)
        self.assertIn("\x1b[12;1H\x1b[2K> say hi", text)
        self.assertEqual(text.count("\x1b[2K"), 12)


if __name__ == "__main__":
    unittest.main()
