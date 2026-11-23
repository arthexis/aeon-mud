"""Stage 1 machine contract: strict YAML, safe guards, deterministic pure transitions."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aeon_mud.machines import (
    MachineError, MachineInstance, compile_condition, compile_machine,
    evaluate_condition, expand, load_machine, main,
)
from aeon_mud.commands import handle_local_command

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples" / "merchant.machine.yaml"


class MachineDefinitionTests(unittest.TestCase):
    def setUp(self):
        self.definition = load_machine(FIXTURE)

    def test_merchant_schema_and_reference_transitions(self):
        self.assertEqual(self.definition.name, "merchant")
        self.assertEqual(self.definition.initial, "idle")
        self.assertEqual(self.definition.states["awaiting_price"].timeout, 10)
        self.assertEqual(len(self.definition.states), 6)
        self.assertTrue(self.definition.states["completed"].final)

    def test_successful_purchase_is_deterministic_and_pure(self):
        instance = MachineInstance(self.definition)
        self.assertEqual(instance.status, "created")
        start = instance.start()
        self.assertEqual(start.commands, ("list",))
        self.assertEqual(instance.state, "idle")
        welcome = instance.receive_line("Welcome to my shop\n".strip(), cursor=7)
        self.assertEqual(welcome.from_state, "idle")
        self.assertEqual(welcome.to_state, "awaiting_price")
        self.assertEqual(welcome.commands, ("value sword",))
        self.assertEqual(welcome.cursor, 7)
        quote = instance.receive_line("The sword costs 75 coins", cursor=11)
        self.assertEqual(quote.to_state, "purchasing")
        self.assertEqual(quote.commands, ("buy sword",))
        self.assertEqual(quote.captures["price"], "75")
        self.assertEqual(quote.variables["price"], "75")
        done = instance.receive_line("You purchase sword", cursor=12)
        self.assertEqual(done.to_state, "completed")
        self.assertEqual(instance.status, "completed")
        self.assertIsNone(instance.receive_line("You purchase sword"))
        self.assertEqual(len(instance.trace), 4)

    def test_expensive_quote_uses_ordered_fallback(self):
        instance = MachineInstance(self.definition)
        instance.start()
        instance.receive_line("Welcome to my shop")
        quote = instance.receive_line("The sword costs 150 coins")
        self.assertEqual(quote.to_state, "leaving")
        self.assertEqual(quote.commands, ("say Too expensive", "leave"))
        self.assertEqual(instance.status, "completed")

    def test_explicit_timeout_transition_and_notification(self):
        instance = MachineInstance(self.definition)
        instance.start()
        instance.receive_line("Welcome to my shop")
        expired = instance.timeout(cursor=25)
        self.assertEqual(expired.to_state, "failed")
        self.assertEqual(expired.event, "timeout")
        self.assertEqual(expired.notifications, ("Merchant did not respond",))
        self.assertEqual(expired.cursor, 25)
        self.assertIsNone(instance.timeout())

    def test_fragmented_received_lines_only(self):
        instance = MachineInstance(self.definition)
        instance.start()
        self.assertEqual(instance.feed("Welcome to"), [])
        decisions = instance.feed(" my shop\nThe sword costs 50 co")
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].to_state, "awaiting_price")
        decisions = instance.feed("ins\n")
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].to_state, "purchasing")

    def test_match_anchored_by_default_and_search_opt_in(self):
        instance = MachineInstance(self.definition)
        instance.start()
        self.assertIsNone(instance.receive_line("NEWS: Welcome to my shop"))
        raw = {
            "name": "watcher", "version": 1, "initial": "start",
            "states": {"start": {"transitions": [
                {"match": "dragon", "search": True, "goto": "done"}]},
                "done": {"final": True}}
        }
        scanned = MachineInstance(compile_machine(raw))
        scanned.start()
        self.assertEqual(scanned.receive_line("The dragon arrives").to_state, "done")

    def test_regex_captures_and_template_substitution(self):
        raw = {
            "name": "regex", "version": 1, "initial": "start",
            "states": {"start": {"transitions": [
                {"match": r"(?P<person>\w+) offers (\d+) coins", "re": True,
                 "goto": "done", "actions": [{"send": "thank {person} for {2} coins"}]}]},
                "done": {"final": True}}
        }
        instance = MachineInstance(compile_machine(raw))
        instance.start()
        decision = instance.receive_line("Gandalf offers 25 coins")
        self.assertEqual(decision.commands, ("thank Gandalf for 25 coins",))
        self.assertEqual(decision.captures["1"], "Gandalf")

    def test_set_action_and_no_recursive_capture_expansion(self):
        raw = {
            "name": "setvar", "version": 1, "initial": "start",
            "states": {"start": {"transitions": [
                {"match": "Gift [who]", "goto": "done", "actions": [
                    {"set": {"last": "{who}"}}, {"send": "thank {last}"}]}]},
                "done": {"final": True}}
        }
        instance = MachineInstance(compile_machine(raw))
        instance.start()
        decision = instance.receive_line("Gift {some_other_var}")
        self.assertEqual(decision.commands, ("thank {some_other_var}",))
        self.assertEqual(instance.variables["last"], "{some_other_var}")

    def test_action_errors_leave_state_unchanged(self):
        raw = {
            "name": "failedtemplate", "version": 1, "initial": "start",
            "states": {"start": {"transitions": [
                {"match": "Gift [who]", "goto": "done",
                 "actions": [{"send": "{missing}"}]}]},
                "done": {"final": True}}
        }
        instance = MachineInstance(compile_machine(raw))
        instance.start()
        with self.assertRaisesRegex(MachineError, "missing"):
            instance.receive_line("Gift Gandalf")
        self.assertEqual(instance.state, "start")
        self.assertEqual(len(instance.trace), 1)

    def test_no_repeated_starts_or_side_effects(self):
        instance = MachineInstance(self.definition)
        instance.start()
        with self.assertRaisesRegex(MachineError, "already"):
            instance.start()
        self.assertEqual(instance.state, "idle")
        self.assertEqual(len(instance.trace), 1)

    def test_reject_untrusted_expression_code(self):
        for code in (
            "__import__('os').system('id')", "price.__class__",
            "[price for _ in range(10)]", "int(price, 0)",
            "price + 5", "a[0]", "a or (lambda: True)()", "__secret",
        ):
            with self.subTest(code=code):
                with self.assertRaises(MachineError):
                    compile_condition(code)
        good = compile_condition("int(price) <= max_price and not blocked")
        self.assertTrue(evaluate_condition(good, {
            "price": "15", "max_price": 20, "blocked": False,
        }))
        self.assertFalse(evaluate_condition(good, {
            "price": "25", "max_price": 20, "blocked": False,
        }))
        with self.assertRaisesRegex(MachineError, "conversion"):
            evaluate_condition(good, {"price": "oops", "max_price": 20, "blocked": False})

    def test_reject_unsafe_send_expansion(self):
        with self.assertRaisesRegex(MachineError, "local"):
            expand("{value}", {"value": "//log --off"}, send=True)
        with self.assertRaisesRegex(MachineError, "one line"):
            expand("{value}", {"value": "north\nsouth"}, send=True)
        with self.assertRaisesRegex(MachineError, "missing"):
            expand("say {missing}", {}, send=True)

    def test_reject_unknown_versions_states_actions_and_timeouts(self):
        base = {"name": "simple", "version": 1, "initial": "start",
                "states": {"start": {"final": True}}}
        for change in (
            {"version": 2},
            {"unapproved": True},
            {"initial": "unreachable"},
            {"states": {"start": {"transitions": [{"match": "x", "goto": "gone"}]}}},
            {"states": {"start": {"on_enter": [{"shell": "rm -rf /"}]}}},
            {"states": {"start": {"timeout": "5s"}}},
            {"states": {"start": {"final": True, "transitions": [{"match": "x", "goto": "start"}]}}},
        ):
            with self.subTest(change=change):
                with self.assertRaises(MachineError):
                    compile_machine({**base, **change})

    def test_reject_nonfinite_variables_and_oversized_definitions(self):
        base = {"name": "simple", "version": 1, "initial": "start",
                "states": {"start": {"final": True}}}
        with self.assertRaises(MachineError):
            compile_machine({**base, "variables": {"x": float("inf")}})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "large.yaml"
            path.write_text(" " * 65537)
            with self.assertRaisesRegex(MachineError, "64 KiB"):
                load_machine(path)

    def test_safe_yaml_rejects_python_tags_aliases_and_duplicate_keys(self):
        samples = (
            "!!python/object/apply:os.system ['echo unsafe']",
            "name: test\nversion: 1\ninitial: start\nvariables: &x {name: A}\nstates: {start: *x}\n",
            "name: test\nname: duplicate\nversion: 1\ninitial: start\nstates: {start: {final: true}}\n",
        )
        with tempfile.TemporaryDirectory() as folder:
            for index, source in enumerate(samples):
                path = Path(folder) / f"invalid-{index}.yaml"
                path.write_text(source, encoding="utf-8")
                with self.subTest(index=index), self.assertRaises(MachineError):
                    load_machine(path)

    def test_validate_from_module_and_local_client_command(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["validate", str(FIXTURE)]), 0)
        self.assertTrue(json.loads(output.getvalue())["valid"])
        ui = mock.Mock()
        transcript = mock.Mock()
        self.assertTrue(handle_local_command(f"//machine validate {FIXTURE}", transcript, ui))
        ui.set_status.assert_called()
        self.assertIn("merchant", str(ui.set_status.call_args))


if __name__ == "__main__":
    unittest.main()
