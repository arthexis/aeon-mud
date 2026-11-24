"""Stage 7 release acceptance: offline integrated Watch/Trigger/Puppet/Machine/Log.

All socket writes use a fake controller or loopback Unix-domain Puppet socket.
No MUD server, public network or gameplay credentials are involved.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aeon_mud.action_scheduler import ActionScheduler
from aeon_mud.commands import handle_local_command
from aeon_mud.machine_bridge import MachineBridge, MachineError
from aeon_mud.machine_replay import replay, mermaid
from aeon_mud.machines import load_machine
from aeon_mud.profiles import Profile, ProfileStore
from aeon_mud.puppet import PuppetRequest, PuppetServer, PuppetClient
from aeon_mud.puppet_journal import PuppetJournal
from aeon_mud.terminal import MudUi
from aeon_mud.transcript import Transcript
from aeon_mud.triggers import TriggerEngine
from aeon_mud.watch_events import WatchEventBus

FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "expedition.machine.yaml"


class FakeSession:
    def __init__(self, ui, transcript, profile):
        self.ui = ui
        self.transcript = transcript
        self.profile = profile
        self.profile.paste_line_delay = 0
        self.trigger_ready = True
        self.connected = True
        self.sent = []

    def current_connection(self):
        return object() if self.connected else None

    def send_line(self, line, **kwargs):
        self.sent.append(line)
        self.transcript.write("send", line)


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            mock.patch("aeon_mud.realm_watches.realm_watch_path",
                       return_value=self.root / "realm_watches.json"),
            mock.patch("aeon_mud.triggers.trigger_store_path",
                       return_value=self.root / "triggers.json"),
            mock.patch("aeon_mud.puppet_journal.config_dir",
                       return_value=self.root / "configuration"),
            mock.patch("aeon_mud.commands.ProfileStore",
                       side_effect=lambda: ProfileStore(self.root / "profiles.json")),
            mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}),
        ]
        for patch in self.patches:
            patch.start()
        self.profile = Profile(name="acceptance", host="localhost")
        ProfileStore(self.root / "profiles.json").set(self.profile)
        self.transcript = Transcript(self.profile)
        self.ui = MudUi(self.profile)
        self.ui.character = "Hero"
        self.watch = WatchEventBus(executor=lambda argv: self.watch_commands.append(argv))
        self.watch_commands = []
        self.ui.watch_bus.close()
        self.ui.watch_bus = self.watch
        self.clock = [100.0]
        self.scheduler = ActionScheduler(clock=lambda: self.clock[0])
        self.journal = PuppetJournal("Hero")
        self.transcript.observers.append(self.journal.record)
        self.machine = MachineBridge(clock=lambda: self.clock[0],
                                     reaction=lambda text=None, immediate=False: 0)
        self.machine.attach(journal=self.journal, character="Hero", profile=self.profile,
                            store_root=self.root / "machine-store")
        self.machine.scheduler = self.scheduler
        self.ui.machines = self.machine
        self.trigger = TriggerEngine(self.profile, "Hero", clock=lambda: self.clock[0])
        self.trigger.scheduler = self.scheduler
        self.trigger.journal = self.journal
        self.ui.triggers = self.trigger
        self.puppet = PuppetServer("Hero")
        self.puppet.scheduler = self.scheduler
        self.puppet.journal = self.journal
        self.puppet.machine_bridge = self.machine
        self.controller = FakeSession(self.ui, self.transcript, self.profile)

    def tearDown(self):
        self.puppet.close()
        self.machine.close()
        self.ui.close()
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def local(self, line: str):
        self.assertTrue(handle_local_command(line, self.transcript, self.ui), line)

    def receive(self, chunk: str):
        journal_event = self.transcript.write("recv", chunk)
        self.ui.append_output(chunk)
        self.trigger.feed(chunk)
        self.machine.feed(chunk, cursor=journal_event["seq"])
        return journal_event

    def tick(self):
        self.puppet.prepare()
        self.trigger.prepare()
        self.machine.poll_timers(self.controller)
        self.scheduler.drain(self.controller, max_steps=4)

    def configure_rules(self):
        self.local('//watch "The sword costs" --search --cooldown 0')
        self.local('//trigger "The [item] costs [price] coins" --send "say noted" --immediate')
        self.local(f"//machine load {FIXTURE}")

    def test_integrated_watch_trigger_machine_puppet_log_switch_and_replay(self):
        first = self.root / "first.log"
        second = self.root / "second.log"
        self.local(f"//log --switch {first}")
        self.configure_rules()
        self.local("//machine start expedition")
        run_id = self.machine.list()["instances"][0]["id"]
        self.assertTrue(self.machine.inspect(run_id)["writer"])

        event = self.receive("Welcome to my ")
        self.assertIsNotNone(event)
        self.assertEqual(self.machine.inspect(run_id)["state"], "idle")
        self.receive("shop\n")
        self.assertEqual(self.machine.inspect(run_id)["state"], "quote__waiting")
        self.local(f"//log --switch {second}")
        quote = self.receive("The sword costs 75 coins\n")
        self.receive("Confirm purchase\n")
        self.puppet.pending.put_nowait(
            PuppetRequest(["look"], immediate=True, due=self.clock[0]))
        self.tick()
        self.watch.pending.join()

        self.assertEqual(set(self.controller.sent),
                         {"say noted", "buy sword", "look"})
        self.assertEqual(len(self.controller.sent), 3)
        self.assertEqual(self.trigger.rules[0].state, "spent")
        self.assertEqual(len(self.watch.receipts), 1)
        self.assertEqual(self.watch.receipts[0]["status"], "delivered")
        self.assertEqual(self.machine.inspect(run_id)["state"], "purchasing")
        self.assertEqual(
            {a["origin"] for a in self.journal.actions.values()},
            {"machine", "trigger", "puppet"})
        self.assertEqual(
            next(a for a in self.journal.actions.values() if a["origin"] == "machine")["source_cursor"],
            self.machine.trace(run_id)["trace"][-1]["source_cursor"])
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue(self.journal.path.is_file())

        final = self.receive("You purchase sword\n")
        self.assertEqual(self.machine.inspect(run_id)["status"], "completed")
        self.assertEqual(final["direction"], "recv")
        report = replay(load_machine(FIXTURE), self.journal.path)
        self.assertEqual(report["state"], "completed")
        self.assertEqual(report["status"], "completed")
        self.assertFalse(report["effects_sent"])
        self.assertIn("quote__waiting --> quote__done", mermaid(load_machine(FIXTURE), report))
        self.assertNotIn('"commands":', json.dumps(report))

    def test_ui_local_commands_never_feed_watch_or_machine(self):
        self.configure_rules()
        self.local("//machine start expedition --observe")
        self.local('//watch "Watching" --search --cooldown 0')
        self.local('//machine diagnose')
        self.assertEqual(self.watch.pending.qsize(), 0)
        self.assertEqual(len(self.watch.receipts), 0)
        self.assertEqual(self.machine.list()["instances"][0]["state"], "idle")
        self.assertEqual(self.trigger.rules[0].state, "armed")
        self.assertEqual(self.controller.sent, [])

    def test_shared_failure_isolation_and_no_automatic_retry(self):
        self.configure_rules()
        self.local("//machine start expedition")
        run_id = self.machine.list()["instances"][0]["id"]
        self.receive("Welcome to my shop\nThe sword costs 45 coins\n")
        self.receive("Confirm purchase\n")
        self.puppet.pending.put_nowait(PuppetRequest(["look"], immediate=True,
                                                     due=self.clock[0]))
        original = self.machine.store.commit
        def error(snapshot, *, reason):
            if reason == "dispatch_attempt":
                raise OSError("simulated full disk")
            return original(snapshot, reason=reason)
        with mock.patch.object(self.machine.store, "commit", side_effect=error):
            self.tick()
        self.assertNotIn("buy sword", self.controller.sent)
        self.assertEqual(self.machine.inspect(run_id)["status"], "recovery_required")
        self.assertEqual(set(self.controller.sent), {"say noted", "look"})
        self.assertEqual(self.machine.scheduler.jobs, {})
        self.tick()
        self.assertEqual(len(self.controller.sent), 2)

    def test_disconnect_cancels_actions_and_restart_requires_intervention(self):
        self.local(f"//machine load {FIXTURE}")
        self.local("//machine start expedition")
        run_id = self.machine.list()["instances"][0]["id"]
        self.receive("Welcome to my shop\n")
        self.puppet.pending.put_nowait(PuppetRequest(["look"], due=self.clock[0] + 30))
        self.puppet.prepare()
        self.machine.on_disconnect()
        self.trigger.on_disconnect()
        self.puppet.on_disconnect()
        self.assertEqual(self.machine.status(run_id)["status"], "recovery_required")
        self.assertEqual(self.scheduler.jobs, {})
        self.machine.close()
        replacement = MachineBridge(clock=lambda: self.clock[0],
                                    reaction=lambda text=None, immediate=False: 0)
        replacement.attach(journal=self.journal, character="Hero", profile=self.profile,
                           store_root=self.root / "machine-store")
        self.machine = replacement
        self.machine.scheduler = self.scheduler
        self.controller.connected = True
        self.tick()
        self.assertEqual(self.controller.sent, [])
        self.assertEqual(self.machine.status(run_id)["status"], "recovery_required")
        self.machine.recover(run_id, "abort")
        self.assertEqual(self.machine.status(run_id)["status"], "stopped")

    @unittest.skipUnless(os.name == "posix", "owner-only Puppet socket requires Unix")
    def test_owner_only_socket_observation_without_gameplay_writes(self):
        self.local(f"//machine load {FIXTURE}")
        self.local("//machine start expedition --observe")
        run_id = self.machine.list()["instances"][0]["id"]
        self.puppet.start()
        client = PuppetClient("Hero")
        response = client.machine("diagnose", run_id)
        self.assertEqual(response["id"], run_id)
        self.assertFalse(response["writer"])
        self.assertEqual(response["status"], "running")
        self.assertEqual(self.controller.sent, [])
        self.assertTrue(self.puppet.path.is_socket())

    def test_compat_watch_state_and_trigger_persistence_after_reload(self):
        self.configure_rules()
        watch_count = len(self.ui.watch_rules)
        trigger_rule = self.trigger.rules[0]
        self.receive("The sword costs 50 coins\n")
        self.tick()
        self.assertEqual(trigger_rule.state, "spent")
        self.assertEqual(len(self.ui.watch_rules), watch_count)
        rules = TriggerEngine(self.profile, "Hero", clock=lambda: self.clock[0])
        # Non-persistent triggers are deliberately not reloaded.
        self.assertEqual(rules.rules, [])
        # Existing watch settings survive independently of machine checkpoints.
        fresh = MudUi(self.profile)
        try:
            self.assertEqual(len(fresh.watch_rules), watch_count)
        finally:
            fresh.close()

    def test_no_cross_source_duplicate_command_after_restart(self):
        self.configure_rules()
        self.local("//machine start expedition")
        rid = self.machine.list()["instances"][0]["id"]
        self.receive("Welcome to my shop\nThe sword costs 50 coins\nConfirm purchase\n")
        self.tick()
        self.assertEqual(self.controller.sent.count("buy sword"), 1)
        self.machine.close()
        self.machine = MachineBridge(clock=lambda: self.clock[0])
        self.machine.attach(journal=self.journal, character="Hero", profile=self.profile,
                            store_root=self.root / "machine-store")
        self.machine.scheduler = self.scheduler
        self.tick()
        self.assertEqual(self.controller.sent.count("buy sword"), 1)
        self.assertEqual(self.machine.status(rid)["status"], "recovery_required")


if __name__ == "__main__":
    unittest.main()
