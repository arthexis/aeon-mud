"""Regression tests for outbound MUD buffer boundaries."""
import unittest

from aeon_mud.config import OUTBOUND_BUFFER_CHARS, OUTBOUND_SPOOF_PREFIX
from aeon_mud.terminal_text import split_outbound_buffers


class OutboundCommandPrefixTests(unittest.TestCase):
    def test_assignment_repeats_prefix_on_every_buffer(self):
        prefix = "description="
        payload = "A" * (OUTBOUND_BUFFER_CHARS * 2 + 13)
        chunks = split_outbound_buffers(prefix + payload)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(chunk.startswith(prefix) for chunk in chunks))
        self.assertTrue(all(len(chunk) <= OUTBOUND_BUFFER_CHARS for chunk in chunks))
        self.assertEqual("".join(chunk[len(prefix):] for chunk in chunks), payload)

    def test_multiword_command_and_equals_in_payload(self):
        prefix = "set description="
        payload = "word=one " * (OUTBOUND_BUFFER_CHARS // 4)
        chunks = split_outbound_buffers(prefix + payload)
        self.assertTrue(all(chunk.startswith(prefix) for chunk in chunks))
        self.assertEqual("".join(chunk[len(prefix):] for chunk in chunks), payload)

    def test_exact_limit_does_not_split(self):
        line = "set=" + "x" * (OUTBOUND_BUFFER_CHARS - 4)
        self.assertEqual(split_outbound_buffers(line), [line])

    def test_plain_text_keeps_spoof_continuations(self):
        line = "x" * (OUTBOUND_BUFFER_CHARS + 7)
        chunks = split_outbound_buffers(line)
        self.assertEqual(chunks[0], line[:OUTBOUND_BUFFER_CHARS])
        self.assertEqual(chunks[1], OUTBOUND_SPOOF_PREFIX + line[OUTBOUND_BUFFER_CHARS:])

    def test_oversized_prefix_falls_back_to_spoof(self):
        line = "x" * OUTBOUND_BUFFER_CHARS + "=remaining"
        chunks = split_outbound_buffers(line)
        self.assertEqual(chunks[0], line[:OUTBOUND_BUFFER_CHARS])
        self.assertTrue(chunks[1].startswith(OUTBOUND_SPOOF_PREFIX))


if __name__ == "__main__":
    unittest.main()
