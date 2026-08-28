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
