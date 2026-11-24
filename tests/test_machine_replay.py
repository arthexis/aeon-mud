"""Stage 5: deterministic offline replay from JSONL journals and explicit text logs."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aeon_mud.machine_bridge import MachineBridge, handle_machine_command
from aeon_mud.machine_replay import mermaid, replay, local_replay
from aeon_mud.machines import MachineError, load_machine, main

ROOT = Path(__file__).resolve().parents[1]
DEFINITION = ROOT / "examples" / "merchant.machine.yaml"
EVENTS = ROOT / "examples" / "merchant.replay.jsonl"


class OfflineReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.definition = load_machine(DEFINITION)

    def tearDown(self):
        self.tmp.cleanup()

    def records(self, *rows, suffix=".jsonl"):
        path = self.root / ("events" + suffix)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def test_historical_journal_reconstructs_exact_state_and_cursors(self):
        result = replay(self.definition, EVENTS)
        self.assertEqual(result["machine"], "merchant")
        self.assertFalse(result["approximate"])
        self.assertFalse(result["effects_sent"])
        self.assertTrue(result["offline"])
        self.assertEqual(result["processed_events"], 3)
        self.assertEqual(result["received_events"], 3)
        self.assertEqual(result["session_count"], 1)
        self.assertEqual(result["transition_count"], 3)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["state"], "completed")
        self.assertEqual([row["to_state"] for row in result["trace"]],
                         ["idle", "awaiting_price", "purchasing", "completed"])
        self.assertEqual([row["cursor"] for row in result["trace"]],
                         [None, 4, 6, 6])
        self.assertEqual(result["trace"][1]["session_id"], "fixture-session")
        self.assertEqual(result["trace"][2]["transition_index"], 0)
        self.assertEqual(result["trace"][0]["command_hashes"],
                         [hashlib.sha256(b"list").hexdigest()])

    def test_replay_default_never_prints_raw_command_or_whole_server_line(self):
        report = replay(self.definition, EVENTS)
        rendered = json.dumps(report)
        self.assertNotIn('"commands":', rendered)
        self.assertNotIn('"list"', rendered)
        self.assertNotIn("Welcome to my shop", rendered)
        self.assertIn("[redacted]", rendered)  # match-group zero

    def test_mermaid_highlights_only_visited_transition_and_current_state(self):
        report = replay(self.definition, EVENTS)
        chart = mermaid(self.definition, report)
        self.assertTrue(chart.startswith("stateDiagram-v2\n"))
        self.assertIn("idle --> awaiting_price: match 1 (visited 1x)", chart)
        self.assertIn("awaiting_price --> purchasing: match 1 (visited 1x)", chart)
        self.assertIn("awaiting_price --> leaving: match 2", chart)
        self.assertNotIn("leaving: match 2 (visited", chart)
        self.assertIn("class completed replayCurrent", chart)
        self.assertNotIn("The [item] costs", chart)  # user pattern cannot escape diagram syntax
        self.assertEqual(chart, mermaid(self.definition, report))

    def test_explicit_timeout_events_work_without_simulating_wall_clock(self):
        path = self.records(
            {"kind": "event", "seq": 1, "session_id": "s", "direction": "recv",
             "text": "Welcome to my shop\n"},
            {"kind": "timeout", "seq": 2, "session_id": "s", "event": "timeout"},
        )
        report = replay(self.definition, path)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["state"], "failed")
        self.assertEqual(report["trace"][-1]["event"], "timeout")
        self.assertEqual(report["trace"][-1]["cursor"], 2)
        self.assertEqual(report["timeouts"], "explicit-events-only")
        self.assertIn("awaiting_price --> failed: timeout (visited 1x)",
                      mermaid(self.definition, report))

    def test_wall_time_gaps_do_not_invent_a_timeout(self):
        path = self.records(
            {"kind": "event", "seq": 1, "direction": "recv",
             "ts": "2026-11-24T00:00:00Z", "text": "Welcome to my shop\n"},
            {"kind": "event", "seq": 2, "direction": "recv",
             "ts": "2026-11-24T00:40:00Z", "text": "Some unrelated message\n"},
        )
        report = replay(self.definition, path)
        self.assertEqual(report["state"], "awaiting_price")
        self.assertEqual(report["status"], "running")

    def test_session_boundaries_do_not_replay_or_join_partial_lines(self):
        path = self.records(
            {"kind": "event", "session_id": "first", "seq": 1,
             "direction": "recv", "text": "Welcome to my"},
            {"kind": "event", "session_id": "second", "seq": 1,
             "direction": "recv", "text": " shop\nWelcome to my shop\n"},
        )
        report = replay(self.definition, path)
        self.assertEqual(report["session_count"], 2)
        self.assertTrue(report["stopped_at_session_boundary"])
        self.assertEqual(report["state"], "idle")
        self.assertEqual(report["status"], "recovery_required")
        self.assertEqual(report["transition_count"], 0)

    def test_local_confirmations_and_sent_commands_never_trigger_transitions(self):
        path = self.records(
            {"direction": "recv", "text": "[client] Welcome to my shop\n"},
            {"direction": "send", "text": "Welcome to my shop\n"},
            {"direction": "client", "text": "Welcome to my shop\n"},
            {"direction": "recv", "secret": True, "text": "Welcome to my shop\n"},
        )
        report = replay(self.definition, path)
        self.assertEqual(report["state"], "idle")
        self.assertEqual(report["transition_count"], 0)

    def test_text_log_requires_explicit_format_and_is_marked_approximate(self):
        path = self.root / "history.log"
        path.write_text("\n--- log started 2026-11-24T00:00:00+00:00 ---\n"
                        "> Welcome to my shop\n[client] Welcome to my shop\n"
                        "Welcome to my shop\nThe sword costs 150 coins\n",
                        encoding="utf-8")
        with self.assertRaisesRegex(MachineError, "ambiguous"):
            replay(self.definition, path)
        result = replay(self.definition, path, format="text")
        self.assertTrue(result["approximate"])
        self.assertEqual(result["state"], "leaving")
        self.assertEqual(result["transition_count"], 2)

    def test_replay_never_uses_live_scheduler_or_machine_persistence(self):
        bridge = MachineBridge()
        bridge.load(str(DEFINITION))
        with mock.patch.object(bridge.scheduler, "enqueue",
                               side_effect=AssertionError("live send scheduled")), \
             mock.patch("aeon_mud.machine_persistence.MachineStore.commit",
                        side_effect=AssertionError("storage modified")):
            result = local_replay([str(EVENTS)], bridge=bridge)
            self.assertEqual(json.loads(result)["state"], "completed")
            chart = local_replay([str(EVENTS), "--diagram"], bridge=bridge)
            self.assertIn("stateDiagram-v2", chart)
        self.assertEqual(bridge.runs, {})
        self.assertEqual(bridge.scheduler.jobs, {})

    def test_local_replay_accepts_explicit_definition_without_bridge(self):
        msg = handle_machine_command(f"//machine replay {DEFINITION} {EVENTS}", None)
        self.assertEqual(json.loads(msg)["state"], "completed")
        with self.assertRaisesRegex(MachineError, "needs MACHINE"):
            local_replay([str(EVENTS)], bridge=None)

    def test_cli_json_and_diagram_modes_do_not_require_a_character_socket(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["replay", str(DEFINITION), str(EVENTS)]), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["state"], "completed")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["replay", str(DEFINITION), str(EVENTS), "--diagram"]), 0)
        self.assertIn("visited 1x", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["diagram", str(DEFINITION)]), 0)
        self.assertIn("stateDiagram-v2", out.getvalue())
        self.assertNotIn("replayCurrent", out.getvalue())

    def test_bad_and_oversized_input_fails_closed(self):
        bad = self.root / "bad.jsonl"
        for raw in ('{"kind":"unexpected"}\n', '{broken\n',
                    '{"kind":"event","direction":"recv","text":45}\n',
                    '{"kind":"event","direction":"recv","text":"yes","seq":true}\n'):
            bad.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(MachineError):
                replay(self.definition, bad)
        bad.write_text("{}\n" * 100)
        with self.assertRaisesRegex(MachineError, "direction"):
            replay(self.definition, bad)
        bad.write_bytes(b"0" * (8 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(MachineError, "8 MiB"):
            replay(self.definition, bad)

    def test_duplicate_out_of_order_cursors_rejected(self):
        path = self.records(
            {"direction": "recv", "seq": 8, "text": "irrelevant\n"},
            {"direction": "recv", "seq": 8, "text": "Welcome to my shop\n"},
        )
        with self.assertRaisesRegex(MachineError, "not increasing"):
            replay(self.definition, path)

    def test_nonconsecutive_session_segments_rejected(self):
        path = self.records(
            {"direction": "recv", "session_id": "a", "text": "x\n"},
            {"direction": "recv", "session_id": "b", "text": "x\n"},
            {"direction": "recv", "session_id": "a", "text": "x\n"},
        )
        with self.assertRaisesRegex(MachineError, "repeated session"):
            replay(self.definition, path)

    def test_symlinks_and_encrypted_logs_are_rejected(self):
        bad = self.root / "log.dpapi.jsonl"
        bad.write_text("encrypted\n")
        with self.assertRaisesRegex(MachineError, "encrypted"):
            replay(self.definition, bad)
        alias = self.root / "alias.jsonl"
        try:
            alias.symlink_to(EVENTS)
        except (OSError, NotImplementedError):
            self.skipTest("symlink unsupported")
        with self.assertRaisesRegex(MachineError, "symlinks"):
            replay(self.definition, alias)

    def test_invalid_local_flags_and_missing_definition_rejected(self):
        with self.assertRaisesRegex(MachineError, "option"):
            local_replay([str(DEFINITION), str(EVENTS), "--write"])
        with self.assertRaisesRegex(MachineError, "one loaded"):
            local_replay([str(EVENTS)])
        self.assertIn("Machine error", handle_machine_command(
            "//machine replay /does-not-exist.yaml /does-not-exist.jsonl", None))


if __name__ == "__main__":
    unittest.main()
