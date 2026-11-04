# Stage 1 — Python reference baseline and Rust migration parity inventory

The stage-1 contract baseline is **complete when the associated Python and Rust test jobs are green**. This milestone means *documented, executable reference behavior*, **not that the Rust implementation has achieved feature parity**. Python remains the authoritative interactive client until explicitly approved replacement.

## Status vocabulary

- **Python verified**: Python behavior is covered by named fixtures or existing regression/integration tests.
- **Rust fixture ingestion**: Rust validates the same fixture format but does **not** implement the behavior.
- **Rust passing**: native Rust behavior matches expected fixture output (not yet claimed for any subsystem).
- **Field acceptance**: human-operated Windows Terminal/real MUD verification, required before cutover.

| Domain | Python source / evidence | Test scope | Rust phase | Current status |
| --- | --- | --- | --- | --- |
| CLI and nested help | `cli.py`; `help.tsv`, `tests/test_compat_contracts.py` | Pure, deterministic | 2 | Python verified; Rust fixture ingestion |
| Endpoint parsing and public-host policy | `config.py`, `session.py`; `hosts.tsv`, existing connection tests | Pure + local network | 2, 5 | Python verified; Rust fixture ingestion |
| Profiles, FQDN aliases and history | `profiles.py`; `persistence.tsv`, `storage_errors.tsv` | Isolated files | 2, 6 | Python verified; Rust fixture ingestion |
| Telnet IAC and negotiation | `telnet.py`; `telnet.tsv`; existing loopback | Pure binary + local network | 3 | Python verified; Rust fixture ingestion |
| UTF-8 fragmentation, safe ANSI | `terminal_text.py`; `text.tsv` | Pure deterministic | 3 | Python verified; Rust fixture ingestion |
| Outbound 1024-character splitting | `terminal_text.py`; `outbound.tsv` | Pure deterministic | 3, 5 | Python verified; Rust fixture ingestion |
| Transport TCP, SOCKS5, subprocess proxy | `transport.py`; existing loopback and proxy tests | Local network and subprocess | 4 | Python reference regression tests; Rust not implemented |
| Reconnect, retry and stale transport safety | `session.py`; `reconnect.tsv`, `connection_races.tsv` | Fake clock/sleeps, synthetic transport | 5 | Python verified; Rust fixture ingestion |
| First visible output, bounded first-command wait | `session.py`; `first_command.tsv` | Fake readiness/timeouts | 5 | Python verified; Rust fixture ingestion |
| Login prompting/credential ordering | `session.py`; `login.tsv`; character login regression tests | Synthetic secrets and prompt fragments | 5, 6 | Python verified; Rust fixture ingestion |
| Windows DPAPI secrets and transcripts | `security.py`, `transcript.py`; `identity.tsv`; existing Windows tests | Windows-only encryption; fake secrets | 6 | Python key contracts; actual cross-language DPAPI not yet verified |
| Terminal input, dual composers and keys | `input.py`, `terminal.py`; `keys.tsv`, terminal regression suite | Pure state model + interactive | 7 | Python verified; Rust fixture ingestion |
| Guarded clipboard paste and unsent input | `commands.py`; `clipboard.tsv` | Simulated clipboard/keypresses | 7 | Python verified; Rust fixture ingestion |
| Slash commands, watches and session profiles | `commands.py`; existing Python regression suite | Isolated local state | 6, 7 | Python regression coverage; Rust not implemented |
| Windows Terminal double-click, copy, paste, resizing | Console prototype, future native TUI | Manual Windows 11 field | 7, 8 | Not yet field accepted |
| Evennia and real MUD interoperability | Python session/transport suite, future field tests | Local loopback + field | 8 | Not yet Rust accepted |

## Immutable reference set

All 14 versioned TSV files under `contracts/v1` use unique scenario IDs and are verified against real Python code in `tests/test_compat_contracts.py`, `tests/test_stage1_session_contracts.py` and `tests/test_stage1_final_contracts.py`. Rust ingests the identical committed files via `rust/src/contracts.rs`. Current case count: **85** (47 original, 17 prior session/storage and 21 final race/readiness/storage/clipboard scenarios).

Fixtures use only synthetic hostnames, control bytes and credentials. Tests never connect to external hosts. Failure, timing, read-only and prompt-state scenarios use mocks, temporary files and deterministic fake timing. Existing localhost integration tests additionally exercise the actual Python transport.

## Stage 1 acceptance and handoff

- [x] Source-to-test inventory classified as pure, local integration, Windows-only or field.
- [x] Versioned and sanitized fixture collection for protocol, text, host parsing, CLI, input, outbound, login, reconnection, persistence, race safety, first command and clipboard.
- [x] Python tests assert outcomes against the actual implementation, not against another copy of the expectation.
- [x] Rust tests ingest the same cases as data, with explicit **not implemented** status for the corresponding engine functionality.
- [x] Fake transports, no real servers, fake waits, error/timeout and storage boundaries.
- [ ] Windows Terminal interactive acceptance — deliberately deferred to phases 7–8, not a stage-1 blocker.
- [ ] Rust behavior matching fixtures — begins at phase 2 (CLI), phase 3 (Telnet) and subsequent phases.

**Stage 2** can start after green CI and issue #22 has been updated. Preserve the Python client as reference, do not substitute fixture parsing for true Rust behavior parity, and do not relabel a module “passing” until it asserts native output against reference expectations.
