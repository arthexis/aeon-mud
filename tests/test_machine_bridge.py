"""Live machine/Puppet event bridge: observe-only or bounded single-writer execution."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from aeon_mud.machine_bridge import MachineBridge, MachineError, handle_machine_command
from aeon_mud.machines import main as machine_main
from aeon_mud.profiles import Profile
from aeon_mud.puppet import PuppetClient, PuppetServer
from aeon_mud.puppet_journal import PuppetJournal
from aeon_mud.transcript import Transcript

FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "merchant.machine.yaml"


class FakeController:
    def __init__(self, transcript):
        self.transcript = transcript
        self.profile = transcript.profile
        self.profile.paste_line_delay = 0
        self.sent = []
        self.trigger_ready = True
        self.connected = True
        self.ui = mock.Mock()

    def current_connection(self):
        return object() if self.connected else None

    def send_line(self, command, **kwargs):
        self.sent.append(command)
        self.transcript.write("send", command)


class MachineBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.patch = mock.patch("aeon_mud.puppet_journal.config_dir", return_value=self.path)
        self.patch.start()
        self.profile = Profile(name="test", host="mud.example")
        self.transcript = Transcript(self.profile)
        self.journal = PuppetJournal("Hero")
        self.transcript.observers.append(self.journal.record)
        self.now = [100.0]
        self.bridge = MachineBridge(clock=lambda: self.now[0],
                                    reaction=lambda text=None, immediate=False: 0 if immediate else .2)
        self.bridge.attach(journal=self.journal, character="Hero")
        self.bridge.load(str(FIXTURE))
        self.controller = FakeController(self.transcript)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def recv(self, text):
        event = self.transcript.write("recv", text)
        self.assertEqual(event["kind"], "event")
        self.bridge.feed(text, cursor=event["seq"])
        return event

    def send_due(self):
        self.now[0] += 0.25
        self.bridge.drain(self.controller)

    def test_send_machine_actions_and_log_cursor_correlation(self):
        started = self.bridge.start("merchant")
        run_id = started["id"]
        self.assertEqual(started["status"], "running")
        self.bridge.drain(self.controller)
        self.assertEqual(self.controller.sent, [])
        self.send_due()
        self.assertEqual(self.controller.sent, ["list"])
        event = self.recv("Welcome to my shop\n")
        self.send_due()
        self.assertEqual(self.controller.sent[-1], "value sword")
        trace = self.bridge.trace(run_id)["trace"]
        self.assertEqual(trace[1]["source_cursor"], event["seq"])
        self.assertEqual(trace[1]["status"], "sent")
        quote = self.recv("The sword costs 90 coins\n")
        self.send_due()
        self.assertEqual(self.controller.sent[-1], "buy sword")
        action = self.journal.actions[self.journal.last_action]
        self.assertEqual(action["origin"], "machine")
        self.assertEqual(action["source_cursor"], quote["seq"])
        self.assertEqual(action["source_session_id"], self.journal.session_id)
        self.assertEqual(action["machine_id"], run_id)
        self.assertEqual(action["machine_name"], "merchant")
        self.assertEqual(action["transition_id"], trace[-1]["transition_id"] if len(trace)==1 else self.bridge.trace(run_id)["trace"][-1]["transition_id"])
        self.recv("You purchase sword\n")
        self.assertEqual(self.bridge.inspect(run_id)["state"], "completed")
        self.assertEqual(self.journal.read(last=True)["events"][-1]["text"], "You purchase sword\n")

    def test_machine_observer_does_not_send(self):
        run = self.bridge.start("merchant", observe=True)
        self.assertFalse(run["writer"])
        self.bridge.drain(self.controller)
        self.recv("Welcome to my shop\nThe sword costs 25 coins\n")
        self.bridge.drain(self.controller)
        self.assertEqual(self.controller.sent, [])
        # Observers are pure and still trace state transitions.
        self.assertEqual(self.bridge.inspect(run["id"])["state"], "purchasing")
        self.assertEqual(self.bridge.trace(run["id"])["trace"][0]["status"], "observed")

    def test_only_one_writer_but_multiple_observers(self):
        first = self.bridge.start("merchant")
        with self.assertRaisesRegex(MachineError, "write control"):
            self.bridge.start("merchant")
        other = self.bridge.start("merchant", observe=True)
        self.assertFalse(other["writer"])
        self.bridge.stop(first["id"])
        self.assertTrue(self.bridge.start("merchant")["writer"])

    def test_character_required_for_writer(self):
        self.bridge.attach(journal=None, character=None)
        with self.assertRaisesRegex(MachineError, "character session"):
            self.bridge.start("merchant")
        self.assertFalse(self.bridge.start("merchant", observe=True)["writer"])

    def test_local_client_text_not_a_machine_event(self):
        run = self.bridge.start("merchant")
        self.send_due()
        self.transcript.write("recv", "[client] Welcome to my shop\n")
        self.assertEqual(self.bridge.inspect(run["id"])["state"], "idle")

    def test_received_match_requires_completed_line(self):
        run = self.bridge.start("merchant")
        self.send_due()
        self.recv("Welcome to my")
        self.assertEqual(self.bridge.inspect(run["id"])["state"], "idle")
        self.recv(" shop\n")
        self.assertEqual(self.bridge.inspect(run["id"])["state"], "awaiting_price")

    def test_observation_delay_is_based_on_actual_match(self):
        run = self.bridge.start("merchant")
        self.send_due()
        self.recv("Welcome to my shop\n")
        self.assertEqual(self.bridge.trace(run["id"])["trace"][-1]["reaction_delay_ms"], 200)

    def test_timeout_only_when_no_pending_effects(self):
        run = self.bridge.start("merchant")
        self.send_due()
        self.recv("Welcome to my shop\n")
        self.now[0] += 20
        self.bridge.drain(self.controller)  # finishes value command; only now start 10s deadline
        self.assertEqual(self.controller.sent[-1], "value sword")
        self.assertIsNotNone(self.bridge.inspect(run["id"])["deadline"])
        self.now[0] += 11
        self.bridge.drain(self.controller)  # plan timeout
        self.assertEqual(self.bridge.inspect(run["id"])["state"], "failed")
        self.bridge.drain(self.controller)  # show safe notification
        self.controller.ui.set_status.assert_called()
        self.assertEqual(self.bridge.inspect(run["id"])["status"], "completed")

    def test_stop_cancels_scheduled_before_send(self):
        run = self.bridge.start("merchant")
        self.bridge.stop(run["id"])
        self.send_due()
        self.assertEqual(self.controller.sent, [])
        self.assertEqual(self.bridge.inspect(run["id"])["status"], "stopped")
        self.assertEqual(self.bridge.trace(run["id"])["trace"][0]["status"], "cancelled")

    def test_pause_resume_and_step_exactly_one_command(self):
        run = self.bridge.start("merchant")
        self.bridge.pause(run["id"])
        self.send_due()
        self.assertEqual(self.controller.sent, [])
        self.bridge.step(run["id"])
        self.bridge.drain(self.controller)
        self.assertEqual(self.controller.sent, ["list"])
        self.assertEqual(self.bridge.inspect(run["id"])["status"], "paused")
        self.bridge.resume(run["id"])
        self.assertEqual(self.bridge.inspect(run["id"])["status"], "running")

    def test_disconnection_requires_recovery_no_replay(self):
        run = self.bridge.start("merchant")
        self.bridge.on_disconnect()
        self.controller.connected = True
        self.send_due()
        self.assertEqual(self.controller.sent, [])
        self.assertEqual(self.bridge.inspect(run["id"])["status"], "recovery_required")
        with self.assertRaisesRegex(MachineError, "explicit recovery"):
            self.bridge.resume(run["id"])

    def test_send_error_never_auto_retries(self):
        run = self.bridge.start("merchant")
        def fail(_command, **_kwargs):
            raise OSError("outcome unknown")
        self.controller.send_line = fail
        self.send_due()
        self.assertEqual(self.bridge.inspect(run["id"])["status"], "recovery_required")
        self.assertEqual(self.bridge.trace(run["id"])["trace"][0]["status"], "outcome_unknown")
        self.bridge.drain(self.controller)
        self.assertEqual(self.bridge.inspect(run["id"])["pending"], False)

    def test_read_only_status_and_trace(self):
        run = self.bridge.start("merchant")
        self.assertEqual(self.bridge.execute("status", [run["id"]])["id"], run["id"])
        self.assertEqual(self.bridge.execute("list", [])["definitions"], ["merchant"])
        self.assertTrue(self.bridge.execute("trace", [run["id"]])["trace"])
        with self.assertRaisesRegex(MachineError, "supports --observe"):
            self.bridge.execute("start", ["merchant", "--unknown"])
        with self.assertRaisesRegex(MachineError, "usage"):
            self.bridge.execute("secret", [])

    def test_cli_validation_and_local_command_share_bridge(self):
        output = handle_machine_command("//machine list", self.bridge)
        self.assertIn("merchant", output)
        self.assertIn("Machine valid: merchant", handle_machine_command(
            f"//machine validate {FIXTURE}", self.bridge))
        self.assertIn("Machine error", handle_machine_command(
            "//machine start no-such-name", self.bridge))
        result = handle_machine_command("//machine start merchant --observe", self.bridge)
        self.assertTrue(json.loads(result)["paused"] is False)
        self.assertTrue(json.loads(result)["writer"] is False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(machine_main(["validate", str(FIXTURE)]), 0)
        self.assertTrue(json.loads(output.getvalue())["valid"])

    def test_reaction_immediate_option(self):
        run = self.bridge.start("merchant", immediate=True)
        self.bridge.drain(self.controller)
        self.assertEqual(self.controller.sent, ["list"])
        self.assertEqual(self.bridge.trace(run["id"])["trace"][0]["reaction_delay_ms"], 0)


@unittest.skipUnless(os.name == "posix", "Unix socket required")
class MachineSocketTests(unittest.TestCase):
    def test_socket_machine_commands_and_read_last(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": folder}), \
                 mock.patch("aeon_mud.puppet_journal.config_dir", return_value=Path(folder)):
                profile = Profile(name="local", host="localhost")
                transcript = Transcript(profile)
                journal = PuppetJournal("Hero")
                transcript.observers.append(journal.record)
                bridge = MachineBridge(reaction=lambda text=None, immediate=False: 0)
                bridge.attach(journal=journal, character="Hero")
                server = PuppetServer("Hero")
                server.attach(transcript, journal=journal)
                server.machine_bridge = bridge
                server.start()
                class Controller(FakeController):
                    pass
                controller = Controller(transcript)
                try:
                    client = PuppetClient("Hero")
                    self.assertEqual(client.machine("load", str(FIXTURE))["name"], "merchant")
                    run = client.machine("start", "merchant", "--immediate")
                    self.assertTrue(run["writer"])
                    bridge.drain(controller)
                    self.assertEqual(controller.sent, ["list"])
                    self.assertEqual(client.machine("inspect", run["id"])["state"], "idle")
                    action = journal.actions[journal.last_action]
                    self.assertEqual(action["origin"], "machine")
                    transcript.write("recv", "Exits: north\n")
                    self.assertEqual(client.read(last=True)["events"][-1]["text"], "Exits: north\n")
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        self.assertEqual(machine_main(["Hero", "status", run["id"]]), 0)
                    self.assertEqual(json.loads(out.getvalue())["id"], run["id"])
                finally:
                    server.detach(transcript)
                    server.close()


if __name__ == "__main__":
    unittest.main()
