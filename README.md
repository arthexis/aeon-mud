# aeon-mud

Standalone line-oriented Telnet client for MUD sessions.

This repository is intentionally independent from game projects and server
projects. It has no runtime dependencies outside the Python standard library.

## Run

```powershell
.\aeon-mud.bat connect --host 127.0.0.1 --port 4000 --allow-plaintext
```

On Linux, macOS, or Raspberry Pi OS:

```bash
./aeon-mud connect --host 127.0.0.1 --port 4000 --allow-plaintext
```

The client also accepts `HOST:PORT` directly:

```powershell
.\aeon-mud.bat connect 127.0.0.1:4000 --allow-plaintext
```

## Rust console prototype

The first **native Windows console executable** lives in `rust/`. This is a
dependency-free Rust program that runs without Python, .NET, PowerShell scripts,
or a separate terminal emulator. It is **not yet a Telnet client**: use the
Python client for real MUD connections until Rust feature parity is reached.

GitHub Actions builds and tests the Windows MSVC executable and publishes
the `aeon-mud-rust-windows-x64` artifact on PRs. Download it from the successful
**CI** run, extract `aeon-mud.exe` anywhere, then use:

```powershell
.\aeon-mud.exe --help
.\aeon-mud.exe --version
.\aeon-mud.exe self-test
.\aeon-mud.exe
```

Running it in Windows Terminal uses the existing tab. Double-clicking the EXE
from Explorer opens the configured Windows default console application, which
can be Windows Terminal. With no arguments the prototype displays its status
and waits for Enter in an interactive console, rather than disappearing.
Connection and profile commands deliberately report not implemented.

With a local Rust compiler, contributors can run:

```powershell
cd rust
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test
cargo run -- --help
cargo build --release --target x86_64-pc-windows-msvc
```

Rust is needed to build, not to run, the EXE. Python remains the reference
implementation while Telnet, profile, credentials and TUI behavior are ported.

## Python package (increment 1)

The repository now includes an `aeon_mud` package with a stable module
and console entry point. A checkout still runs without installation:

```powershell
python -m aeon_mud connect tapestries --login Merlin
python -m aeon_mud self-test
```

Optional editable installation makes `aeon-mud` available as a command
where your Python scripts directory is on PATH:

```powershell
python -m pip install -e .
aeon-mud self-test
```

The Windows BAT/PowerShell launchers, Unix shell script, and
`python aeon_mud_client.py` remain supported.
For this first increment, the package CLI delegates to the existing client
module; internal modules migrate incrementally in later PRs. No runtime
dependencies were added, and profile/credential paths remain unchanged.

## Package architecture — Increment 2

Infrastructure is now implemented in dedicated, standard-library-only modules:

- `aeon_mud.config`: application paths, JSON persistence and host/port parsing
- `aeon_mud.profiles`: server profiles, command history and known hosts
- `aeon_mud.security`: Windows DPAPI and per-character encrypted secrets
- `aeon_mud.telnet`: stateful Telnet negotiation codec
- `aeon_mud.transport`: TCP, SOCKS5 and subprocess transports
- `aeon_mud.terminal_text`: ANSI sanitization and stream decoding
- `aeon_mud.transcript`: transcript writing and decryption

`aeon_mud_client.py` re-exports the old public names to preserve existing
commands, launchers, tests and consumers. Terminal rendering, session control
and CLI dispatch will move in subsequent increments; their behavior is
unchanged here. Configuration files remain in their existing locations.
The package still has no new runtime dependencies.

## Package architecture — Increment 3

The interactive client is now divided into four additional package modules:

- `aeon_mud.input`: key events, keyboard reader, editable input layout
- `aeon_mud.terminal`: watch rules, output rendering and dual input state
- `aeon_mud.commands`: local slash commands, saved session settings, paste
- `aeon_mud.session`: Telnet startup checks, controller, reconnection and interactive event loop

`aeon_mud_client.py` keeps CLI command definitions, built-in self-test and
compatibility re-exports. Launcher arguments, profile locations, encrypted
credentials and the MUD protocol remain unchanged.

## GitHub branch protection / ruleset

The workflow is named **CI** and runs for every pull request (including
documentation-only changes) and manual `workflow_dispatch`; it does not
run on pushes to `main`.

Three diagnostic jobs run: `self-test` (Linux Python, loopback and wheel),
`windows-terminal` (plain Windows entry points), and `rust-console` (native
Windows Rust build, tests and executable artifact). A final, always-run job
named **`CI`** passes only if all three jobs succeeded. Failed, cancelled or skipped
dependencies fail the gate.

The `main` GitHub ruleset requires exactly one status check, **`CI`**
(GitHub Actions). Do not require the diagnostic jobs individually. Keep the
job name stable, since GitHub matches required check contexts by exact name.

## Tests

The project uses standard-library `unittest` without additional test
dependencies. Run the full suite from the repository root:

```sh
python -m unittest discover -s tests -v
```

The tests are grouped by behavior (transport and reconnect, character login,
startup commands, terminal input, CLI/launchers and package architecture).
Shared network and session fixtures live in `tests/support.py`. Tests
normally import the package module that owns the behavior so mocked
dependencies remain scoped to their implementation. Only compatibility
tests import `aeon_mud_client` directly. Keep the built-in
`python -m aeon_mud self-test` for installed-package smoke checks.

## Package architecture — Increment 4

The package is the authoritative implementation. The CLI commands, parser,
and built-in self-test now live in `aeon_mud.cli`, alongside the existing
`aeon_mud.session`, `terminal`, `input`, `commands`, and infrastructure modules.

`aeon_mud_client.py` is a small compatibility entry point (under 60 lines),
re-exporting the previous names. Existing Windows/Linux launchers,
`python -m aeon_mud`, and the installed `aeon-mud` console script all use
the same CLI implementation. No persistence format, credential, protocol or
runtime dependency changes are part of this final extraction.

The test suite now checks for circular package imports, unintended imports
of the legacy wrapper from package modules, and equivalent CLI help across
entry points. Runtime regression tests continue to cover host consent,
initial Telnet negotiation, first-command handling, auto-login, reconnects
and the terminal's independent input buffers.

## Windows Terminal experiment

On Windows, use the existing Python client directly in **Windows Terminal**.
There is no separate terminal emulator, GUI wrapper, or native launcher to
install. Python must be available on PATH.

```powershell
.\aeon-mud.bat connect tapestries --login Merlin
# Or use the PowerShell wrapper for quoting-sensitive arguments:
.\aeon-mud.ps1 connect tapestries --login Merlin
# CLI help stays in the current tab:
.\aeon-mud.bat help profile list
```

You can open a new Windows Terminal tab with `wt.exe` if desired, but the
client does not require `wt.exe` or manage Windows Terminal tabs. Keyboard,
selection and clipboard shortcuts are governed by Windows Terminal settings;
the client retains its guarded paste command and multiline confirmation
when it receives the corresponding key event. Clipboard and Ctrl+C behavior
must be verified interactively on Windows 11; the project does not override
global terminal keybindings. Exit the client using `Ctrl+]`.

## Profiles

Saved profiles, known hosts, command history, secrets, and transcripts live
outside the repository:

- Windows: `%APPDATA%\AeonMud\client`
- Linux/macOS: `~/.aeon-mud/client`

Show the exact local paths:

```powershell
.\aeon-mud.bat paths
```

Create and use a local profile:

```powershell
.\aeon-mud.bat profile add local-mud --host 127.0.0.1 --port 4000 --allow-plaintext --expected-banner login
.\aeon-mud.bat connect local-mud
```

For complex regex arguments that include shell metacharacters such as `|`, use
the PowerShell wrapper or edit `profiles.json` directly:

```powershell
.\aeon-mud.ps1 profile add local-mud --host 127.0.0.1 --port 4000 --allow-plaintext --expected-banner '(?im)(username|name|login|connect)'
```

An example profile file is provided in `profiles.example.json`.

### Automatically remembered host profiles

Connecting directly with `--host` automatically creates a profile keyed by the
hostname after a **successful** connection. Subsequent launches can use the FQDN
or its unique leftmost subdomain without specifying connection flags again:

```powershell
.\aeon-mud.bat connect --host tapestries.fur.com --port 2069 --allow-plaintext
.\aeon-mud.bat connect tapestries.fur.com
.\aeon-mud.bat connect tapestries
```

For a public address, the client still asks for explicit permission on the
first connection. It remembers that acknowledgement **only** for the exact
hostname and port, after connection succeeds. A different endpoint prompts
again. If more than one saved profile shares the same subdomain, use the FQDN
or the full profile name to disambiguate. Explicitly named profiles continue
to take precedence, and a one-time destination override does not overwrite
a manually saved profile.

Profiles are saved in `profiles.json` under the client configuration
directory. Use `aeon-mud.bat profile list` to inspect or
`aeon-mud.bat profile remove NAME` to forget one. Telnet remains unencrypted;
remembering permission is a convenience, not identity verification.

### Character login

Use the same profile and character arguments from a Windows Terminal tab.

```powershell
.\aeon-mud.bat connect tapestries --login Merlin
.\aeon-mud.bat connect tapestries --login Gandalf
```

The first use of `--login CHARACTER` prompts for the password without
echoing it. After connection, the client sends a single MUD command
`connect CHARACTER PASSWORD`. Future launches for that character reuse its
saved password; each server profile can remember multiple character names.

On Windows, passwords are **reversibly encrypted with Windows DPAPI** in the
existing `secrets.json` file, keyed by server hostname, port and character.
Only character names (not passwords) appear in `profiles.json`. DPAPI
decryption is tied to the current Windows user/machine, so copying files to
another computer will not automatically recover credentials. On other
platforms the password is prompted each time rather than saved insecurely.

To replace or clear one character's saved password without affecting other
characters, use:

```powershell
.\aeon-mud.bat secret set tapestries --character Merlin
.\aeon-mud.bat secret clear tapestries --character Merlin
```

Omit `--password` to enter a replacement without exposing it in PowerShell
history. After clearing, the next `--login Merlin` prompts again.

The login command is redacted from command history and transcripts, but
**Telnet itself is unencrypted**: the server, network intermediaries, or
another party observing the connection can still see the password. Use this
feature only on servers you trust, preferably over a secure tunnel.

## Controls

Two unlabeled input composers are shown at the bottom, each with a simple `>`
prompt. The terminal cursor identifies the active composer. Each remembers its
own draft, cursor location, and history-navigation position. Sending a command
clears **only the active composer**; the other draft stays untouched.

- `Tab`: switch active composer
- `Ctrl+Left` / `Ctrl+Right`: move one word in the active composer
- `Ctrl+K`: delete from the cursor to the end of that composer
- `Ctrl+A` / `Ctrl+E` (or `Home` / `End`): move to start/end
- `Ctrl+U`: clear the active composer
- `Ctrl+W`: delete the preceding word
- `Up` / `Down`: scroll output
- `PageUp` / `PageDown`: scroll by page
- `Ctrl+Up` / `Ctrl+Down`: command history
- `Ctrl+N`: insert a newline in the wrapped input composer
- `Ctrl+V`: guarded paste into the active composer (multi-line confirmation retained)
- `/log [file]`: start logging to `output.log` or to the named file
- `/watch pattern color`: colorize messages that start with `pattern`; `*` is a wildcard
- `/save file` / `/load [file]`: save and restore session profile settings
- `Ctrl+]`: quit
- `F2`: show log path
- `F3`: repeat last command
- `F4`: jump to bottom
- `F5`: show key help

Local slash commands are handled by the client and are not sent to the MUD.
Logging is opt-in; the client does not write session logs unless `/log` is used.

## Transport Notes

Telnet is plaintext. The client adds guard rails around plaintext sessions, but
it does not make Telnet cryptographically safe. For secure hops, use a SOCKS5
proxy or a profile `proxy_command` such as OpenSSH `-W host:port`.

Normal outbound sends longer than 1024 characters are split automatically. The
first 1024 characters are sent unchanged. Continuation chunks are prefixed with
`spoof ` while keeping every emitted line at 1024 characters or less. Secret and
login sends are not split.

## Validate

```powershell
python -m py_compile .\aeon_mud_client.py
.\aeon-mud.bat self-test
python -m json.tool .\profiles.example.json
git diff --check
```
