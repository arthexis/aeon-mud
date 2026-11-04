"""Final stage-1 executable Python reference contracts (synthetic only).

All cases are deterministic: no external network, actual passwords, or timers.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from aeon_mud import commands, config, profiles, session, telnet
from aeon_mud.input import Key


FIXTURES = Path(__file__).resolve().parents[1] / "contracts" / "v1"


def rows(filename, count):
    found = set()
    for text in (FIXTURES / filename).read_text(encoding="utf-8").splitlines():
        if not text or text.startswith("#"):
            continue
        parts = text.split("\t")
        if len(parts) != count:
            raise AssertionError(f"{filename}: expected {count} fields, got {parts!r}")
        if parts[0] in found:
            raise AssertionError(f"{filename}: duplicate case {parts[0]}")
        found.add(parts[0])
        yield parts


class FinalStageOneContracts(unittest.TestCase):
    def controller(self, current=None):
        transport = current if current is not None else mock.Mock(name="transport")
        ui = mock.Mock()
        ui.input_buffer = "pre"
        transcript = mock.Mock()
        controller = session.SessionController(
            profiles.Profile(name="synthetic", host="127.0.0.1"),
            transport, telnet.TelnetCodec(), transcript, ui, None,
        )
        return controller, ui, transcript, transport

    def test_connection_generation_races(self):
        cases = list(rows("connection_races.tsv", 5))
        self.assertGreaterEqual(len(cases), 6)
        for name, action, expected, old_closed, current in cases:
            with self.subTest(name=name):
                controller, ui, transcript, old = self.controller()
                new = mock.Mock(name="new")
                if action != "stop_before_retry":
                    controller.install_connection(new, telnet.TelnetCodec())
                try:
                    if action == "stale_disconnect":
                        result = str(controller.mark_disconnected(old, "stale"))
                    elif action == "current_disconnect":
                        result = str(controller.mark_disconnected(new, "current"))
                    elif action in {"stale_send", "current_send"}:
                        try:
                            controller.send_raw(b"test", expected_transport=old if action == "stale_send" else new)
                            result = "ok"
                        except session.ConnectionChanged:
                            result = "ConnectionChanged"
                    elif action == "login_worker_stale":
                        controller.character_login = ("Hero", "synthetic-password")
                        # Reconnect happens immediately after the worker captured the old transport.
                        with mock.patch.object(controller, "send_character_login", side_effect=session.ConnectionChanged("generation changed")) as send:
                            session.send_character_login_after_ready(controller)
                            self.assertEqual(send.call_count, 1)
                        result = "no_send"
                    elif action == "stop_before_retry":
                        controller.mark_disconnected(old, "")
                        stopped = threading.Event()
                        stopped.set()
                        with mock.patch.object(session, "open_session_transport") as opener:
                            result = str(session.reconnect_session(stopped, controller, transcript, "synthetic"))
                            opener.assert_not_called()
                    else:
                        self.fail(action)
                    if expected in {"true", "false"}:
                        self.assertEqual(result.lower(), expected)
                    else:
                        self.assertEqual(result, expected)
                    self.assertEqual(old.close.called, old_closed == "true" or action not in {"stale_disconnect", "stale_send", "current_send", "login_worker_stale"})
                    observed = controller.current_connection()
                    self.assertEqual(
                        "none" if observed is None else "new" if observed[0] is new else "old",
                        current,
                    )
                finally:
                    controller.close()

    def test_first_command_readiness_and_secret_redaction(self):
        cases = list(rows("first_command.tsv", 7))
        self.assertGreaterEqual(len(cases), 3)
        for name, ready, secret, timeout, wire, remember, redacted in cases:
            with self.subTest(name=name):
                controller, ui, transcript, transport = self.controller()
                try:
                    if ready == "true":
                        controller.mark_server_output()
                    with mock.patch.object(controller.ready_for_commands, "wait", wraps=controller.ready_for_commands.wait) as wait:
                        # Simulate timeout without blocking in the silent-server case.
                        if ready == "false":
                            wait.side_effect = lambda timeout: False
                        controller.send_line(
                            "synthetic" if secret == "true" else "look",
                            secret=(secret == "true"),
                        )
                        wait.assert_called_once_with(timeout=float(timeout))
                    transport.sendall.assert_called_once_with(bytes.fromhex(wire))
                    self.assertEqual(ui.remember.called, remember == "true")
                    transcript.write.assert_called_once()
                    self.assertEqual(transcript.write.call_args.kwargs["secret"], redacted == "true")
                finally:
                    controller.close()

    def test_legacy_and_corrupt_persistence_fail_closed_without_rewrite(self):
        cases = list(rows("storage_errors.tsv", 4))
        self.assertGreaterEqual(len(cases), 7)
        payloads = {
            "bad_json": "{broken",
            "json_array": "[]",
            "empty_profiles": '{"profiles":{}}',
            "bad_port": '{"profiles":{"bad":{"host":"localhost","port":70000}}}',
            "wrong_version": '{"format":"aeon-mud-session-profile","version":99}',
            "wrong_format": '{"format":"foreign","version":1}',
            "legacy": '{"profile":{}}',
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, kind, token, expected in cases:
                with self.subTest(name=name):
                    path = Path(tmp) / (name + ".json")
                    path.write_text(payloads[token], encoding="utf-8")
                    before = path.read_bytes()
                    def load():
                        if kind == "load_json":
                            return config.load_json(path, {})
                        if kind == "profile_get":
                            store = profiles.ProfileStore(path)
                            return store.get("bad" if token == "bad_port" else "unknown")
                        if kind == "session_load":
                            return commands.load_session_profile(path)
                        raise AssertionError(kind)
                    if expected == "ok":
                        self.assertIsInstance(load(), dict)
                    else:
                        exc = {"ValueError": ValueError, "SystemExit": SystemExit,
                               "JSONDecodeError": json.JSONDecodeError}[expected]
                        with self.assertRaises(exc):
                            load()
                    self.assertEqual(path.read_bytes(), before)
                    self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_guarded_clipboard_paste_cancellation_and_send_recovery(self):
        cases = list(rows("clipboard.tsv", 8))
        self.assertGreaterEqual(len(cases), 5)
        clipboard = {"NONE": None, "SINGLE": "look", "CRLF": "look\r\nnorth\r\n"}
        for name, token, answer, fail_at, inserted, sends, retained, status in cases:
            with self.subTest(name=name):
                ui = mock.Mock()
                ui.input_buffer = "pre"
                controller = mock.Mock()
                controller.profile.paste_line_delay = 0
                attempted = []
                def send(line, **kwargs):
                    attempted.append(line)
                    if fail_at != "-" and len(attempted) == int(fail_at):
                        raise OSError("synthetic disconnect")
                controller.send_line.side_effect = send
                reader = mock.Mock()
                reader.read_key.return_value = Key("char", answer) if answer != "-" else Key("escape")
                with (
                    mock.patch.object(commands, "get_clipboard_text", return_value=clipboard[token]),
                    mock.patch.object(commands, "KeyReader", return_value=reader),
                    mock.patch.object(commands.time, "sleep", return_value=None),
                ):
                    commands.handle_paste(ui, controller)
                if inserted != "-":
                    ui.insert_text.assert_called_once_with(inserted)
                else:
                    ui.insert_text.assert_not_called()
                expected_sends = [] if sends == "-" else sends.split(";")
                if fail_at != "-":
                    self.assertEqual(attempted, ["prelook"])
                else:
                    self.assertEqual(attempted, expected_sends)
                if retained != "-":
                    ui.replace_input.assert_called_once_with(retained.replace(";", "\n"))
                else:
                    ui.replace_input.assert_not_called()
                if status != "-":
                    self.assertIn(status, [call.args[0] for call in ui.set_status.call_args_list][-1])
                if answer == "no":
                    reader.close.assert_called_once()
                    ui.clear_modal.assert_called_once()


if __name__ == "__main__":
    unittest.main()
