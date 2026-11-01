"""Automatic hostname profile and public-Telnet approval tests."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock


from aeon_mud import cli, profiles, session, telnet



class AutoProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = profiles.ProfileStore(Path(self.directory.name) / "profiles.json")

    @staticmethod
    def args(*parts):
        return cli.build_parser().parse_args(["connect", *parts])

    def test_host_creates_profile_then_reuses_with_no_repeated_flags(self):
        def connected(profile, _args, on_connected=None):
            on_connected(profile)

        with mock.patch.object(cli, "ProfileStore", return_value=self.store), mock.patch.object(
            cli, "session_loop", side_effect=connected
        ):
            cli.command_connect(self.args("--host", "TAPESTRIES.FUR.COM", "--port", "2069",
                                          "--allow-plaintext"))
            saved = self.store.get("tapestries.fur.com")
            self.assertEqual(saved.port, 2069)
            self.assertTrue(saved.allow_plaintext)
            by_host = cli.profile_from_args(self.args("--host", "tapestries.fur.com"), self.store)
            self.assertEqual(by_host.name, "tapestries.fur.com")
            self.assertEqual(by_host.port, 2069)
            by_name = cli.profile_from_args(self.args("tapestries.fur.com"), self.store)
            self.assertEqual(by_name.port, 2069)
            by_subdomain = cli.profile_from_args(self.args("tapestries"), self.store)
            self.assertEqual(by_subdomain.host.casefold(), "tapestries.fur.com")

    def test_new_port_uses_independent_profile(self):
        with mock.patch.object(cli, "ProfileStore", return_value=self.store), mock.patch.object(
            cli, "session_loop", side_effect=lambda profile, args, on_connected=None: on_connected(profile)
        ):
            cli.command_connect(self.args("--host", "tapestries.fur.com", "--port", "2069", "--allow-plaintext"))
            cli.command_connect(self.args("--host", "tapestries.fur.com", "--port", "2070", "--allow-plaintext"))
        self.assertEqual(self.store.get("tapestries.fur.com").port, 2069)
        self.assertEqual(self.store.get("tapestries.fur.com:2070").port, 2070)
        with self.assertRaisesRegex(SystemExit, "ambiguous"):
            cli.profile_from_args(self.args("tapestries"), self.store)

    def test_existing_named_profile_not_mutated_by_target_override(self):
        self.store.set(profiles.Profile(name="favorite", host="original.test", port=4000,
                                   allow_plaintext=True))
        with mock.patch.object(cli, "ProfileStore", return_value=self.store), mock.patch.object(
            cli, "session_loop", side_effect=lambda profile, args, on_connected=None: on_connected(profile)
        ):
            cli.command_connect(self.args("favorite", "--host", "other.test", "--port", "4001"))
        original = self.store.get("favorite")
        self.assertEqual((original.host, original.port), ("original.test", 4000))

    def test_public_approval_saved_after_connection_only(self):
        profile = profiles.Profile(name="example.test", host="example.test", port=2069,
                              allow_plaintext=True)
        args = self.args("--host", "example.test", "--port", "2069")
        transport = mock.MagicMock()
        with (
            mock.patch.object(session, "is_private_target", return_value=False),
            mock.patch.object(session, "prompt_yes_no", return_value=True) as prompt,
            mock.patch.object(session, "open_session_transport", return_value=(transport, telnet.TelnetCodec(), "")),
            mock.patch.object(session, "run_sanity_checks"),
            mock.patch.object(session, "KeyReader", side_effect=KeyboardInterrupt),
            mock.patch.object(session, "MudUi"),
            mock.patch.object(session, "Transcript"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                session.session_loop(profile, args, on_connected=self.store.set)
            self.assertEqual(prompt.call_count, 1)
            self.assertEqual(self.store.get("example.test").approved_public_endpoint, "example.test:2069")
            with self.assertRaises(KeyboardInterrupt):
                session.session_loop(self.store.get("example.test"), args)
            self.assertEqual(prompt.call_count, 1)

    def test_other_port_requires_new_public_consent(self):
        profile = profiles.Profile(name="example.test", host="example.test", port=2070,
                              allow_plaintext=True,
                              approved_public_endpoint="example.test:2069")
        args = self.args("--host", "example.test", "--port", "2070")
        with (
            mock.patch.object(session, "is_private_target", return_value=False),
            mock.patch.object(session, "prompt_yes_no", return_value=False) as prompt,
            mock.patch.object(session, "open_session_transport") as connect,
        ):
            with self.assertRaisesRegex(SystemExit, "aborted"):
                session.session_loop(profile, args)
            prompt.assert_called_once()
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
