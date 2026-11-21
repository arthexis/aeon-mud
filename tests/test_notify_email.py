"""Offline SMTP configuration and transport safety regression tests."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aeon_mud import cli
from aeon_mud import notify_email as email


class EmailNotificationTests(unittest.TestCase):
    def config(self):
        return {
            "host": "mail.example.org", "port": 465, "tls": "ssl",
            "from": "mud@example.org", "to": ["alerts@example.org"],
            "username": "mud-user", "password_env": "MUD_SMTP_PASSWORD",
            "enabled": True,
        }

    def event(self):
        return {
            "schema": 1, "event_id": "example", "timestamp": "2026-11-21T00:00:00Z",
            "realm": "example.org:4000", "character": "Alice",
            "pattern": "Found [item]", "message": "Found an ancient key",
            "captures": {"named": {"item": "an ancient key"}, "groups": ["an ancient key"]},
        }

    def test_write_read_disable_and_no_secret_in_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "client" / "notify_email.json"
            email.save_email_config(self.config(), path)
            self.assertEqual(email.load_email_config(path)["to"], ["alerts@example.org"])
            self.assertNotIn("actual-password", path.read_text())
            self.assertNotIn("password\":", path.read_text())
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            config = email.load_email_config(path)
            config["enabled"] = False
            email.save_email_config(config, path)
            with self.assertRaisesRegex(RuntimeError, "not configured and enabled"):
                email.send_watch_email(self.event(), path)

    def test_invalid_config_fails_without_network(self):
        invalid = [
            {"tls": "none"}, {"port": 0}, {"to": ["bad\nbcc@example.org"]},
            {"from": "evil\r\nbcc@example.org"}, {"password_env": "bad-var"},
            {"enabled": "yes"}, {"password": "do-not-store"},
        ]
        for change in invalid:
            with self.subTest(change=change):
                config = self.config() | change
                with self.assertRaises(ValueError):
                    email.validate_email_config(config)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "notify_email.json"
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                email.save_email_config(self.config(), path)
                # Impossible unless deliberately interrupted.

    def test_tls_email_contains_structured_captures(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "private" / "notify_email.json"
            email.save_email_config(self.config(), path)
            server = mock.MagicMock()
            server.__enter__.return_value = server
            with mock.patch.dict(os.environ, {"MUD_SMTP_PASSWORD": "test-only"}, clear=False), \
                 mock.patch.object(email.smtplib, "SMTP_SSL", return_value=server) as smtp, \
                 mock.patch.object(email.smtplib, "SMTP") as plain:
                email.send_watch_email(self.event(), path)
            self.assertEqual(smtp.call_args.args, ("mail.example.org", 465))
            plain.assert_not_called()
            server.login.assert_called_once_with("mud-user", "test-only")
            message = server.send_message.call_args.args[0]
            body = message.get_content()
            self.assertIn('"item": "an ancient key"', body)
            self.assertEqual(message["Subject"], "aeon-mud watch notification")
            self.assertEqual(message["To"], "alerts@example.org")

    def test_cli_offline_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "client" / "notify_email.json"
            with mock.patch.object(email, "email_config_path", return_value=path), \
                 mock.patch.object(cli, "email_config_path", return_value=path):
                args = cli.build_parser().parse_args([
                    "notify", "email", "configure", "--smtp-host", "mail.example.org",
                    "--from", "mud@example.org", "--to", "alerts@example.org",
                    "--username", "mud-user", "--password-env", "MUD_SMTP_PASSWORD"
                ])
                # The path is looked up by notify_email.save_email_config, not cli.
                with mock.patch.object(cli, "save_email_config", side_effect=lambda data: email.save_email_config(data, path)):
                    args.func(args)
                self.assertTrue(email.load_email_config(path)["enabled"])
                with mock.patch.object(cli, "load_email_config", side_effect=lambda: email.load_email_config(path)), \
                     mock.patch.object(cli, "save_email_config", side_effect=lambda data: email.save_email_config(data, path)):
                    cli.build_parser().parse_args(["notify", "email", "disable"]).func(None)
                self.assertFalse(email.load_email_config(path)["enabled"])


if __name__ == "__main__":
    unittest.main()
