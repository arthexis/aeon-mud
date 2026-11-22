"""Observation cursors, read-last, log switching, durable recovery, and CLI integration."""
import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from aeon_mud.puppet import PuppetServer, PuppetClient, main
from aeon_mud.puppet_journal import PuppetJournal
from aeon_mud.profiles import Profile
from aeon_mud.transcript import Transcript


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = mock.patch("aeon_mud.puppet_journal.config_dir", return_value=self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_read_last_and_read_incrementally(self):
        journal = PuppetJournal("Hero")
        journal.record("recv", "before")
        first = journal.start_action(["look"])
        journal.record("send", "look")
        journal.record("recv", "a room\n")
        snapshot = journal.read(last=True)
        self.assertEqual(snapshot["action_id"], first["action_id"])
        self.assertEqual([v["text"] for v in snapshot["events"]], ["a room\n"])
        cursor = snapshot["cursor_end"]
        journal.record("recv", "Exits: north\n")
        self.assertEqual([v["text"] for v in journal.read(after=cursor)["events"]], ["Exits: north\n"])
        second = journal.start_action(["north"])
        journal.record("recv", "next room")
        self.assertEqual([v["text"] for v in journal.read(action_id=first["action_id"], until_next=True)["events"]], ["a room\n", "Exits: north\n"])
        self.assertTrue(journal.read(action_id=first["action_id"], until_next=True)["complete"])
        self.assertEqual(journal.read(last=True)["action_id"], second["action_id"])

    def test_persist_across_new_session_and_log_switch(self):
        first = self.root / "first.log"
        second = self.root / "second.log"
        transcript = Transcript(Profile(name="demo", host="localhost"))
        journal = PuppetJournal("Hero")
        transcript.observers.append(journal.record)
        transcript.enable_text_log(str(first))
        action = journal.start_action(["look"], durable=True, log_path=transcript.path)
        transcript.write("recv", "first response")
        transcript.enable_text_log(str(second))
        transcript.write("recv", "second response")
        restored = PuppetJournal("Hero")
        output = restored.read(action_id=action["action_id"])
        self.assertEqual([v["text"] for v in output["events"]], ["first response", "second response"])
        self.assertEqual(output["events"][0]["log_path"], str(first))
        self.assertEqual(output["events"][1]["log_path"], str(second))
        self.assertGreater(output["events"][0]["log_offset"], 0)
        self.assertGreater(output["events"][1]["log_offset"], 0)

    def test_disabled_log_observation_does_not_create_sidecar(self):
        transcript = Transcript(Profile(name="demo", host="localhost"))
        journal = PuppetJournal("Hero")
        transcript.observers.append(journal.record)
        action = journal.start_action(["look"])
        transcript.write("recv", "visible without logging")
        self.assertEqual(journal.read(action_id=action["action_id"])["events"][0]["text"], "visible without logging")
        self.assertFalse(journal.path.exists())

    def test_redacts_secrets(self):
        journal = PuppetJournal("Hero")
        action = journal.start_action(["look"])
        journal.record("send", "password", secret=True)
        self.assertEqual(journal.read(action_id=action["action_id"], received_only=False)["events"][0]["text"], "[redacted]")

    def test_unknown_action_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown or expired"):
            PuppetJournal("Hero").read(action_id="missing")


    def test_pagination_is_bounded_without_losing_events(self):
        journal = PuppetJournal("Hero")
        action = journal.start_action(["look"])
        for number in range(45):
            journal.record("recv", f"line {number}\n")
        seen = []
        cursor = None
        for _ in range(4):
            result = journal.read(action_id=action["action_id"], after=cursor)
            seen.extend(row["text"] for row in result["events"])
            cursor = result["cursor_end"]
            if not result["more"]:
                break
        self.assertEqual(seen, [f"line {n}\n" for n in range(45)])

    def test_expired_cursor_is_rejected(self):
        journal = PuppetJournal("Hero")
        for index in range(4100):
            journal.record("recv", str(index))
        with self.assertRaisesRegex(ValueError, "expired"):
            journal.read(after=0)


@unittest.skipUnless(os.name == "posix", "Unix sockets only")
class SocketObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name}),
                        mock.patch("aeon_mud.puppet_journal.config_dir", return_value=self.root)]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def test_send_capture_and_read_last(self):
        transcript = Transcript(Profile(name="demo", host="localhost"))
        server = PuppetServer("Hero")
        server.start()
        server.attach(transcript)
        sent = []
        class Controller:
            profile = type("Profile", (), {"paste_line_delay": 0})()
            def current_connection(self):
                return object()
            def send_line(self, line, **kwargs):
                sent.append(line)
                transcript.write("send", line)
        client = PuppetClient("Hero")
        output = []
        thread = threading.Thread(target=lambda: output.append(client.send("look")))
        thread.start()
        deadline = time.monotonic() + 4
        while thread.is_alive() and time.monotonic() < deadline:
            server.drain(Controller())
            time.sleep(.01)
        thread.join(2)
        try:
            self.assertFalse(thread.is_alive())
            self.assertEqual(sent, ["look"])
            action = output[0]["action_id"]
            self.assertTrue(action)
            transcript.write("recv", "You see a tavern\n")
            self.assertEqual(client.read(last=True)["events"][0]["text"], "You see a tavern\n")
            self.assertEqual(client.read(action)["action_id"], action)
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                self.assertEqual(main(["Hero", "--read-last"]), 0)
            self.assertIn("You see a tavern", stdout.getvalue())
        finally:
            server.detach(transcript)
            server.close()


    def test_long_observation_does_not_block_other_clients(self):
        transcript = Transcript(Profile(name="demo", host="localhost"))
        server = PuppetServer("Hero")
        server.attach(transcript)
        server.start()
        class Controller:
            profile = type("Profile", (), {"paste_line_delay": 0})()
            def current_connection(self):
                return object()
            def send_line(self, line, **kwargs):
                transcript.write("send", line)
        client = PuppetClient("Hero")
        read_results = []
        reading = threading.Thread(target=lambda: read_results.append(client.read(after=0, wait=1)))
        reading.start()
        time.sleep(.12)
        output = []
        sent = threading.Thread(target=lambda: output.append(client.send("look")))
        start = time.monotonic()
        sent.start()
        deadline = start + .75
        while sent.is_alive() and time.monotonic() < deadline:
            server.drain(Controller())
            time.sleep(.01)
        sent.join(1)
        reading.join(2)
        try:
            self.assertFalse(sent.is_alive())
            self.assertLess(time.monotonic() - start, 1.7)
            self.assertTrue(output[0]["action_id"])
            self.assertTrue(read_results)
        finally:
            server.detach(transcript)
            server.close()


if __name__ == "__main__":
    unittest.main()
