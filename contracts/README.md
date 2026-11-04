# Compatibility contracts — v1

The files in this directory are **frozen, curated Python reference behavior**
for the incremental Rust port. They contain no credentials or external endpoints.

- `telnet.tsv`: one codec instance per case; feed comma-separated hex chunks in order,
  concatenate payload and response bytes, and compare with expected hexadecimal bytes.
- `text.tsv`: one UTF-8 incremental stream decoder per case; feed hex chunks in order,
  concatenate emitted text, and compare UTF-8 output bytes.
- `hosts.tsv`: host input and optional explicit port; compare normalized host/port
  or require a validation error.
- `help.tsv`: tokenized CLI help argument forms and canonical normalized tokens.
- `keys.tsv`: control-character key events.
- `outbound.tsv`: 1024-character outbound chunk boundaries, including the `spoof ` prefix.
- `identity.tsv`: synthetic host/port/character-derived DPAPI key names, not passwords.
- `reconnect.tsv`: retry delays, backoff caps, disabled reconnect and connection attempt counts; fake transport and recorded sleeps.
- `login.tsv`: prompt-driven username/password ordering and character-login override, using synthetic strings only.
- `persistence.tsv`: profile/character and history round trips in isolated temporary directories, including consecutive duplicate suppression and read-only loads.

All TSVs have a comment header, unique case IDs, and fixed-width columns.
A dash (`-`) represents no bytes or no optional port. Hex encoding avoids
newline, terminal escape and binary-file ambiguity across Windows and Linux.

`tests/test_compat_contracts.py` runs these fixtures against the real Python
implementation and is included in the existing `self-test` CI job.
`rust/src/contracts.rs` consumes the **same committed files**, checks the
format and case counts as part of `cargo test`, and provides the fixture
entrypoint for future Rust implementation tests.

**Important:** Rust does *not* yet implement these codecs or profile parsing.
Ingesting a fixture is not parity. Each future module must assert its output
against these rows before its corresponding feature can be marked complete.

Update a fixture only when fixing an incorrect reference or explicitly
approving a behavior change; add new rows for regressions. Do not capture
live user files or credentials. Remaining phase-1 scenarios include reconnect race conditions, prompt timing across a real loopback transport, corrupted/legacy JSON files, clipboard state transitions and additional ANSI edge cases. Fixtures validate Python behavior; Rust still only ingests these contract files until native implementations are added.
