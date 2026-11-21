"""Watch event matching, action parsing, gway dispatch and bounded delivery."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from aeon_mud import commands, profiles, terminal, watch_events
from support import make_profile


class WatchEventTests(unittest.TestCase):
    def profile(self):
        return make_profile(name="realm", host="mud.example", port=4000)

    def test_sound_names_and_argv(self):
        self.assertEqual(watch_events.parse_sound("notice"), (None, "notice"))
        self.assertEqual(watch_events.sound_argv("notice"),
                         ["gway-lcd-sound", "sound", "play", "notice"])
        self.assertEqual(watch_events.parse_sound("github.warning"), ("github", "warning"))
        self.assertEqual(watch_events.sound_argv("github.warning"),
                         ["gway-lcd-sound", "sound", "play", "warning", "--motif", "github"])
        for item in ("", ".notice", "github.", "a.b.c", "--help", "hi;rm", "a/b", "a b"):
            with self.subTest(item=item), self.assertRaises(ValueError):
                watch_events.parse_sound(item)
        self.assertEqual(watch_events.lcd_argv("Warning"), [
            "gway-lcd-sound", "lcd", "show", "MUD WATCH", "Warning", "--seconds", "15"])
        with self.assertRaises(ValueError):
            watch_events.lcd_argv("unsafe\ntext")

    def test_watch_command_actions_and_legacy(self):
        self.assertEqual(commands.parse_watch_with_actions('"You have mail" --color cyan --sound github.notice --lcd "NEW MAIL" --cooldown 30'),
                         ("You have mail", "cyan", "github.notice", "NEW MAIL", 30, False, False))
        self.assertEqual(commands.parse_watch_with_actions('dragon red'),
                         ("dragon", "red", None, None, 5, False, False))
        self.assertEqual(commands.parse_watch_with_actions('"danger red" --sound notice'),
                         ("danger red", "yellow", "notice", None, 5, False, False))
        self.assertEqual(commands.parse_watch_with_actions('"dragon appears" --sound attention'),
                         ("dragon appears", "yellow", "attention", None, 5, False, False))
        self.assertEqual(commands.parse_watch_with_actions('"^dragon.*" --re --search --sound github.warning'),
                         ("^dragon.*", "yellow", "github.warning", None, 5, True, True))
        self.assertEqual(commands.parse_watch_with_actions('dragon --search --re'),
                         ("dragon", "yellow", None, None, 5, True, True))
        for item in ("x --sound", "x --sound a.b.c", "x --cooldown nan",
                     "x --cooldown -3", "x --lcd", "x --bad flag"):
            with self.subTest(item=item), self.assertRaises(ValueError):
                commands.parse_watch_with_actions(item)

    def test_persistence_and_legacy_rule_upgrade(self):
        old = terminal.watch_rules_from_data([{"pattern": "alert", "color": "red"}])
        self.assertEqual(terminal.watch_rules_to_data(old), [{"pattern": "alert", "color": "red"}])
        self.assertFalse(old[0].search)
        self.assertFalse(old[0].use_regex)
        self.assertFalse(old[0].matches("some alert"))
        self.assertTrue(old[0].matches("alert here"))
        rule = terminal.make_watch_rule("dragon*", "cyan", sound="github.warning",
                                        lcd="DRAGON", cooldown=60)
        encoded = terminal.watch_rules_to_data([rule])
        self.assertEqual(encoded[0]["sound"], "github.warning")
        decoded = terminal.watch_rules_from_data(encoded)
        self.assertEqual(decoded[0].sound, rule.sound)
        self.assertEqual(decoded[0].lcd, rule.lcd)
        self.assertEqual(decoded[0].cooldown, 60)
        advanced = terminal.make_watch_rule(r"dragon\d+", "cyan", sound="notice",
                                            search=True, use_regex=True)
        round_trip = terminal.watch_rules_from_data(terminal.watch_rules_to_data([advanced]))
        self.assertTrue(round_trip[0].search)
        self.assertTrue(round_trip[0].use_regex)
        self.assertTrue(round_trip[0].matches("A dragon42 appears"))
        self.assertFalse(round_trip[0].matches("A dragon appears"))
        for bad in ({"pattern": "[", "color": "yellow", "re": True},
                    {"pattern": "danger", "color": "yellow", "search": "true"},
                    {"pattern": "danger", "color": "yellow", "re": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                terminal.watch_rules_from_data([bad])
        with self.assertRaises(ValueError):
            terminal.watch_rules_from_data([{"pattern": "dragon", "color": "red", "sound": "a.b.c"}])

    def test_fragmented_ansi_line_emits_once_and_ignores_local_feedback(self):
        called = []
        bus = watch_events.WatchEventBus(executor=lambda argv: called.append(argv))
        ui = terminal.MudUi(self.profile())
        ui.watch_bus = bus
        ui.watch_rules = [terminal.make_watch_rule("dragon", "red", search=True, sound="github.warning",
                                                    lcd="DANGER", cooldown=0)]
        ui.character = "Merlin"
        ui.append_output("\x1b[31mThe dra")
        ui.append_output("gon appears\x1b[0m\r")
        ui.append_output("\nThe dragon fled\n", watch_events=False)
        bus.pending.join()
        self.assertEqual(called, [
            ["gway-lcd-sound", "sound", "play", "warning", "--motif", "github"],
            ["gway-lcd-sound", "lcd", "show", "MUD WATCH", "DANGER", "--seconds", "15"],
        ])
        receipt = bus.receipts[0]
        self.assertEqual(receipt["status"], "delivered")
        self.assertEqual(receipt["realm"], "mud.example:4000")
        ui.close()

    def test_four_match_modes_for_highlighting_and_events(self):
        cases = [
            ("dragon", False, False, "dragon attacks", True),
            ("dragon", False, False, "A dragon attacks", False),
            ("dragon", True, False, "A dragon attacks", True),
            ("dr.gon", False, False, "dragon attacks", False),
            ("dr.gon", False, True, "dragon attacks", True),
            ("dr.gon", False, True, "A dragon attacks", False),
            ("dr.gon", True, True, "A dragon attacks", True),
            ("dragon*attacks", False, False, "dragon fiercely attacks", True),
        ]
        for pattern, search, use_regex, line, expected in cases:
            with self.subTest(pattern=pattern, search=search, re=use_regex, line=line):
                rule = terminal.make_watch_rule(pattern, "yellow", search=search,
                                                use_regex=use_regex, sound="notice", cooldown=0)
                self.assertEqual(rule.matches(line), expected)
                self.assertEqual(terminal.apply_watch_rules(line, [rule]).startswith("\x1b[33m"), expected)
                dispatched = []
                bus = watch_events.WatchEventBus(executor=lambda argv: dispatched.append(argv))
                bus.feed(line + "\n", (rule,), self.profile())
                bus.pending.join()
                self.assertEqual(len(dispatched), int(expected))
                bus.close()

    def test_invalid_regex_rejected_cleanly(self):
        for pattern in ("[", "(", "*"):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ValueError, "invalid watch regex"):
                terminal.make_watch_rule(pattern, "cyan", use_regex=True)
        self.assertTrue(terminal.make_watch_rule("*", "yellow").matches("anything"))

    def test_cooldown_drops_repeated_match_without_blocking_receive(self):
        t = [100.0]
        events = []
        bus = watch_events.WatchEventBus(executor=lambda argv: events.append(argv),
                                         clock=lambda: t[0])
        rules = (terminal.make_watch_rule("attack", "red", sound="warning", cooldown=20),)
        bus.feed("attack\nattack\n", rules, self.profile(), "Merlin")
        bus.pending.join()
        self.assertEqual(len(events), 1)
        t[0] += 21
        bus.feed("attack\n", rules, self.profile(), "Merlin")
        bus.pending.join()
        self.assertEqual(len(events), 2)
        bus.close()
        bus.feed("attack\n", rules, self.profile(), "Merlin")
        self.assertEqual(len(events), 2)

    def test_missing_provider_does_not_break_client(self):
        def fail(_argv):
            raise FileNotFoundError("no provider")
        bus = watch_events.WatchEventBus(executor=fail)
        bus.feed("alert\n", (terminal.make_watch_rule("alert", "yellow", sound="notice"),),
                 self.profile())
        bus.pending.join()
        self.assertEqual(bus.receipts[0]["status"], "failed")
        self.assertEqual(bus.receipts[0]["errors"], ["FileNotFoundError"])
        bus.close()

    def test_action_dispatch_never_uses_shell_or_untrusted_message(self):
        with mock.patch.object(watch_events.subprocess, "run") as run:
            watch_events._run_local(watch_events.sound_argv("mcp.notice"))
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["gway-lcd-sound", "sound", "play", "notice", "--motif", "mcp"])
        self.assertEqual(run.call_args.kwargs["timeout"], 10)
        self.assertNotIn("shell", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
