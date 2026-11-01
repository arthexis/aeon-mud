"""Verify extracted modules are the objects used by legacy entry points."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import aeon_mud_client as legacy
from aeon_mud import config, profiles, security, telnet, transport, terminal_text, transcript, session


class InfrastructureExtractionTests(unittest.TestCase):
    def test_compatibility_identity(self):
        for symbol, module in [
            ("Profile", profiles),
            ("ProfileStore", profiles),
            ("KnownHosts", profiles),
            ("HistoryStore", profiles),
            ("Dpapi", security),
            ("SecretStore", security),
            ("TelnetCodec", telnet),
            ("SocketTransport", transport),
            ("ProcessTransport", transport),
            ("Transcript", transcript),
            ("TerminalStreamDecoder", terminal_text),
        ]:
            with self.subTest(symbol=symbol):
                self.assertIs(getattr(legacy, symbol), getattr(module, symbol))

    def test_profile_persists_and_character_password_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            profile_store = profiles.ProfileStore(Path(directory) / "profiles.json")
            p = profiles.Profile(name="remote", host="remote.test", port=2069)
            p.characters = ["Merlin", "Gandalf"]
            profile_store.set(p)
            self.assertEqual(profile_store.get("remote").characters, ["Merlin", "Gandalf"])
            self.assertEqual(
                security.character_secret_key(p, "Merlin"),
                legacy.character_secret_key(p, "Merlin"),
            )
            self.assertNotEqual(
                security.character_secret_key(p, "Merlin"),
                security.character_secret_key(p, "Gandalf"),
            )

    def test_telnet_codec_remains_transport_independent(self):
        codec = telnet.TelnetCodec()
        payload, negotiation = codec.feed(bytes([telnet.IAC, telnet.DO, telnet.TELNET_NAWS]))
        self.assertEqual(payload, b"")
        self.assertIn(bytes([telnet.IAC, telnet.WILL, telnet.TELNET_NAWS]), negotiation)

    def test_session_host_trust_path_hook_is_patchable(self):
        profile = profiles.Profile(name="local", host="127.0.0.1")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trusted.json"
            with mock.patch.object(session, "known_hosts_path", return_value=path):
                session.run_sanity_checks(profile, "initial banner", True, True)
            self.assertTrue(path.exists())
            self.assertIsNotNone(profiles.KnownHosts(path).get("local"))

    def test_config_path_and_ansi_helpers_share_behavior(self):
        self.assertIs(legacy.profile_path, config.profile_path)
        self.assertIs(legacy.strip_ansi, terminal_text.strip_ansi)
        self.assertEqual(terminal_text.strip_ansi("\x1b[31mhello\x1b[0m"), "hello")


if __name__ == "__main__":
    unittest.main()
