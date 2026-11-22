"""Persistent text-log command semantics and restart behavior."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from aeon_mud.commands import handle_log_command
from aeon_mud.profiles import Profile, ProfileStore
from aeon_mud.transcript import Transcript


class LogOptionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profiles = self.root / "profiles.json"
        self.patch = mock.patch("aeon_mud.commands.ProfileStore", side_effect=lambda: ProfileStore(self.profiles))
        self.patch.start()
        self.profile = Profile(name="log-test", host="localhost")
        ProfileStore(self.profiles).set(self.profile)
        self.transcript = Transcript(self.profile)
        self.ui = mock.Mock()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def cmd(self, value):
        self.assertTrue(handle_log_command(value, self.transcript, self.ui))

    def test_enable_persist_on_existing_and_new_session(self):
        target = str(self.root / "first.log")
        self.cmd(f"//log {target}")
        self.assertFalse(self.transcript.persist)
        self.cmd("//log --persist")
        saved = ProfileStore(self.profiles).get("log-test")
        self.assertTrue(saved.log_persist)
        fresh = Transcript(saved)
        self.assertEqual(fresh.path, Path(target))
        fresh.write("recv", "restarted")
        self.assertIn("restarted", Path(target).read_text())

    def test_stop_clears_persist(self):
        target = str(self.root / "first.log")
        self.cmd(f"//log {target} --persist")
        self.cmd("//log --stop")
        saved = ProfileStore(self.profiles).get("log-test")
        self.assertFalse(saved.log_persist)
        self.assertEqual(Transcript(saved).mode, "off")

    def test_switch_requires_flag_and_preserves_persist(self):
        first = str(self.root / "first.log")
        second = str(self.root / "second.log")
        self.cmd(f"//log {first} --persist")
        self.cmd(f"//log {second}")
        self.assertEqual(self.transcript.path, Path(first))
        self.assertFalse(Path(second).exists())
        self.cmd(f"//log --switch {second}")
        self.assertEqual(self.transcript.path, Path(second))
        self.assertEqual(ProfileStore(self.profiles).get("log-test").transcript_path, second)

    def test_idempotent_same_file(self):
        target = str(self.root / "first.log")
        self.cmd(f"//log {target}")
        self.cmd(f"//log {target}")
        self.assertEqual(Path(target).read_text().count("--- log started "), 1)

    def test_status_and_off(self):
        self.cmd("//log --status")
        self.cmd("//log --persist")
        self.assertTrue(self.transcript.persist)
        self.cmd("//log --off")
        self.assertFalse(self.transcript.persist)


    def test_relative_log_resumes_from_other_working_directory(self):
        import os
        previous = Path.cwd()
        first = self.root / "cwd-first"
        second = self.root / "cwd-second"
        first.mkdir()
        second.mkdir()
        try:
            os.chdir(first)
            self.cmd("//log relative.log --persist")
            expected = first / "relative.log"
            self.assertEqual(ProfileStore(self.profiles).get("log-test").transcript_path, str(expected))
            os.chdir(second)
            recovered = Transcript(ProfileStore(self.profiles).get("log-test"))
            self.assertEqual(recovered.path, expected)
            recovered.write("recv", "from new cwd")
            self.assertIn("from new cwd", expected.read_text())
            self.assertFalse((second / "relative.log").exists())
        finally:
            os.chdir(previous)

    def test_stop_rejects_unrelated_destination(self):
        first = str(self.root / "one.log")
        second = str(self.root / "two.log")
        self.cmd(f"//log {first} --persist")
        self.cmd(f"//log --stop {second}")
        self.assertEqual(self.transcript.path, Path(first))
        self.assertTrue(self.transcript.persist)

    def test_other_transcript_mode_requires_explicit_switch(self):
        self.transcript.mode = "plain"
        self.transcript.path = self.root / "existing.jsonl"
        target = self.root / "text.log"
        self.cmd(f"//log {target}")
        self.assertEqual(self.transcript.mode, "plain")
        self.assertFalse(target.exists())
        self.cmd(f"//log --switch {target}")
        self.assertEqual(self.transcript.mode, "text")
        self.assertEqual(self.transcript.path, target)


if __name__ == "__main__":
    unittest.main()
