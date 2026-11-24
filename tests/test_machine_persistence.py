"""Stage 4: crash-safe machine snapshots, decision ledger and operator recovery."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from aeon_mud.machine_bridge import MachineBridge, MachineError
from aeon_mud.machine_persistence import MachineStore, DurableMachineError, REDACTED
from aeon_mud.profiles import Profile
from aeon_mud.puppet_journal import PuppetJournal
from aeon_mud.transcript import Transcript

FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "merchant.machine.yaml"


class Controller:
    def __init__(self, transcript):
        self.transcript = transcript
        self.profile = transcript.profile
        self.profile.paste_line_delay = 0
        self.connected = True
        self.trigger_ready = True
        self.sent = []
        self.ui = mock.Mock()

    def current_connection(self):
        return object() if self.connected else None

    def send_line(self, command, **kwargs):
        self.sent.append(command)
        self.transcript.write("send", command)


class DurableMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = mock.patch("aeon_mud.puppet_journal.config_dir",
                                return_value=self.root / "journals")
        self.patch.start()
        self.profile = Profile(name="lab", host="mud.example")
        self.transcript = Transcript(self.profile)
        self.journal = PuppetJournal("Hero")
        self.transcript.observers.append(self.journal.record)
        self.controller = Controller(self.transcript)
        self.time = [100.0]
        self.bridge = self.new_bridge()
        self.bridge.load(str(FIXTURE))

    def tearDown(self):
        self.bridge.close()
        self.patch.stop()
        self.tmp.cleanup()

    def new_bridge(self, *, character="Hero", profile=None):
        bridge = MachineBridge(clock=lambda: self.time[0],
                               reaction=lambda text=None, immediate=False: 0 if immediate else .2)
        bridge.attach(journal=self.journal, profile=profile or self.profile,
                      character=character, store_root=self.root / "machine")
        return bridge

    def restarted(self):
        self.bridge.close()     # crash: no on_disconnect(), pending remains on old object
        next_bridge = self.new_bridge()
        self.bridge = next_bridge
        return next_bridge

    def test_scoped_owner_only_store_and_checksums(self):
        path = self.bridge.store.path
        ledger = self.bridge.store.ledger
        self.assertTrue(path.is_file())
        self.assertTrue(ledger.is_file())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(ledger.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        record = json.loads(ledger.read_text().splitlines()[-1])
        snap = record["snapshot"]
        self.assertEqual(record["checksum"],
                         hashlib.sha256(json.dumps(
                             {k: v for k, v in record.items() if k != "checksum"},
                             sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                             allow_nan=False).encode("utf-8")).hexdigest())
        self.assertEqual(record["reason"], "definition_loaded")

    def test_restart_requires_operator_skip_and_no_auto_send(self):
        run = self.bridge.start("merchant")
        self.assertTrue(run["pending"])
        self.restarted()
        result = self.bridge.status(run["id"])
        self.assertEqual(result["status"], "recovery_required")
        self.assertEqual(self.bridge.inspect(run["id"])["recovery_action"] is not None, True)
        self.bridge.drain(self.controller)
        self.assertEqual(self.controller.sent, [])
        with self.assertRaisesRegex(MachineError, "write control"):
            self.bridge.start("merchant")
        done = self.bridge.recover(run["id"], "skip")
        self.assertEqual(done["status"], "running")
        self.assertFalse(done["pending"])
        self.bridge.drain(self.controller)
        self.assertEqual(self.controller.sent, [])

    def test_retry_requires_exact_commands_and_explicit_direction(self):
        run = self.bridge.start("merchant")
        self.restarted()
        with self.assertRaisesRegex(MachineError, "requires all exact"):
            self.bridge.recover(run["id"], "retry", commands=["north"])
        with self.assertRaisesRegex(MachineError, "requires all exact"):
            self.bridge.recover(run["id"], "retry")
        resumed = self.bridge.recover(run["id"], "retry",
                                      commands=["list"], immediate=True)
        self.assertEqual(resumed["status"], "running")
        self.bridge.drain(self.controller)
        self.assertEqual(self.controller.sent, ["list"])
        self.assertEqual(self.bridge.trace(run["id"])["trace"][-1]["status"], "sent")

    def test_dispatch_attempt_durable_before_actual_socket_send(self):
        run = self.bridge.start("merchant", immediate=True)
        checked = []
        original = self.controller.send_line
        def send(command, **kwargs):
            row = json.loads(self.bridge.store.ledger.read_text().splitlines()[-1])
            checked.append((row["reason"], row["snapshot"]["runs"][0]["pending"]["status"]))
            original(command, **kwargs)
        self.controller.send_line = send
        self.bridge.drain(self.controller)
        self.assertEqual(checked, [("dispatch_attempt", "dispatching")])
        latest = json.loads(self.bridge.store.ledger.read_text().splitlines()[-1])
        self.assertEqual(latest["reason"], "dispatch_result")
        self.assertIsNone(latest["snapshot"]["runs"][0]["pending"])
        self.assertEqual(self.bridge.inspect(run["id"])["receipt"]["status"], "sent")

    def test_crash_between_attempt_and_receipt_demands_risk_ack(self):
        run = self.bridge.start("merchant", immediate=True)
        self.controller.send_line = mock.Mock(side_effect=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.bridge.drain(self.controller)
        self.restarted()
        self.assertEqual(self.bridge.status(run["id"])["status"], "recovery_required")
        self.assertEqual(self.bridge.trace(run["id"])["trace"][-1]["status"], "outcome_unknown")
        with self.assertRaisesRegex(MachineError, "accept-duplicate-risk"):
            self.bridge.execute("recover", [run["id"], "retry", "--send", "list"])
        self.bridge.execute("recover", [
            run["id"], "retry", "--send", "list", "--accept-duplicate-risk", "--immediate"])
        self.controller.send_line = Controller.send_line.__get__(self.controller, Controller)
        self.bridge.drain(self.controller)
        self.assertEqual(self.controller.sent, ["list"])

    def test_confirmed_send_failure_never_retries_after_restart(self):
        run = self.bridge.start("merchant", immediate=True)
        self.controller.send_line = mock.Mock(side_effect=OSError("broken pipe"))
        self.bridge.drain(self.controller)
        self.assertEqual(self.bridge.status(run["id"])["status"], "recovery_required")
        self.restarted()
        self.bridge.drain(self.controller)
        self.controller.send_line.assert_called_once_with("list", secret=False, remember=False)
        self.bridge.recover(run["id"], "abort")
        self.assertEqual(self.bridge.status(run["id"])["status"], "stopped")
        self.restarted()
        self.assertEqual(self.bridge.status(run["id"])["status"], "stopped")

    def test_persistent_transition_cursor_and_log_switch(self):
        self.bridge.start("merchant", immediate=True)
        log_a = self.root / "log-A.txt"
        log_b = self.root / "log-B.txt"
        self.transcript.enable_text_log(str(log_a))
        self.bridge.drain(self.controller)
        self.transcript.enable_text_log(str(log_b))
        event = self.transcript.write("recv", "Welcome to my shop\n")
        self.bridge.feed("Welcome to my shop\n", cursor=event["seq"])
        before = self.bridge.trace()["trace"][-1]
        self.assertEqual(before["source_cursor"], event["seq"])
        self.time[0] += 1
        self.bridge.drain(self.controller)
        self.assertEqual(self.controller.sent[-1], "value sword")
        action = self.journal.actions[self.journal.last_action]
        self.assertEqual(action["origin"], "machine")
        self.assertEqual(action["source_cursor"], event["seq"])
        self.assertEqual(action["log_path"], str(log_b))
        self.assertTrue(log_a.exists())
        self.assertTrue(log_b.exists())
        self.restarted()
        self.assertEqual(self.bridge.trace()["trace"][-1]["source_cursor"], event["seq"])

    def test_redaction_omits_original_command_bodies_and_sensitive_values(self):
        run = self.bridge.start("merchant", immediate=True)
        with self.bridge.lock:
            instance = self.bridge._get(run["id"])
            instance.machine.variables["password"] = "ULTRA_PRIVATE_VALUE"
            self.bridge._persist("test_redaction")
        raw = self.bridge.store.ledger.read_text()
        self.assertNotIn("ULTRA_PRIVATE_VALUE", raw)
        self.assertIn(REDACTED, raw)
        self.assertNotIn('"commands":["list"]', raw)
        self.bridge.drain(self.controller)
        action = self.journal.actions[self.journal.last_action]
        self.assertTrue(action["commands"][0].startswith("sha256:"))
        self.restarted()
        with self.assertRaisesRegex(MachineError, "redacted/truncated"):
            self.bridge.recover(run["id"], "skip")
        self.assertEqual(self.bridge.recover(run["id"], "abort")["status"], "stopped")

    def test_definition_tampering_prevents_restart_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / "merchant.yaml"
            copy.write_bytes(FIXTURE.read_bytes())
            self.bridge.load(str(copy))
            run = self.bridge.start("merchant")
            copy.write_text("name: corrupted\n", encoding="utf-8")
            self.restarted()
            self.assertEqual(self.bridge.status(run["id"])["status"], "recovery_required")
            with self.assertRaisesRegex(MachineError, "only abort"):
                self.bridge.recover(run["id"], "skip")
            self.bridge.recover(run["id"], "abort")

    def test_crash_after_clean_dispatch_still_needs_recovery(self):
        run = self.bridge.start("merchant", immediate=True)
        self.bridge.drain(self.controller)
        self.assertEqual(self.bridge.status(run["id"])["status"], "running")
        self.restarted()
        self.assertEqual(self.bridge.status(run["id"])["status"], "recovery_required")
        with self.assertRaisesRegex(MachineError, "no persisted pending"):
            self.bridge.recover(run["id"], "retry", commands=["list"])
        self.assertEqual(self.bridge.recover(run["id"], "skip")["status"], "running")

    def test_torn_tail_is_healed_using_verified_ledger_prefix(self):
        store = self.bridge.store
        store.ledger.write_bytes(store.ledger.read_bytes() + b'{"incomplete":')
        self.restarted()
        self.assertEqual(self.bridge.list()["definitions"], ["merchant"])
        self.assertTrue(self.bridge.store.ledger.read_bytes().endswith(b"\n"))
        self.restarted()
        self.assertEqual(self.bridge.list()["definitions"], ["merchant"])

    def test_checksum_tampering_fails_closed(self):
        store = self.bridge.store
        raw = store.ledger.read_text()
        self.assertIn('"definitions":', raw)
        store.ledger.write_text(raw.replace('"definitions":', '"changed_defs":'))
        self.bridge.close()
        with self.assertRaises(DurableMachineError):
            self.new_bridge()

    def test_scope_isolation_and_exclusive_lock(self):
        with self.assertRaises(DurableMachineError):
            self.new_bridge()
        other = self.new_bridge(character="Other")
        try:
            self.assertEqual(other.list()["definitions"], [])
        finally:
            other.close()

    @unittest.skipUnless(os.name == "posix", "symlink protection on POSIX")
    def test_symlinked_ledger_is_refused(self):
        store = self.bridge.store
        store.close()
        store.ledger.unlink()
        outside = self.root / "outside"
        outside.write_text("not ours\n")
        store.ledger.symlink_to(outside)
        with self.assertRaises(DurableMachineError):
            self.new_bridge()
        self.assertEqual(outside.read_text(), "not ours\n")
        store.ledger.unlink()

    def test_checkpoint_fsync_failure_blocks_unsent_commands(self):
        run = self.bridge.start("merchant", immediate=True)
        original = self.bridge.store.commit
        def fail(snapshot, *, reason):
            if reason == "dispatch_attempt":
                raise OSError("storage offline")
            return original(snapshot, reason=reason)
        with mock.patch.object(self.bridge.store, "commit", side_effect=fail):
            self.bridge.drain(self.controller)
        self.assertEqual(self.controller.sent, [])
        self.assertEqual(self.bridge.status(run["id"])["status"], "recovery_required")

    def test_operator_recovery_is_exposed_through_command_contract(self):
        run = self.bridge.start("merchant")
        self.restarted()
        result = self.bridge.execute("recover", [run["id"], "skip"])
        self.assertEqual(result["status"], "running")
        with self.assertRaisesRegex(MachineError, "not awaiting"):
            self.bridge.execute("recover", [run["id"], "abort"])


    def test_failed_checkpoint_mirror_uses_verified_ledger_after_restart(self):
        run = self.bridge.start("merchant")
        original = os.replace
        def mirror_fault(source, target):
            if str(target) == str(self.bridge.store.path):
                raise OSError("simulated power loss before mirror replace")
            return original(source, target)
        with mock.patch("aeon_mud.machine_persistence.os.replace", side_effect=mirror_fault):
            with self.assertRaisesRegex(MachineError, "checkpoint"):
                self.bridge.pause(run["id"])
        self.assertEqual(self.bridge.status(run["id"])["status"], "recovery_required")
        self.restarted()
        self.assertEqual(self.bridge.status(run["id"])["status"], "recovery_required")
        self.bridge.drain(self.controller)
        self.assertEqual(self.controller.sent, [])

    def test_failed_resume_checkpoint_disables_pending_machine_commands(self):
        run = self.bridge.start("merchant")
        self.bridge.pause(run["id"])
        original = self.bridge.store.commit
        def fail(snapshot, *, reason):
            if reason == "resumed":
                raise OSError("read-only volume")
            return original(snapshot, reason=reason)
        with mock.patch.object(self.bridge.store, "commit", side_effect=fail):
            with self.assertRaisesRegex(MachineError, "checkpoint"):
                self.bridge.resume(run["id"])
        self.assertEqual(self.bridge.status(run["id"])["status"], "recovery_required")
        self.time[0] += 10
        self.bridge.drain(self.controller)
        self.assertEqual(self.controller.sent, [])
        self.assertEqual(len(self.bridge.scheduler.jobs), 0)

    def test_aborted_missing_definition_remains_stopped_across_restarts(self):
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / "merchant.yaml"
            copy.write_bytes(FIXTURE.read_bytes())
            self.bridge.load(str(copy))
            run = self.bridge.start("merchant")
            copy.unlink()
            self.restarted()
            self.assertEqual(self.bridge.status(run["id"])["status"], "recovery_required")
            self.bridge.recover(run["id"], "abort")
            self.restarted()
            self.assertEqual(self.bridge.status(run["id"])["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
