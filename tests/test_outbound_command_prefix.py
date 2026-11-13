"""Regression tests for outbound MUD buffer boundaries."""
import unittest
from pathlib import Path

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


    def test_assignment_breaks_on_spaces_without_losing_text(self):
        prefix = "describe="
        payload = ("longword short " * 120) + "ending"
        chunks = split_outbound_buffers(prefix + payload)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.startswith(prefix) for chunk in chunks))
        self.assertTrue(all(len(chunk) <= OUTBOUND_BUFFER_CHARS for chunk in chunks))
        self.assertEqual("".join(chunk[len(prefix):] for chunk in chunks), payload)
        self.assertTrue(all(chunk[-1].isspace() for chunk in chunks[:-1]))

    def test_spoof_breaks_on_spaces_without_losing_text(self):
        line = ("alpha beta gamma " * 130) + "omega"
        chunks = split_outbound_buffers(line)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.startswith(OUTBOUND_SPOOF_PREFIX) for chunk in chunks[1:]))
        self.assertTrue(all(len(chunk) <= OUTBOUND_BUFFER_CHARS for chunk in chunks))
        self.assertEqual(
            chunks[0] + "".join(chunk[len(OUTBOUND_SPOOF_PREFIX):] for chunk in chunks[1:]),
            line,
        )
        self.assertTrue(all(chunk[-1].isspace() for chunk in chunks[:-1]))

    def test_hard_split_unbroken_oversized_word(self):
        prefix = "description="
        payload = "x" * (OUTBOUND_BUFFER_CHARS * 2)
        chunks = split_outbound_buffers(prefix + payload)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= OUTBOUND_BUFFER_CHARS for chunk in chunks))
        self.assertEqual("".join(chunk[len(prefix):] for chunk in chunks), payload)

    def test_whitespace_runs_preserved(self):
        prefix = "say="
        payload = ("hello  world\t again " * 90) + "end"
        chunks = split_outbound_buffers(prefix + payload)
        self.assertEqual("".join(chunk[len(prefix):] for chunk in chunks), payload)
        self.assertTrue(all(len(chunk) <= OUTBOUND_BUFFER_CHARS for chunk in chunks))

    def test_shared_splitting_fixtures(self):
        """The same deterministic TSV is exercised by Rust unit tests."""
        fixture = Path(__file__).resolve().parents[1] / "contracts/v1/outbound_splitting.tsv"
        tested = 0
        for line in fixture.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            case, prefix_hex, pattern_hex, times, tail_hex, lengths, mode = line.split("\t")
            decode = lambda value: bytes.fromhex(value).decode("utf-8") if value != "-" else ""
            prefix = decode(prefix_hex)
            pattern = decode(pattern_hex)
            tail = decode(tail_hex)
            payload = pattern * int(times) + tail
            original = prefix + payload
            chunks = split_outbound_buffers(original)
            expected = [int(length) for length in lengths.split(",")]
            self.assertEqual([len(chunk) for chunk in chunks], expected, case)
            self.assertTrue(all(len(chunk) <= OUTBOUND_BUFFER_CHARS for chunk in chunks), case)
            if len(chunks) > 1:
                if mode == "assignment":
                    self.assertTrue(all(chunk.startswith(prefix) for chunk in chunks), case)
                    self.assertEqual("".join(chunk[len(prefix):] for chunk in chunks), payload, case)
                elif mode == "spoof":
                    self.assertTrue(all(chunk.startswith(OUTBOUND_SPOOF_PREFIX) for chunk in chunks[1:]), case)
                    self.assertEqual(
                        chunks[0] + "".join(chunk[len(OUTBOUND_SPOOF_PREFIX):] for chunk in chunks[1:]),
                        original,
                        case,
                    )
                else:
                    self.fail(f"unknown fixture mode: {case}")
            else:
                self.assertEqual(chunks, [original], case)
            tested += 1
        self.assertGreaterEqual(tested, 14)


if __name__ == "__main__":
    unittest.main()
