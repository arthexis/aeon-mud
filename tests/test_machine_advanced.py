"""Stage 6: hierarchical transitions, submachine calls, bounded retries, operators."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aeon_mud.machines import MachineInstance, MachineError, compile_machine, load_machine
from aeon_mud.machine_bridge import MachineBridge
from aeon_mud.machine_replay import mermaid
from aeon_mud.profiles import Profile
from aeon_mud.puppet_journal import PuppetJournal
from aeon_mud.transcript import Transcript

ROOT = Path(__file__).resolve().parents[1]
ADVANCED = ROOT / "examples" / "expedition.machine.yaml"


class InterpreterAdvancedTests(unittest.TestCase):
    def setUp(self):
        self.definition = load_machine(ADVANCED)
        self.machine = MachineInstance(self.definition)
        self.machine.start()

    def test_scoped_call_return_and_export(self):
        m = self.machine
        called = m.receive_line("Welcome to my shop", cursor=10)
        self.assertEqual(called.to_state, "quote__waiting")
        self.assertEqual(called.stack_depth, 1)
        self.assertEqual(m.variables["price"], "0")
        self.assertEqual(m.frames[-1]["locals"]["item"], "sword")
        returned = m.receive_line("The sword costs 75 coins", cursor=11)
        self.assertEqual(returned.from_state, "quote__waiting")
        self.assertEqual(returned.to_state, "reviewing")
        self.assertEqual(returned.stack_depth, 0)
        self.assertEqual(m.variables["price"], "75")
        self.assertNotIn("1", m.variables)
        # Original root-level match captures keep the Stage 1 behavior;
        # submachine captures never overwrite that root capture.
        self.assertEqual(m.variables["0"], "Welcome to my shop")
        self.assertEqual(m.frames, [])
        buy = m.receive_line("Confirm purchase", cursor=12)
        self.assertEqual(buy.to_state, "purchasing")
        self.assertEqual(buy.commands, ("buy sword",))
        m.receive_line("You purchase sword")
        self.assertEqual(m.status, "completed")

    def test_reusable_submachine_resets_locals_between_calls(self):
        m = self.machine
        m.receive_line("Welcome to my shop")
        m.receive_line("The sword costs 150 coins")
        self.assertEqual(m.variables["price"], "150")
        called = m.receive_line("Request a new quote")
        self.assertEqual(called.to_state, "quote__waiting")
        self.assertEqual(m.frames[-1]["locals"]["price"], "0")
        m.receive_line("The sword costs 50 coins")
        self.assertEqual(m.variables["price"], "50")
        self.assertEqual(m.frames, [])
        self.assertEqual(m.receive_line("Confirm purchase").to_state, "purchasing")

    def test_inherited_transitions_and_child_precedence(self):
        m = self.machine
        m.receive_line("Welcome to my shop")
        m.receive_line("The sword costs 60 coins")
        event = m.receive_line("A guard warns trespassers")
        self.assertEqual(event.from_state, "reviewing")
        self.assertEqual(event.transition_source, "common")
        self.assertEqual(event.to_state, "leaving")
        self.assertEqual(m.status, "completed")

    def test_global_transition_unwinds_active_submachine(self):
        m = self.machine
        m.receive_line("Welcome to my shop")
        decision = m.receive_line("You are dead")
        self.assertEqual(decision.to_state, "dead")
        self.assertEqual(decision.transition_source, "*")
        self.assertEqual(decision.stack_depth, 0)
        self.assertEqual(m.frames, [])
        self.assertEqual(m.status, "completed")

    def test_system_event_does_not_emit_commands(self):
        m = self.machine
        m.receive_line("Welcome to my shop")
        event = m.signal("disconnect")
        self.assertEqual(event.to_state, "offline")
        self.assertEqual(event.commands, ())
        self.assertEqual(m.frames, [])
        self.assertIsNone(m.signal("stop"))

    def test_retry_backoff_limit_and_final_timeout(self):
        m = self.machine
        m.receive_line("Welcome to my shop")
        first = m.timeout(cursor=20)
        self.assertEqual((first.event, first.retry_attempt, first.to_state),
                         ("retry", 1, "quote__waiting"))
        self.assertEqual(first.commands, ())
        second = m.timeout(cursor=21)
        self.assertEqual(second.retry_attempt, 2)
        third = m.timeout(cursor=22)
        self.assertEqual(third.event, "timeout")
        self.assertEqual(third.to_state, "reviewing")
        self.assertEqual(m.retry_counts.get("quote__waiting"), None)

    def test_nested_calls_bounded_to_four(self):
        raw = {
            "name": "recursion", "version": 1, "initial": "start",
            "states": {"start": {"transitions": [{
                "match": "go", "call": "child", "return_to": "start"}]}},
            "submachines": {"child": {
                "initial": "loop",
                "states": {
                    "loop": {"transitions": [{
                        "match": "go", "call": "child", "return_to": "loop"}]},
                    "end": {"final": True},
                }}}
        }
        m = MachineInstance(compile_machine(raw))
        m.start()
        for depth in range(4):
            decision = m.receive_line("go")
            self.assertEqual(decision.stack_depth, depth + 1)
        original = (m.state, copy.deepcopy(m.frames), list(m.trace))
        with self.assertRaisesRegex(MachineError, "recursion limit"):
            m.receive_line("go")
        self.assertEqual((m.state, m.frames, m.trace), original)

    def test_failed_action_rolls_back_scopes_and_stack(self):
        raw = {
            "name": "atomic", "version": 1, "initial": "idle",
            "variables": {"item": "sword"},
            "states": {
                "idle": {"transitions": [{
                    "match": "go", "call": "sub", "return_to": "done",
                    "with": {"item": "{item}"}
                }]},
                "done": {"final": True}
            },
            "submachines": {"sub": {
                "initial": "enter", "variables": {"item": ""},
                "states": {
                    "enter": {"on_enter": [{"send": "say {undefined}"}]},
                    "exit": {"final": True}
                }
            }}
        }
        m = MachineInstance(compile_machine(raw))
        m.start()
        with self.assertRaisesRegex(MachineError, "missing"):
            m.receive_line("go")
        self.assertEqual(m.state, "idle")
        self.assertEqual(m.frames, [])
        self.assertEqual(len(m.trace), 1)

    def test_advanced_schema_rejects_unsafe_structures(self):
        base = {"name": "example", "version": 1, "initial": "idle",
                "states": {"idle": {}}}
        cases = [
            {"states": {"idle": {"parent": "idle"}}},
            {"states": {"idle": {"parent": "other"}}},
            {"states": {"idle": {"parent": "other"}, "other": {"parent": "idle"}}},
            {"states": {"idle": {"retry": {"max": 100}}}},
            {"states": {"idle": {"timeout": "1s", "on_timeout": {"goto": "idle"},
                                 "on_enter": [{"send": "look"}],
                                 "retry": {"max": 2}}}},
            {"states": {"idle": {"transitions": [{
                "match": "x", "call": "unknown", "return_to": "idle"}]}}},
            {"events": {"disconnect": {"goto": "idle", "actions": [{"send": "north"}]}}},
            {"events": {"unsafe": {"goto": "idle"}}},
            {"global_transitions": [{"match": "x", "goto": "missing"}]},
            {"submachines": {"sub": {"initial": "start", "states": {"start": {}}}}},
        ]
        for item in cases:
            with self.subTest(item=item), self.assertRaises(MachineError):
                compile_machine({**base, **item})

    def test_inherited_global_and_call_mermaid_graph(self):
        doc = mermaid(self.definition)
        self.assertIn("reviewing (inherits common)", doc)
        self.assertIn("idle --> quote__waiting: call quote", doc)
        self.assertIn("quote__done --> reviewing: return", doc)
        self.assertIn("global_guard --> dead: global 1", doc)
        self.assertIn("quote__waiting --> quote__waiting: retry max 2", doc)


class AdvancedBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patcher = mock.patch("aeon_mud.puppet_journal.config_dir",
                                  return_value=self.root / "journal")
        self.patcher.start()
        self.profile = Profile(name="lab", host="mud.example")
        self.transcript = Transcript(self.profile)
        self.journal = PuppetJournal("Hero")
        self.transcript.observers.append(self.journal.record)
        self.clock = [100.0]
        self.bridge = self.build()
        self.bridge.load(str(ADVANCED))

    def build(self):
        bridge = MachineBridge(clock=lambda: self.clock[0],
                               reaction=lambda text=None, immediate=False: 0)
        bridge.attach(journal=self.journal, profile=self.profile,
                      character="Hero", store_root=self.root / "machines")
        return bridge

    def tearDown(self):
        self.bridge.close()
        self.patcher.stop()
        self.tmp.cleanup()

    def test_observers_and_diagnostics_without_dispatch(self):
        first = self.bridge.start("expedition", observe=True)
        second = self.bridge.start("expedition", observe=True)
        self.assertFalse(first["writer"])
        self.assertFalse(second["writer"])
        self.bridge.feed("Welcome to my shop\n", cursor=5)
        self.assertEqual(self.bridge.diagnose(first["id"])["stack_depth"], 1)
        self.assertEqual(len(self.bridge.diagnose()["instances"]), 2)
        self.assertEqual(self.bridge.scheduler.jobs, {})
        self.assertEqual(self.bridge.diagnose(first["id"])["timeout_seconds"], 2)

    def test_operator_variable_override_requires_pause_and_no_pending(self):
        run = self.bridge.start("expedition", observe=True)
        with self.assertRaisesRegex(MachineError, "paused"):
            self.bridge.intervene(run["id"], "max_price", "200")
        self.bridge.pause(run["id"])
        self.bridge.intervene(run["id"], "max_price", "200")
        self.assertEqual(self.bridge.inspect(run["id"])["variables"]["max_price"], "200")
        with self.assertRaisesRegex(MachineError, "existing"):
            self.bridge.intervene(run["id"], "new_variable", "x")
        self.bridge.resume(run["id"])

    def test_operator_signal_death_requires_paused_instance(self):
        run = self.bridge.start("expedition", observe=True)
        with self.assertRaisesRegex(MachineError, "paused"):
            self.bridge.signal(run["id"], "death")
        self.bridge.pause(run["id"])
        result = self.bridge.signal(run["id"], "death")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["state"], "dead")
        with self.assertRaisesRegex(MachineError, "lifecycle"):
            self.bridge.signal(run["id"], "disconnect")

    def test_persistent_call_frames_survive_without_auto_resume(self):
        run = self.bridge.start("expedition", observe=True)
        self.bridge.feed("Welcome to my shop\n", cursor=20)
        self.assertEqual(self.bridge.inspect(run["id"])["stack_depth"], 1)
        self.bridge.close()
        restored = self.build()
        self.bridge = restored
        result = restored.inspect(run["id"])
        self.assertEqual(result["status"], "recovery_required")
        self.assertEqual(result["stack_depth"], 1)
        self.assertEqual(result["frames"][0]["name"], "quote")
        self.assertEqual(restored.scheduler.jobs, {})
        restored.recover(run["id"], "skip")
        self.assertEqual(restored.inspect(run["id"])["state"], "quote__waiting")

    def test_backoff_timer_from_bridge_uses_retry_count(self):
        run = self.bridge.start("expedition", observe=True)
        self.bridge.feed("Welcome to my shop\n", cursor=21)
        state = self.bridge._get(run["id"])
        self.assertEqual(state.deadline, 102)
        class Controller:
            trigger_ready = True
            ui = mock.Mock()
            def current_connection(self): return object()
        ctl = Controller()
        self.clock[0] += 2
        self.bridge.poll_timers(ctl)
        self.assertEqual(self.bridge.inspect(run["id"])["retry_attempts"], 1)
        self.assertEqual(state.deadline, 105)
        self.clock[0] += 3
        self.bridge.poll_timers(ctl)
        self.assertEqual(self.bridge.inspect(run["id"])["retry_attempts"], 2)
        self.assertEqual(state.deadline, 109)

    def test_disconnect_cannot_send_from_global_handler(self):
        run = self.bridge.start("expedition", observe=True)
        self.bridge.feed("Welcome to my shop\n")
        self.bridge.on_disconnect()
        state = self.bridge.inspect(run["id"])
        self.assertEqual(state["status"], "recovery_required")
        self.assertEqual(state["state"], "offline")
        self.assertEqual(self.bridge.scheduler.jobs, {})


if __name__ == "__main__":
    unittest.main()
