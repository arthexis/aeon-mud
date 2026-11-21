"""Regression tests for client-side commands and per-realm watches."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from aeon_mud.profiles import Profile
from aeon_mud.realm_watches import load_realm_watches, realm_key, save_realm_watches
from aeon_mud.terminal import make_watch_rule, apply_watch_rules, parse_watch_arguments, MudUi
from aeon_mud.commands import send_composed_input


class RealmWatchTests(unittest.TestCase):
    def test_pattern_starts_line_by_default_and_search_opt_in(self):
        anchored = [make_watch_rule("dragon*attacks", "yellow")]
        self.assertTrue(apply_watch_rules("dragon suddenly attacks!", anchored).startswith("\x1b[33m"))
        self.assertEqual(apply_watch_rules("A dragon suddenly attacks!", anchored), "A dragon suddenly attacks!")
        anywhere = [make_watch_rule("dragon*attacks", "yellow", search=True)]
        self.assertTrue(apply_watch_rules("A dragon suddenly attacks!", anywhere).startswith("\x1b[33m"))
        self.assertEqual(apply_watch_rules("a peaceful meadow", anywhere), "a peaceful meadow")

    def test_optional_color_and_old_syntax(self):
        self.assertEqual(parse_watch_arguments("an important message"), ("an important message", "yellow"))
        self.assertEqual(parse_watch_arguments('"dragon attacks" --color cyan'), ("dragon attacks", "cyan"))
        self.assertEqual(parse_watch_arguments("dragon red"), ("dragon", "red"))
        with self.assertRaises(ValueError):
            parse_watch_arguments("dragon --color invisible")

    def test_endpoint_key_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("aeon_mud.realm_watches.realm_watch_path", return_value=Path(directory) / "watch.json"):
                first = Profile("one", "Example.COM.", 4000)
                second = Profile("another-character", "example.com", 4000)
                other = Profile("other", "example.com", 4001)
                self.assertEqual(realm_key(first), realm_key(second))
                save_realm_watches(first, [{"pattern": "gold", "color": "yellow"}], False)
                self.assertEqual(load_realm_watches(second), ([{"pattern": "gold", "color": "yellow"}], False))
                self.assertEqual(load_realm_watches(other), ([], True))

    def test_slash_commands_never_sent_and_realm_toggle(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("aeon_mud.realm_watches.realm_watch_path", return_value=Path(directory) / "watch.json"), \
                 patch("aeon_mud.terminal.HistoryStore.list_for", return_value=[]), \
                 patch.object(MudUi, "render"):
                profile = Profile("hero", "example.com", 4000)
                ui = MudUi(profile)
                transcript = Mock()
                controller = Mock()
                controller.profile = profile
                for command in ("//watch dragon", "//unwatch", "//help", "//nonsense"):
                    self.assertTrue(send_composed_input(command, False, transcript, ui, controller))
                controller.send_line.assert_not_called()
                for unknown in ("//watchdog", "//logger", "//savepoint", "//", "//unknown arg"):
                    self.assertTrue(send_composed_input(unknown, False, transcript, ui, controller))
                controller.send_line.assert_not_called()
                self.assertEqual(load_realm_watches(profile), ([{"pattern": "dragon", "color": "yellow"}], False))
                self.assertTrue(send_composed_input("//watch dragon --color blue", False, transcript, ui, controller))
                self.assertEqual(load_realm_watches(profile), ([{"pattern": "dragon", "color": "blue"}], True))
                self.assertTrue(send_composed_input("look", False, transcript, ui, controller))
                controller.send_line.assert_called_once_with("look", secret=False, remember=True)
                self.assertTrue(send_composed_input("/look", False, transcript, ui, controller))
                self.assertEqual(controller.send_line.call_args.args[0], "/look")


if __name__ == "__main__":
    unittest.main()
