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
                         ("You have mail", "cyan", "github.notice", "NEW MAIL", 30))
        self.assertEqual(commands.parse_watch_with_actions('dragon red'),
                         ("dragon", "red", None, None, 5))
        self.assertEqual(commands.parse_watch_with_actions('"dragon appears" --sound attention'),
                         ("dragon appears", "yellow", "attention", None, 5))
        for item in ("x --sound", "x --sound a.b.c", "x --cooldown nan",
                     "x --cooldown -3", "x --lcd", "x --bad flag"):
            with self.subTest(item=item), self.assertRaises(ValueError):
                commands.parse_watch_with_actions(item)

    def test_persistence_and_legacy_rule_upgrade(self):
        old = terminal.watch_rules_from_data([{"pattern": "alert", "color": "red"}])
        self.assertEqual(terminal.watch_rules_to_data(old), [{"pattern": "alert", "color": "red"}])
        rule = terminal.make_watch_rule("dragon*", "cyan", sound="github.warning",
                                        lcd="DRAGON", cooldown=60)
        encoded = terminal.watch_rules_to_data([rule])
        self.assertEqual(encoded[0]["sound"], "github.warning")
        decoded = terminal.watch_rules_from_data(encoded)
        self.assertEqual(decoded[0].sound, rule.sound)
        self.assertEqual(decoded[0].lcd, rule.lcd)
        self.assertEqual(decoded[0].cooldown, 60)
        with self.assertRaises(ValueError):
            terminal.watch_rules_from_data([{"pattern": "dragon", "color": "red", "sound": "a.b.c"}])

    def test_fragmented_ansi_line_emits_once_and_ignores_local_feedback(self):
        called = []
        bus = watch_events.WatchEventBus(executor=lambda argv: called.append(argv))
        ui = terminal.MudUi(self.profile())
        ui.watch_bus = bus
        ui.watch_rules = [terminal.make_watch_rule("dragon", "red", sound="github.warning",
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
