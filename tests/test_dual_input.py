"""Independent terminal input composers and editing shortcuts."""
import unittest
from unittest import mock


from aeon_mud import input as keyboard, profiles, terminal
from support import make_profile



class DualInputTests(unittest.TestCase):
    def make_ui(self):
        with mock.patch.object(profiles.HistoryStore, "list_for", return_value=["north", "look"]):
            return terminal.MudUi(profiles.Profile(name="dual", host="127.0.0.1", allow_plaintext=True))

    def test_tab_switch_preserves_cursor_and_both_drafts(self):
        ui = self.make_ui()
        ui.insert_text("say first")
        ui.move_cursor(-5)
        self.assertEqual(ui.cursor, 4)
        ui.switch_input()
        ui.insert_text("cast magic")
        ui.move_cursor(-2)
        self.assertEqual((ui.active_input, ui.input_buffer, ui.cursor), (1, "cast magic", 8))
        ui.switch_input()
        self.assertEqual((ui.active_input, ui.input_buffer, ui.cursor), (0, "say first", 4))
        ui.insert_text("!")
        self.assertEqual(ui.input_buffer, "say !first")
        ui.switch_input()
        self.assertEqual((ui.input_buffer, ui.cursor), ("cast magic", 8))

    def test_both_rows_use_plain_prompt_and_active_cursor(self):
        ui = self.make_ui()
        ui.insert_text("first")
        rows, row, column = ui._input_window(80, 24)
        self.assertEqual(rows, ["> first", "> "])
        self.assertEqual((row, column), (0, 8))
        ui.switch_input()
        ui.insert_text("second")
        rows, row, column = ui._input_window(80, 24)
        self.assertEqual(rows, ["> first", "> second"])
        self.assertEqual((row, column), (1, 9))

    def test_editing_actions_are_local_and_word_navigation(self):
        ui = self.make_ui()
        ui.insert_text("cast detect magic")
        ui.move_word(-1)
        self.assertEqual(ui.cursor, len("cast detect "))
        ui.delete_to_end()
        self.assertEqual(ui.input_buffer, "cast detect ")
        ui.switch_input()
        ui.insert_text("look")
        ui.clear_input()
        self.assertEqual(ui.input_buffer, "")
        ui.switch_input()
        self.assertEqual(ui.input_buffer, "cast detect ")
        ui.move_word(-1)
        self.assertEqual(ui.cursor, 5)
        ui.move_word(1)
        self.assertEqual(ui.cursor, len(ui.input_buffer))

    def test_history_draft_restored_and_independent(self):
        ui = self.make_ui()
        ui.insert_text("unfinished")
        ui.history_prev()
        self.assertEqual(ui.input_buffer, "look")
        ui.switch_input()
        ui.insert_text("other unfinished")
        ui.history_prev()
        self.assertEqual(ui.input_buffer, "look")
        ui.history_next()
        self.assertEqual(ui.input_buffer, "other unfinished")
        ui.switch_input()
        ui.history_next()
        self.assertEqual(ui.input_buffer, "unfinished")

    def test_secret_mode_disallows_switch_and_masks_both(self):
        ui = self.make_ui()
        ui.insert_text("first")
        ui.switch_input()
        ui.insert_text("second")
        ui.set_secret_entry(True)
        self.assertEqual((ui.active_input, ui.input_buffer), (0, "first"))
        ui.switch_input()
        self.assertEqual(ui.active_input, 0)
        rows, _, _ = ui._input_window(80, 24)
        self.assertEqual(rows, ["> *****", "> ******"])

    def test_control_keys_and_ansi_word_navigation(self):
        self.assertEqual(keyboard.key_from_control_char("\t").name, "switch_input")
        self.assertEqual(keyboard.key_from_control_char("\x0b").name, "delete_to_end")
        self.assertEqual(keyboard.key_from_control_char("\x0e").name, "newline")
        self.assertEqual(keyboard.key_from_control_char("\x1d").name, "quit")
        self.assertEqual(keyboard.key_from_ansi_escape("\x1b[1;5D").name, "ctrl_left")
        self.assertEqual(keyboard.key_from_ansi_escape("\x1b[1;5C").name, "ctrl_right")

    def test_layout_caps_each_composer_independently(self):
        ui = self.make_ui()
        ui.insert_text("x" * 100)
        ui.switch_input()
        ui.insert_text("y" * 100)
        rows, r, c = ui._input_window(10, 20)
        self.assertLessEqual(len(rows), 8)
        self.assertGreaterEqual(r, 0)
        self.assertGreater(c, 0)
        self.assertTrue(any("x" in item for item in rows))
        self.assertTrue(any("y" in item for item in rows))


if __name__ == "__main__":
    unittest.main()
