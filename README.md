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

All original launchers, including `aeon-mud.bat`, the Unix shell script,
`aeon-mud-wezterm.bat`, and `python aeon_mud_client.py`, remain supported.
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

Two diagnostic jobs run: `self-test` (Linux Python, loopback and wheel)
and `wezterm-launcher` (Windows scripts). A final, always-run job named
**`CI`** passes only if both jobs succeeded. Failed, cancelled or skipped
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
re-exporting the previous names. Existing Windows/Linux/WezTerm launchers,
`python -m aeon_mud`, and the installed `aeon-mud` console script all use
the same CLI implementation. No persistence format, credential, protocol or
runtime dependency changes are part of this final extraction.

The test suite now checks for circular package imports, unintended imports
of the legacy wrapper from package modules, and equivalent CLI help across
entry points. Runtime regression tests continue to cover host consent,
initial Telnet negotiation, first-command handling, auto-login, reconnects
and the terminal's independent input buffers.

## Aeon WezTerm clipboard shortcuts

Windows started through `aeon-mud-wezterm.bat` use the repository-local
`aeon-mud-wezterm.lua` shortcuts. They do **not** modify your global
WezTerm settings.

- **Ctrl+C** copies the terminal selection to the Windows clipboard rather
  than interrupting the MUD session.
- **Ctrl+V**, **Ctrl+Shift+V** and **Shift+Insert** request a paste through
  Aeon's clipboard handler. A single line goes into the current draft; a
  multiline paste asks for confirmation before sending commands.
- **Ctrl+]** explicitly ends the client session, as before.

WezTerm's native mouse selection can be copied with Ctrl+C; for copy mode
use the regular WezTerm shortcuts. The launcher-specific config intentionally
uses WezTerm defaults plus these shortcuts rather than the user's separate
global WezTerm theme/key settings. The normal PowerShell/Windows Terminal and
Linux launchers do not load this configuration.

## Optional WezTerm GUI launcher (Windows)

[WezTerm](https://wezterm.org/) is an **optional** terminal GUI host. The client
still works unchanged in PowerShell/Windows Terminal and requires only Python.
WezTerm is not bundled, downloaded automatically, or added as a Python package.

Install WezTerm once using the [official Windows installation instructions](https://wezterm.org/install/windows.html)
or Windows Package Manager:

```powershell
winget install --id wez.wezterm --exact
```

Run the dedicated Windows launcher with the same arguments as `aeon-mud.bat`:

```powershell
.\aeon-mud-wezterm.bat connect --host tapestries.fur.com --port 2069 --allow-plaintext
# Or a previously saved profile:
.\aeon-mud-wezterm.bat connect local-mud
```

Help commands are handled by the same CLI as `aeon-mud.bat`, directly in
the current terminal; they never open WezTerm:

```powershell
.\aeon-mud-wezterm.bat -h
.\aeon-mud-wezterm.bat -h connect
.\aeon-mud-wezterm.bat connect help
.\aeon-mud-wezterm.bat help profile list
```

All of `help connect`, `connect help`, `-h connect`, and
`connect --help` select the same command-specific help. The same applies to
nested commands such as `profile list` and `secret set`.

The launcher starts WezTerm in its own GUI window, running the existing Python
client; the current directory is the repository directory. It checks for
`python.exe` and `wezterm-gui.exe` on PATH, then common Windows install
locations. If WezTerm is installed as a portable ZIP, set
`AEON_WEZTERM_PATH` to its full `wezterm-gui.exe` path. Neither GUI nor
client is installed into the repository.

If the launcher prints no error but no window appears, use a new PowerShell session
after installing with Winget, then run `wezterm.exe start` to check the
WezTerm installation directly. The launcher now prints the resolved executable
path, its launch status and any CLI errors. You can override executable
discovery with `$env:AEON_WEZTERM_PATH = "C:\\Program Files\\WezTerm\\wezterm.exe"`.
WezTerm's CLI delegates to its GUI executable and surfaces useful errors.

The GUI launcher starts Aeon MUD inside a PowerShell session that **stays
open when the Python client exits**, so connection errors and tracebacks remain
visible. After the client exits, read the diagnostic and close the WezTerm
window/tab manually. This is intentional; it also permits troubleshooting
when a session ends immediately after startup.

For direct PowerShell use, invoke `aeon-mud-wezterm.ps1` with the same arguments.
All network and plaintext-consent behavior remains in the Python client.
WezTerm provides the window, tabs, fonts and terminal emulation; Aeon MUD still
owns its keyboard shortcuts and in-terminal UI. Exit with `Ctrl+]` or `Ctrl+C`.

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
.\aeon-mud-wezterm.bat connect --host tapestries.fur.com --port 2069 --allow-plaintext
.\aeon-mud-wezterm.bat connect tapestries.fur.com
.\aeon-mud-wezterm.bat connect tapestries
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

### WezTerm tab titles and character login

WezTerm's tab shows the connection hostname instead of `python.exe`. When
a character is selected, the title becomes `<server> (<character>)`.

```powershell
.\aeon-mud-wezterm.bat connect tapestries --login Merlin
.\aeon-mud-wezterm.bat connect tapestries --login Gandalf
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

## Android APK

The `android/` directory contains a limited Kotlin Android client that produces
a debug APK:

```powershell
cd android
.\gradlew.bat :app:assembleDebug
```

Output:

```text
android/app/build/outputs/apk/debug/app-debug.apk
```

The Android client is intentionally foreground-only and battery-saver gated. It
does not use background services, wake locks, reconnect loops, notifications, or
battery-optimization exemptions. It refuses to connect unless Android reports
system power-saver mode through `PowerManager.isPowerSaveMode`, which is the
portable Android signal available when Samsung maximum power saving is active.

This Windows laptop currently needs a local Android SDK and JDK 17 before it can
run the APK build locally. The GitHub Actions workflow `Android APK` builds the
same debug APK and uploads it as the `aeon-mud-debug-apk` artifact.
