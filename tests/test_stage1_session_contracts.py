"""Deterministic session and persistence reference contracts for Rust migration.

All transports, clocks, temporary files and credentials are synthetic.
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from aeon_mud import profiles, session, telnet


ROOT = Path(__file__).resolve().parents[1] / "contracts" / "v1"


def contract_rows(filename: str, columns: int):
    seen = set()
    for line in (ROOT / filename).read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != columns:
            raise AssertionError(f"{filename}: expected {columns} columns: {line!r}")
        if parts[0] in seen:
            raise AssertionError(f"{filename}: duplicate case {parts[0]}")
        seen.add(parts[0])
        yield parts


def values(field: str, separator: str):
    return [] if field == "-" else field.split(separator)


class StageOneSessionContracts(unittest.TestCase):
    def test_reconnect_delay_and_attempt_sequence(self):
        cases = list(contract_rows("reconnect.tsv", 7))
        self.assertGreaterEqual(len(cases), 6)
        for name, enabled, initial, maximum, failures, expected_delays, attempts in cases:
            with self.subTest(name=name):
                profile = profiles.Profile(
                    name="synthetic",
                    host="127.0.0.1",
                    reconnect=(enabled == "true"),
                    reconnect_initial_delay=float(initial),
                    reconnect_max_delay=float(maximum),
                )
                ui = mock.Mock()
                transcript = mock.Mock()
                controller = session.SessionController(
                    profile, None, telnet.TelnetCodec(), transcript, ui, None
                )
                fresh_transport = mock.Mock()
                failures_count = int(failures)
                open_results = [OSError("synthetic connection refused")] * failures_count
                open_results.append((fresh_transport, telnet.TelnetCodec(), "ready"))
                delays = []

                def record_delay(_stop, seconds):
                    delays.append(seconds)
                    return False

                with (
                    mock.patch.object(session, "open_session_transport", side_effect=open_results) as open_transport,
                    mock.patch.object(session, "run_sanity_checks"),
                    mock.patch.object(session, "sleep_until_stop", side_effect=record_delay),
                    mock.patch.object(session, "append_client_event"),
                    mock.patch.object(session, "connection_target_label", return_value="127.0.0.1:4000"),
                ):
                    result = session.reconnect_session(
                        threading.Event(), controller, transcript, "synthetic disconnect"
                    )
                self.assertEqual(result, profile.reconnect)
                self.assertEqual(open_transport.call_count, int(attempts))
                self.assertEqual(delays, [float(v) for v in values(expected_delays, ",")])
                if result:
                    self.assertIs(controller.current_connection()[0], fresh_transport)
                    self.assertTrue(controller.ready_for_commands.is_set())
                controller.close()

    def test_automatic_login_and_character_override_order(self):
        cases = list(contract_rows("login.tsv", 5))
        self.assertGreaterEqual(len(cases), 6)
        for name, enabled, character_login, chunks, expected in cases:
            with self.subTest(name=name):
                profile = profiles.Profile(
                    name="synthetic",
                    host="localhost",
                    auto_login=(enabled == "true"),
                    username="alice",
                )
                controller = session.SessionController(
                    profile, mock.Mock(), telnet.TelnetCodec(), mock.Mock(), mock.Mock(),
                    "synthetic-password",
                )
                if character_login == "true":
                    controller.character_login = ("Hero", "synthetic-password")
                sends = []
                def fake_send(line, secret=False, remember=True, **_kwargs):
                    sends.append(f"{line}:{'secret' if secret else 'plain'}")
                    if secret:
                        self.assertFalse(remember)

                with mock.patch.object(controller, "send_line", side_effect=fake_send):
                    for text in chunks.split(";"):
                        controller.observe_output(text)
                self.assertEqual(sends, values(expected, ","))

    def test_profile_history_roundtrip_and_no_read_mutation(self):
        cases = list(contract_rows("persistence.tsv", 7))
        self.assertGreaterEqual(len(cases), 5)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for name, profile_name, host, port, characters, history, expected in cases:
                with self.subTest(name=name):
                    path = base / (name + "-profiles.json")
                    hist_path = base / (name + "-history.json")
                    store = profiles.ProfileStore(path)
                    profile = profiles.Profile(
                        name=profile_name, host=host, port=int(port),
                        characters=values(characters, ";"),
                    )
                    store.set(profile)
                    original_bytes = path.read_bytes()
                    restored = profiles.ProfileStore(path).get(profile_name)
                    self.assertEqual(restored.host, host)
                    self.assertEqual(restored.port, int(port))
                    self.assertEqual(restored.characters, values(characters, ";"))
                    self.assertEqual(path.read_bytes(), original_bytes)
                    self.assertNotIn(b"password", original_bytes.lower())
                    history_store = profiles.HistoryStore(hist_path)
                    for command in values(history, ";"):
                        history_store.add(profile, command)
                    self.assertEqual(
                        profiles.HistoryStore(hist_path).list_for(restored),
                        values(expected, ";"),
                    )
                    saved_bytes = hist_path.read_bytes() if hist_path.exists() else None
                    profiles.HistoryStore(hist_path)
                    self.assertEqual(
                        hist_path.read_bytes() if hist_path.exists() else None, saved_bytes
                    )


if __name__ == "__main__":
    unittest.main()
