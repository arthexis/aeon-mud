"""Stage 3: one bounded foreground scheduler shared by Puppet, Trigger and Machine."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aeon_mud.action_scheduler import ActionScheduler
from aeon_mud.machine_bridge import MachineBridge
from aeon_mud.profiles import Profile
from aeon_mud.puppet import PuppetRequest, PuppetServer
from aeon_mud.puppet_journal import PuppetJournal
from aeon_mud.transcript import Transcript
from aeon_mud.triggers import TriggerEngine

FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "merchant.machine.yaml"


class FakeController:
    def __init__(self, transcript, *, pace=0):
        self.profile = transcript.profile
        self.profile.paste_line_delay = pace
        self.transcript = transcript
        self.trigger_ready = True
        self.sent = []
        self.connected = True
        self.ui = mock.Mock()

    def current_connection(self):
        return object() if self.connected else None

    def send_line(self, line, **_kwargs):
        self.sent.append(line)
        self.transcript.write("send", line)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [mock.patch("aeon_mud.puppet_journal.config_dir", return_value=self.root),
                        mock.patch("aeon_mud.triggers.trigger_store_path", return_value=self.root / "triggers.json")]
        for patch in self.patches:
            patch.start()
        self.now = [100.0]
        self.clock = lambda: self.now[0]
        self.scheduler = ActionScheduler(clock=self.clock, limit=8)
        self.transcript = Transcript(Profile(name="demo", host="localhost"))
        self.journal = PuppetJournal("Hero")
        self.transcript.observers.append(self.journal.record)
        self.controller = FakeController(self.transcript)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def test_global_bounded_queue_and_no_local_command_injection(self):
        for i in range(8):
            self.scheduler.enqueue("trigger", [f"say {i}"])
        with self.assertRaisesRegex(ValueError, "full"):
            self.scheduler.enqueue("machine", ["look"])
        with self.assertRaisesRegex(ValueError, "never local"):
            ActionScheduler().enqueue("trigger", ["//log off"])
        # Puppet remains an opaque send bridge; no client-side command parsing.
        self.assertEqual(ActionScheduler().enqueue("puppet", ["//not-a-local-command"]).commands,
                         ("//not-a-local-command",))

    def test_ordering_and_multiline_pacing_are_shared(self):
        self.controller.profile.paste_line_delay = .5
        self.scheduler.enqueue("puppet", ["north", "south"], due=100)
        self.scheduler.enqueue("trigger", ["wave"], due=100)
        self.scheduler.drain(self.controller, max_steps=3)
        self.assertEqual(self.controller.sent, ["north", "wave"])
        self.now[0] += .5
        self.scheduler.drain(self.controller)
        self.assertEqual(self.controller.sent, ["north", "wave", "south"])

    def test_reaction_delay_is_not_resampled_by_scheduler(self):
        self.scheduler.enqueue("machine", ["look"], due=101)
        self.scheduler.drain(self.controller)
        self.assertEqual(self.controller.sent, [])
        self.now[0] += 1
        self.scheduler.drain(self.controller)
        self.assertEqual(self.controller.sent, ["look"])

    def test_only_foreground_dispatches_and_paused_action_does_not_starve_others(self):
        paused = self.scheduler.enqueue("machine", ["north"], can_send=lambda: False)
        self.scheduler.enqueue("trigger", ["wave"])
        self.scheduler.drain(self.controller, max_steps=3)
        self.assertEqual(self.controller.sent, ["wave"])
        self.assertIn(paused.key, self.scheduler.jobs)

    def test_cancel_before_send_and_uncertain_after_first_send(self):
        first = self.scheduler.enqueue("trigger", ["look"], due=200)
        self.scheduler.cancel(origin="trigger", reason="disabled")
        self.assertEqual(first.status, "cancelled")
        self.controller.profile.paste_line_delay = 2
        job = self.scheduler.enqueue("puppet", ["north", "south"], journal=self.journal)
        self.scheduler.drain(self.controller)
        self.assertEqual(job.status, "sending")
        self.scheduler.cancel(origin="puppet", reason="disconnected")
        self.assertEqual(job.status, "outcome_unknown")
        self.now[0] += 20
        self.scheduler.drain(self.controller)
        self.assertEqual(self.controller.sent, ["north"])

    def test_send_attempt_failure_is_unknown_and_never_retried(self):
        calls = []
        self.controller.send_line = lambda cmd, **kwargs: (calls.append(cmd), (_ for _ in ()).throw(OSError("broken pipe")))
        job = self.scheduler.enqueue("machine", ["buy sword"])
        self.scheduler.drain(self.controller)
        self.assertEqual(job.status, "outcome_unknown")
        self.scheduler.drain(self.controller)
        self.assertEqual(calls, ["buy sword"])

    def test_readiness_does_not_reserve_action_or_send(self):
        self.controller.trigger_ready = False
        job = self.scheduler.enqueue("puppet", ["look"], journal=self.journal)
        self.scheduler.drain(self.controller)
        self.assertEqual(self.controller.sent, [])
        self.assertIsNone(job.action)
        self.assertEqual(len(self.journal.actions), 0)
        self.controller.trigger_ready = True
        self.scheduler.drain(self.controller)
        self.assertEqual(self.controller.sent, ["look"])

    def test_cancel_racing_with_inflight_send_reports_uncertainty(self):
        entered = threading.Event()
        release = threading.Event()
        def send(cmd, **kwargs):
            entered.set()
            release.wait(timeout=1)
            self.controller.sent.append(cmd)
        self.controller.send_line = send
        job = self.scheduler.enqueue("puppet", ["look"], journal=self.journal)
        worker = threading.Thread(target=lambda: self.scheduler.drain(self.controller))
        worker.start()
        self.assertTrue(entered.wait(1))
        self.scheduler.cancel(origin="puppet", reason="stop during send")
        release.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.controller.sent, ["look"])
        self.assertEqual(job.status, "outcome_unknown")
        self.scheduler.drain(self.controller)
        self.assertEqual(self.controller.sent, ["look"])

    def test_shared_three_producers_and_journal_metadata(self):
        machines = MachineBridge(clock=self.clock, reaction=lambda text=None, immediate=False: 0)
        machines.attach(journal=self.journal, character="Hero")
        machines.scheduler = self.scheduler
        machines.load(str(FIXTURE))
        triggers = TriggerEngine(self.controller.profile, "Hero", clock=self.clock)
        triggers.scheduler = self.scheduler
        triggers.journal = self.journal
        puppet = PuppetServer("Hero")
        puppet.scheduler = self.scheduler
        puppet.journal = self.journal

        machine = machines.start("merchant", immediate=True)
        triggers.add("Dragon appears", ["say beware"], immediate=True)
        triggers.feed("Dragon appears\n")
        triggers.prepare()
        request = PuppetRequest(["look"], immediate=True, due=self.now[0])
        puppet.pending.put_nowait(request)
        puppet.prepare()
        self.scheduler.drain(self.controller, max_steps=3)
        self.assertEqual(self.controller.sent, ["list", "say beware", "look"])
        self.assertTrue(request.done.is_set())
        self.assertEqual(machines.inspect(machine["id"])["receipt"]["status"], "sent")
        self.assertEqual(self.journal.actions[request.action["action_id"]]["origin"], "puppet")
        self.assertEqual(set(row["origin"] for row in self.journal.actions.values()),
                         {"puppet", "trigger", "machine"})
        self.assertEqual(self.journal.read(last=True)["action_id"], request.action["action_id"])
        triggers.on_disconnect()
        machines.on_disconnect()
        puppet.on_disconnect()
        self.assertFalse(self.scheduler.jobs)


if __name__ == "__main__":
    unittest.main()
