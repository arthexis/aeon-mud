"""Versioned, deterministic compatibility contracts for the Python reference.

Rust consumes exactly these TSV rows; this test verifies expected outputs against
the authoritative Python implementation. No production endpoints or credentials.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from aeon_mud import cli, config, telnet, terminal_text, input as keyboard, security, profiles


ROOT = Path(__file__).resolve().parents[1] / "contracts" / "v1"


def rows(name: str, fields: int):
    source = ROOT / name
    seen = set()
    lines = source.read_text(encoding="utf-8").splitlines()
    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != fields:
            raise AssertionError(f"{name}: expected {fields} columns: {line!r}")
        if parts[0] in seen:
            raise AssertionError(f"{name}: duplicate case {parts[0]!r}")
        seen.add(parts[0])
        yield parts


def decode_chunks(value: str):
    return [bytes.fromhex(chunk) for chunk in value.split(",")]


def hex_bytes(value: str):
    return b"" if value == "-" else bytes.fromhex(value)


class CompatibilityContractTests(unittest.TestCase):
    def test_telnet_binary_stream(self):
        cases = list(rows("telnet.tsv", 4))
        self.assertGreaterEqual(len(cases), 7)
        for name, chunks, output_hex, replies_hex in cases:
            with self.subTest(name=name):
                codec = telnet.TelnetCodec()
                out = bytearray()
                replies = bytearray()
                for chunk in decode_chunks(chunks):
                    received, response = codec.feed(chunk)
                    out.extend(received)
                    replies.extend(response)
                self.assertEqual(bytes(out), hex_bytes(output_hex))
                self.assertEqual(bytes(replies), hex_bytes(replies_hex))

    def test_streaming_text_safety(self):
        cases = list(rows("text.tsv", 3))
        self.assertGreaterEqual(len(cases), 6)
        for name, chunks, expected_hex in cases:
            with self.subTest(name=name):
                decoder = terminal_text.TerminalStreamDecoder("utf-8")
                result = "".join(decoder.feed(chunk) for chunk in decode_chunks(chunks))
                self.assertEqual(result.encode("utf-8"), hex_bytes(expected_hex))

    def test_host_parsing(self):
        cases = list(rows("hosts.tsv", 5))
        self.assertGreaterEqual(len(cases), 8)
        for name, host_input, explicit, expected_host, expected_port in cases:
            with self.subTest(name=name):
                explicit_port = None if explicit == "-" else int(explicit)
                if expected_host == "ERROR":
                    with self.assertRaises(config.HostPortError):
                        config.normalize_host_port(
                            host_input, explicit_port, explicit_port=explicit_port is not None
                        )
                else:
                    self.assertEqual(
                        config.normalize_host_port(
                            host_input, explicit_port, explicit_port=explicit_port is not None
                        ), (expected_host, int(expected_port))
                    )

    def test_nested_help_aliases(self):
        cases = list(rows("help.tsv", 3))
        self.assertGreaterEqual(len(cases), 5)
        for name, raw_argv, expected_argv in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    cli.normalize_help_argv(raw_argv.split(",")),
                    expected_argv.split(","),
                )

    def test_keyboard_control_contracts(self):
        cases = list(rows("keys.tsv", 3))
        self.assertGreaterEqual(len(cases), 10)
        for name, hex_char, event_name in cases:
            with self.subTest(name=name):
                key = keyboard.key_from_control_char(chr(int(hex_char, 16)))
                self.assertEqual(key.name, event_name)

    def test_outbound_buffer_boundaries(self):
        cases = list(rows("outbound.tsv", 4))
        self.assertGreaterEqual(len(cases), 7)
        for name, char, count, lengths in cases:
            with self.subTest(name=name):
                self.assertEqual(len(char), 1)
                source = char * int(count)
                chunks = terminal_text.split_outbound_buffers(source)
                self.assertEqual(
                    [len(chunk) for chunk in chunks],
                    [int(n) for n in lengths.split(",")],
                )
                self.assertTrue(all(len(chunk) <= 1024 for chunk in chunks))
                self.assertTrue(all(chunk.startswith("spoof ") for chunk in chunks[1:]))
                self.assertEqual(
                    chunks[0] + "".join(chunk[len("spoof "):] for chunk in chunks[1:]),
                    source,
                )

    def test_character_identity_keys_without_credentials(self):
        cases = list(rows("identity.tsv", 5))
        self.assertGreaterEqual(len(cases), 4)
        for name, host, port, character, key in cases:
            with self.subTest(name=name):
                profile = profiles.Profile(name="synthetic", host=host, port=int(port))
                self.assertEqual(
                    security.character_secret_key(profile, character), key
                )


if __name__ == "__main__":
    unittest.main()
