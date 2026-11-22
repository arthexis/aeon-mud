"""One-shot trigger behavior, persistence, concurrency, and Puppet correlation."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aeon_mud.profiles import Profile
from aeon_mud.triggers import TriggerEngine, handle_trigger_command, render_template, duration
from aeon_mud.puppet_journal import PuppetJournal
from aeon_mud.transcript import Transcript


class FakeController:
    def __init__(self, transcript, connected=True, fail=False):
        self.transcript = transcript
        self.profile = transcript.profile
        self.connected = connected
        self.fail = fail
        self.sent = []

    def current_connection(self):
        return object() if self.connected else None

    def send_line(self, command, **kwargs):
        self.sent.append(command)
        if self.fail:
            raise OSError("delivery uncertain")
        self.transcript.write("send", command)


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "triggers.json"
        self.patches = [
            mock.patch("aeon_mud.triggers.trigger_store_path", return_value=self.path),
            mock.patch("aeon_mud.puppet_journal.config_dir", return_value=self.root),
        ]
        for patch in self.patches:
            patch.start()
        self.profile = Profile(name="test", host="mud.example")
        self.transcript = Transcript(self.profile)
        self.clock = [100.0]
        self.engine = TriggerEngine(self.profile, "Hero", clock=lambda: self.clock[0])
        self.controller = FakeController(self.transcript)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def command(self, text):
        return handle_trigger_command(text, self.engine)

    def test_sigils_fragmented_once_and_escaped_literals(self):
        rule = self.engine.add(r"*** You receive [amount] gold from [sender]",
                               ["thank [sender]", "say Received {amount} gold"])
        self.engine.feed("*** You receive 25 gold from ")
        self.engine.feed("Gandalf\n*** You receive 30 gold from Saruman\n")
        self.assertEqual(rule.state, "spent")
        self.engine.drain(self.controller)
        self.assertEqual(self.controller.sent, ["thank Gandalf"])
        self.clock[0] += .2
        self.engine.drain(self.controller)
        self.assertEqual(self.controller.sent, ["thank Gandalf", "say Received 25 gold"])
        self.assertEqual(rule.receipt["status"], "sent")
        self.assertEqual(rule.receipt["captures"]["named"], {"amount": "25", "sender": "Gandalf"})
        self.engine.feed("*** You receive 99 gold from X\n")
        self.engine.drain(self.controller)
        self.assertEqual(len(self.controller.sent), 2)

    def test_regex_groups_and_search(self):
        rule = self.engine.add(r"(?P<who>\w+) offers (\d+) coins", ["thank {who}", "say {2}"],
                               use_regex=True, search=True)
        self.engine.feed("NEWS: Gandalf offers 25 coins\n")
        self.engine.drain(self.controller)
        self.clock[0] += 1
        self.engine.drain(self.controller)
        self.assertEqual(self.controller.sent, ["thank Gandalf", "say 25"])
        self.assertEqual(rule.receipt["captures"]["groups"], ["Gandalf", "25"])

    def test_default_starts_line_and_search_opt_in(self):
        self.engine.add("A bell rings", ["wake"])
        self.engine.feed("In the distance A bell rings\n")
        self.engine.drain(self.controller)
        self.assertEqual(self.controller.sent, [])
        self.engine.add("A bell rings", ["look"], search=True)
        self.engine.feed("In the distance A bell rings\n")
        self.engine.drain(self.controller)
        self.assertEqual(self.controller.sent, ["look"])

    def test_persistence_spent_forever_and_once_per_new_session(self):
        forever = self.engine.add("Hunger", ["eat bread"], persist=True, name="food")
        next_session = self.engine.add("Cold", ["wear coat"], persist=True,
                                       once_per_session=True, name="coat")
        self.engine.feed("Hunger\nCold\n")
        self.engine.drain(self.controller)
        self.clock[0] += 1
        self.engine.drain(self.controller)
        self.assertEqual(forever.state, "spent")
        self.assertEqual(next_session.state, "spent")
        fresh = TriggerEngine(self.profile, "Hero")
        self.assertEqual(fresh.status("food")["state"], "spent")
        self.assertEqual(fresh.status("coat")["state"], "armed")
        different = TriggerEngine(self.profile, "Other")
        self.assertEqual(different.rules, [])
        self.assertTrue(self.path.exists())

    def test_rearm_does_not_auto_repeat_on_reconnect(self):
        r = self.engine.add("Danger", ["hide"], persist=True)
        self.engine.feed("Danger\n")
        self.engine.on_disconnect()
        self.controller.connected = True
        self.engine.feed("Danger\n")
        self.engine.drain(self.controller)
        self.assertEqual(self.controller.sent, [])
        self.engine.modify(r.id, "rearm")
        self.engine.feed("Danger\n")
        self.engine.drain(self.controller)
        self.assertEqual(self.controller.sent, ["hide"])

    def test_dry_run_and_test_do_not_send(self):
        r = self.engine.add("A [who] waves", ["wave [who]"], dry_run=True)
        self.assertEqual(self.engine.test(r.id, "A Gandalf waves")["commands"], ["wave Gandalf"])
        self.assertEqual(r.state, "armed")
        self.engine.feed("A Gandalf waves\n")
        self.engine.drain(self.controller)
        self.assertEqual(self.controller.sent, [])
        self.assertEqual(r.receipt["status"], "dry_run")
        self.assertEqual(r.state, "spent")

    def test_pause_all_cancels_queued_commands(self):
        r = self.engine.add("A door opens", ["north"], delay=3)
        self.engine.feed("A door opens\n")
        self.engine.pause(True)
        self.clock[0] += 10
        self.engine.pause(False)
        self.engine.drain(self.controller)
        self.assertEqual(self.controller.sent, [])
        self.assertEqual(r.receipt["status"], "failed")

    def test_failed_send_is_not_retried(self):
        r = self.engine.add("Danger", ["hide"])
        self.controller.fail = True
        self.engine.feed("Danger\n")
        self.engine.drain(self.controller)
        self.assertEqual(r.receipt["status"], "outcome_unknown")
        self.controller.fail = False
        self.engine.drain(self.controller)
        self.assertEqual(self.controller.sent, ["hide"])

    def test_cannot_send_local_commands_or_line_breaks(self):
        for command in ("//log --off", "say a\nquit"):
            with self.assertRaises(ValueError):
                self.engine.add("danger", [command])
        rule = self.engine.add("Gift [sender]", ["{sender}"])
        self.engine.feed("Gift //log --off\n")
        self.engine.drain(self.controller)
        self.assertEqual(self.controller.sent, [])
        self.assertEqual(rule.receipt["status"], "failed")

    def test_response_injection_is_not_recursive(self):
        r = self.engine.add("Gift [sender]", ["say thanks {sender}"])
        self.engine.feed("Gift {other}\n")
        self.engine.drain(self.controller)
        self.assertEqual(self.controller.sent, ["say thanks {other}"])

    def test_parser_and_management(self):
        msg = self.command('//trigger "A guard says [name]" --send "say Hello, {name}" --name greet --persist --delay 500ms')
        self.assertIn("armed", msg)
        self.assertIn("greet", self.command("//trigger --list"))
        rule = self.engine._find("greet")
        self.assertEqual(rule.delay, .5)
        self.assertIn("Hello, Gandalf", self.command('//trigger --test greet "A guard says Gandalf"'))
        self.assertIn("off", self.command("//trigger --off greet"))
        self.assertEqual(rule.state, "disabled")
        self.assertIn("on", self.command("//trigger --on greet"))
        self.engine.feed("A guard says Gandalf\n")
        self.clock[0] += 1
        self.engine.drain(self.controller)
        self.assertEqual(rule.state, "spent")
        self.assertIn("error", self.command("//trigger --on greet").lower())
        self.assertIn("rearm", self.command("//trigger --rearm greet"))
        self.assertIn("remove", self.command("//trigger --remove greet"))
        self.assertFalse(self.engine.rules)

    def test_journal_action_receipt_and_read_last(self):
        journal = PuppetJournal("Hero")
        self.engine.journal = journal
        self.transcript.observers.append(journal.record)
        r = self.engine.add("Door opens", ["north"], name="door")
        self.engine.feed("Door opens\n")
        self.engine.drain(self.controller)
        self.assertTrue(r.receipt["action_id"])
        action = journal.actions[r.receipt["action_id"]]
        self.assertEqual(action["origin"], "trigger")
        self.assertEqual(action["trigger_id"], r.id)
        self.assertEqual(action["trigger_name"], "door")
        self.transcript.write("recv", "You enter a room\n")
        self.assertEqual(journal.read(last=True)["events"][0]["text"], "You enter a room\n")

    def test_other_realm_does_not_load_saved_triggers(self):
        self.engine.add("Hunger", ["eat bread"], persist=True)
        other = Profile(name="else", host="other.example")
        self.assertEqual(TriggerEngine(other, "Hero").rules, [])

    def test_cli_duration_bounds(self):
        self.assertEqual(duration("500ms"), .5)
        self.assertEqual(duration("1m"), 60)
        with self.assertRaises(ValueError):
            duration("100s")


    def test_gameplay_readiness_blocks_dispatch(self):
        rule = self.engine.add("A warning", ["hide"])
        self.engine.feed("A warning\n")
        self.controller.trigger_ready = False
        self.engine.drain(self.controller)
        self.assertEqual(self.controller.sent, [])
        self.assertEqual(rule.state, "spent")
        self.controller.trigger_ready = True
        self.engine.drain(self.controller)
        self.assertEqual(self.controller.sent, ["hide"])

    def test_disconnected_session_cancels_queued_responses(self):
        rule = self.engine.add("A warning", ["hide"], persist=True)
        self.engine.feed("A warning\n")
        self.engine.on_disconnect()
        self.engine.drain(self.controller)
        self.assertEqual(self.controller.sent, [])
        self.assertEqual(rule.receipt["status"], "failed")
        self.assertEqual(TriggerEngine(self.profile, "Hero").status(rule.id)["state"], "spent")


    def test_delayed_rule_does_not_stall_immediate_trigger(self):
        self.engine.add("Door", ["north"], delay=5)
        self.engine.add("Bell", ["wake"])
        self.engine.feed("Door\nBell\n")
        self.engine.drain(self.controller)
        self.assertEqual(self.controller.sent, ["wake"])
        self.clock[0] += 5
        self.engine.drain(self.controller)
        self.assertEqual(self.controller.sent, ["wake", "north"])


if __name__ == "__main__":
    unittest.main()
