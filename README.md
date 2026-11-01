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

## Controls

- `Up` / `Down`: scroll output
- `PageUp` / `PageDown`: scroll by page
- `Ctrl+Up` / `Ctrl+Down`: command history
- `Ctrl+N`: insert a newline in the wrapped input composer
- `Ctrl+V`: guarded paste with multi-line confirmation
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
