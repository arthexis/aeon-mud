"""Character login, encrypted credential isolation, and WezTerm tab titles."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import aeon_mud_client as mud
from aeon_mud import cli, session


class CharacterLoginTests(unittest.TestCase):
    def profile(self, host="tapestries.fur.com", port=2069):
        return mud.Profile(name="tapestries", host=host, port=port, allow_plaintext=True)

    def args(self, *arguments):
        return mud.build_parser().parse_args(["connect", "tapestries", *arguments])

    def test_login_flag_and_reject_command_injection(self):
        self.assertEqual(self.args("--login", "Merlin").login, "Merlin")
        for name in ["", "a b", "a\nshutdown", "x;quit", "x" * 65]:
            with self.subTest(name=name), self.assertRaises(SystemExit):
                mud.validate_character_name(name)

    def test_title_formats_and_osc_one_only_in_wezterm(self):
        profile = self.profile()
        self.assertEqual(mud.connection_tab_title(profile), "tapestries.fur.com")
        self.assertEqual(mud.connection_tab_title(profile, "Merlin"), "tapestries.fur.com (Merlin)")
        with mock.patch.dict(mud.os.environ, {"WEZTERM_PANE": "1"}), mock.patch.object(
            mud.sys.stdout, "isatty", return_value=True
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            # redirect_stdout replaces the mocked stdout; mock its isatty directly.
            with mock.patch.object(mud.sys.stdout, "isatty", return_value=True):
                mud.set_connection_tab_title(profile, "Merlin")
            self.assertEqual(output.getvalue(), "\x1b]1;tapestries.fur.com (Merlin)\x1b\\")
        with mock.patch.dict(mud.os.environ, {"WEZTERM_PANE": ""}), contextlib.redirect_stdout(io.StringIO()) as output:
            mud.set_connection_tab_title(profile)
            self.assertEqual(output.getvalue(), "")

    def test_secrets_independent_across_character_and_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = self.profile()
            store = mud.SecretStore(Path(tmp) / "secrets.json")
            with mock.patch.object(mud.Dpapi, "protect", side_effect=lambda data: b"sealed:" + data), mock.patch.object(
                mud.Dpapi, "unprotect", side_effect=lambda data: data.split(b":", 1)[1]
            ):
                alice = mud.character_secret_key(profile, "Alice")
                bob = mud.character_secret_key(profile, "Bob")
                elsewhere = mud.character_secret_key(self.profile("elsewhere.test"), "Alice")
                store.set_password(alice, "one")
                store.set_password(bob, "two")
                store.set_password(elsewhere, "three")
                self.assertEqual(store.get_password(alice), "one")
                self.assertEqual(store.get_password(bob), "two")
                self.assertEqual(store.get_password(elsewhere), "three")
                self.assertNotIn("one", store.path.read_text())
                self.assertNotIn("two", store.path.read_text())

    def test_login_sent_once_secret_and_never_recorded(self):
        profile = self.profile()
        transport = mock.Mock()
        transcript = mock.Mock()
        ui = mock.Mock()
        controller = mud.SessionController(profile, transport, mud.TelnetCodec(), transcript, ui, None)
        controller.character_login = ("Merlin", "swordfish")
        controller.send_character_login()
        transport.sendall.assert_called_once_with(b"connect Merlin swordfish\r\n")
        transcript.write.assert_called_once_with("send", "connect Merlin swordfish", secret=True)
        ui.remember.assert_not_called()
        with mock.patch.object(controller, "send_line") as send:
            controller.observe_output("Username:\nPassword:")
            send.assert_not_called()

    def test_first_login_prompts_stores_and_next_login_reuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = self.profile()
            profile.require_private_host = False
            secret_store = mud.SecretStore(Path(tmp) / "secrets.json")
            profile_store = mud.ProfileStore(Path(tmp) / "profiles.json")
            key = mud.character_secret_key(profile, "Merlin")
            transport = mock.Mock()
            with (
                mock.patch.object(mud.Dpapi, "available", return_value=True),
                mock.patch.object(mud.Dpapi, "protect", side_effect=lambda data: b"sealed:" + data),
                mock.patch.object(mud.Dpapi, "unprotect", side_effect=lambda data: data.split(b":", 1)[1]),
                mock.patch.object(session, "SecretStore", return_value=secret_store),
                mock.patch.object(mud.getpass, "getpass", return_value="swordfish") as prompt,
                mock.patch.object(session, "open_session_transport",
                                  return_value=(transport, mud.TelnetCodec(), "ready")),
                mock.patch.object(session, "run_sanity_checks"),
                mock.patch.object(session, "KeyReader") as reader,
                mock.patch.object(session, "MudUi"),
                mock.patch.object(session, "Transcript"),
                mock.patch.object(mud.threading, "Thread") as thread,
            ):
                # Run the deferred login worker synchronously, without
                # starting the mocked receiver loop.
                def start_worker(target, args=(), **kwargs):
                    instance = mock.Mock()
                    if target is mud.send_character_login_after_ready:
                        instance.start.side_effect = lambda: target(*args)
                    return instance
                thread.side_effect = start_worker
                reader.return_value.read_key.return_value = mud.Key("quit")
                args = self.args("--login", "Merlin")
                mud.session_loop(profile, args, on_connected=profile_store.set)
                self.assertEqual(secret_store.get_password(key), "swordfish")
                self.assertEqual(profile_store.get(profile.name).characters, ["Merlin"])
                transport.sendall.assert_called_with(b"connect Merlin swordfish\r\n")
                self.assertEqual(prompt.call_count, 1)
                mud.session_loop(profile_store.get(profile.name), args)
                self.assertEqual(prompt.call_count, 1)
                self.assertEqual(transport.sendall.call_count, 2)

    def test_secret_cli_manages_only_one_character(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_store = mud.ProfileStore(Path(tmp) / "profiles.json")
            profile_store.set(self.profile())
            store = mud.SecretStore(Path(tmp) / "secrets.json")
            set_args = mud.build_parser().parse_args(
                ["secret", "set", "tapestries", "--character", "Merlin"]
            )
            clear_args = mud.build_parser().parse_args(
                ["secret", "clear", "tapestries", "--character", "Merlin"]
            )
            with (
                mock.patch.object(cli, "ProfileStore", return_value=profile_store),
                mock.patch.object(cli, "SecretStore", return_value=store),
                mock.patch.object(mud.Dpapi, "available", return_value=True),
                mock.patch.object(mud.Dpapi, "protect", side_effect=lambda data: b"sealed:" + data),
                mock.patch.object(mud.Dpapi, "unprotect", side_effect=lambda data: data.split(b":", 1)[1]),
                mock.patch.object(mud.getpass, "getpass", return_value="pw"),
            ):
                mud.command_secret_set(set_args)
                key = mud.character_secret_key(self.profile(), "Merlin")
                store.set_password(mud.character_secret_key(self.profile(), "Gandalf"), "other")
                self.assertEqual(store.get_password(key), "pw")
                mud.command_secret_clear(clear_args)
                self.assertIsNone(store.get_password(key))
                self.assertEqual(
                    store.get_password(mud.character_secret_key(self.profile(), "Gandalf")), "other"
                )

    def test_existing_profile_can_store_multiple_character_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = mud.ProfileStore(Path(tmp) / "profiles.json")
            profile = self.profile()
            profile.characters.extend(["Merlin", "Gandalf"])
            store.set(profile)
            reloaded = store.get("tapestries")
            self.assertEqual(reloaded.characters, ["Merlin", "Gandalf"])
            self.assertNotIn("password", reloaded.to_stored_dict())


if __name__ == "__main__":
    unittest.main()
