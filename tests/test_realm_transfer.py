"""Portable realm migration: save/restore, merge/replace, invalid inputs and optional providers."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aeon_mud import cli, realm_transfer, terminal, watch_events


class RealmTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.local = self.root / "client" / "realm_watches.json"
        self.export = self.root / "export.json"
        patch = mock.patch.object(realm_transfer, "realm_watch_path", return_value=self.local)
        patch.start()
        self.addCleanup(patch.stop)

    def _write_local(self, data):
        self.local.parent.mkdir(parents=True, exist_ok=True)
        self.local.write_text(json.dumps({"realms": data}), encoding="utf-8")

    def _load_local(self):
        return json.loads(self.local.read_text(encoding="utf-8"))["realms"]

    def test_roundtrip_preserves_capture_and_optional_actions(self):
        rules = [
            terminal.make_watch_rule("You receive [item] from [sender]", "cyan",
                                     sound="github.notice", email=True, cooldown=41,
                                     search=True, lcd="GOT ITEM"),
            terminal.make_watch_rule(r"(?P<user>\w+) attacks (\d+)", "red", use_regex=True)
        ]
        data = terminal.watch_rules_to_data(rules)
        self._write_local({"mud.example:4000": {"enabled": False, "watch_rules": data}})
        self.assertEqual(realm_transfer.export_realms(self.export), 1)
        payload = json.loads(self.export.read_text())
        self.assertEqual(payload["format"], realm_transfer.FORMAT)
        self.assertEqual(payload["version"], 1)
        self.assertNotIn("smtp", json.dumps(payload).lower())
        self.assertEqual(payload["realms"]["mud.example:4000"]["watch_rules"], data)
        self.local.unlink()
        self.assertEqual(realm_transfer.import_realms(self.export), 1)
        imported = self._load_local()["mud.example:4000"]
        self.assertFalse(imported["enabled"])
        self.assertEqual(imported["watch_rules"], data)
        restored = terminal.watch_rules_from_data(imported["watch_rules"])
        self.assertEqual(restored[0].captures(restored[0].match("You receive a key from Merlin"))["named"],
                         {"item": "a key", "sender": "Merlin"})
        self.assertTrue(restored[0].email)
        self.assertEqual(restored[0].sound, "github.notice")

    def test_merge_replace_and_single_realm_export(self):
        self._write_local({
            "a.example:4000": {"enabled": True, "watch_rules": []},
            "b.example:4000": {"enabled": False, "watch_rules": []},
        })
        self.assertEqual(realm_transfer.export_realms(self.export, realm="a.example:4000"), 1)
        self._write_local({"b.example:4000": {"enabled": True, "watch_rules": []}})
        self.assertEqual(realm_transfer.import_realms(self.export), 1)
        self.assertEqual(set(self._load_local()), {"a.example:4000", "b.example:4000"})
        self.assertEqual(realm_transfer.import_realms(self.export, replace=True), 1)
        self.assertEqual(set(self._load_local()), {"a.example:4000"})
        with self.assertRaises(FileExistsError):
            realm_transfer.export_realms(self.export)
        with self.assertRaisesRegex(ValueError, "realm not found"):
            realm_transfer.export_realms(self.root / "missing.json", realm="absent:4000")

    def test_rejects_malformed_import_atomically(self):
        self._write_local({"safe.example:4000": {"enabled": True, "watch_rules": []}})
        original = self.local.read_bytes()
        for data in (
            {"format": "unknown", "version": 1, "realms": {}},
            {"format": realm_transfer.FORMAT, "version": 2, "realms": {}},
            {"format": realm_transfer.FORMAT, "version": 1, "realms": {"safe.example:4000":
                {"enabled": True, "watch_rules": [{"pattern": "[", "color": "yellow", "re": True}]}}},
            {"format": realm_transfer.FORMAT, "version": 1, "realms": {"bad key":
                {"enabled": True, "watch_rules": []}}},
            {"format": realm_transfer.FORMAT, "version": 1, "realms": {"safe.example:4000":
                {"enabled": True, "watch_rules": [{"pattern": "hi", "color": "not-a-color"}]}}},
        ):
            self.export.write_text(json.dumps(data), encoding="utf-8")
            with self.subTest(data=data), self.assertRaises(ValueError):
                realm_transfer.import_realms(self.export)
            self.assertEqual(self.local.read_bytes(), original)
        self.export.write_bytes(b"x" * (realm_transfer.MAX_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "1 MiB"):
            realm_transfer.import_realms(self.export)
        self.assertEqual(self.local.read_bytes(), original)

    def test_cli_commands_and_missing_sound_are_noops(self):
        self._write_local({"a.example:4000": {"enabled": True, "watch_rules": []}})
        cli.build_parser().parse_args(["realm", "export", str(self.export)]).func(
            cli.build_parser().parse_args(["realm", "export", str(self.export)]))
        self.local.unlink()
        cli.build_parser().parse_args(["realm", "import", str(self.export)]).func(
            cli.build_parser().parse_args(["realm", "import", str(self.export)]))
        self.assertEqual(set(self._load_local()), {"a.example:4000"})
        with mock.patch.object(watch_events.shutil, "which", return_value=None), \
             mock.patch.object(watch_events.subprocess, "run") as called:
            watch_events._run_local(watch_events.sound_argv("github.notice"))
            watch_events._run_local(watch_events.lcd_argv("MUD"))
            called.assert_not_called()


if __name__ == "__main__":
    unittest.main()
