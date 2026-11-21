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

## Rust preview GitHub Releases

The native Windows x64 console EXE can be downloaded directly from
[GitHub Releases](https://github.com/arthexis/aeon-mud/releases) instead of
extracting a CI artifact. Preview releases include **aeon-mud.exe** and
**SHA256SUMS.txt**, and are labeled *pre-release* because Rust does not yet
support gameplay. The Python client remains the working reference.

To publish a new preview: open **Actions → Rust Preview Release → Run workflow**,
choose `main`, and enter a new tag such as `rust-preview-v0.1.0`. The workflow
formats, lints, tests and builds the Windows MSVC executable, verifies its
self-test, and creates a prerelease with the EXE and SHA-256 checksum attached.
Pushing an existing `rust-preview-v*` tag also triggers publishing.
Use a **new** tag for each preview; existing releases are not overwritten.

No Rust toolchain is needed to run the downloadable EXE. The EXE remains
a console bootstrap, **not yet a functional MUD client**.

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

## Managed GWay Remote deployment (gway-001 only)

The canonical installer is `deploy.sh`, backed by `ansible/playbook.yml`
and the `aeon_mud` Ansible role. This is an opt-in, local-only CLI deployment,
never a Telnet connection or a systemd service. It requires Python 3.10+,
Ansible Core, Git, the installation user `arthe`, and a machine whose
verified hostname is `gway-001`. All other nodes fail closed.

From an exact, clean source checkout:

```sh
sh ./deploy.sh --expected-sha "$(git rev-parse HEAD)"
```

GWay Remote uses this command only after its own exact-main-SHA, CI and
per-node deployment authorization checks. The owner installer independently
verifies source SHA and a clean tracked tree, assembles a dependency-free
zipapp, checks its `--help`, and atomically activates
`~/.local/share/aeon-mud/current` with the last release in `previous`.
The managed command is `~/.local/bin/aeon-mud`. An unrelated existing command
is never overwritten. Release evidence is a full-SHA directory, verified
through the active executable link, not an attempt journal. Running the
same SHA is idempotent. No root privileges, daemon, external credentials,
auto-login, system service, or SSH management are introduced.

### One-time legacy venv adoption

On gway-001 the owner Ansible role explicitly allows a one-time adoption
only if `~/.local/bin/aeon-mud` is exactly a symlink to
`~/.local/share/aeon-mud/venv/bin/aeon-mud`, owned by the same user and
containing a regular executable. It does not adopt unrelated commands, symlink
chains, or missing targets. The installer checks the exact authorized SHA and
the new CLI before switching. It keeps the virtual environment unchanged and
preserves `~/.local/share/aeon-mud/legacy-entry` as the rollback pointer.
A failed activation restores the original command. Repeating installation of
an already-managed release is idempotent.

To manually restore the legacy command after a successful migration (only
after stopping CLI sessions and inspecting the links):

```sh
test "$(readlink "$HOME/.local/share/aeon-mud/legacy-entry")" = "$HOME/.local/share/aeon-mud/venv/bin/aeon-mud"
ln -s "$HOME/.local/share/aeon-mud/venv/bin/aeon-mud" "$HOME/.local/bin/.aeon-mud-rollback"
mv -Tf "$HOME/.local/bin/.aeon-mud-rollback" "$HOME/.local/bin/aeon-mud"
```

The repository and Ansible role are designed for gway-001 by default.
Other fleet nodes remain unapproved; adding a node requires an explicit
separate policy change and acceptance. A merged PR alone does not imply
installation on the appliance.

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
another computer will not automatically recover credentials.

On Linux/macOS, the client uses Fernet encryption from the Python
`cryptography` package to persist each character's password under
`~/.aeon-mud/client/secrets.json`. The separate `secrets.key` and ciphertext
file are created with mode 0600, under an owner-only (0700) directory.
The key resides in the same Unix account: this protects against accidental
disclosure or copying only `secrets.json`, **not** account compromise.
The key must be backed up together with the encrypted secrets if migration
is intended. The installed gway-001 Ansible role verifies `cryptography`
is present. With no encryption backend available, character passwords are
prompted each time rather than saved in plaintext.

To replace or clear one character's saved password without affecting other
characters, use:

```powershell
.\aeon-mud.bat secret set tapestries --character Merlin
.\aeon-mud.bat secret clear tapestries --character Merlin
```

Omit `--password` to enter a replacement without exposing it in PowerShell
history. After clearing, the next `--login Merlin` prompts again. The same `secret\nset/clear` commands work on Unix using `aeon-mud secret ...`.

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
- `//log [file]`: start logging to `output.log` or to the named file
- `//watch PATTERN [--color COLOR] [--sound [MOTIF.]SOUND] [--lcd LABEL] [--cooldown SECONDS] [--search] [--re] [--email]`: highlight matching output and optionally deliver a local sound/LCD alert. Matches start at column 1 by default; `--search` searches anywhere in each line, `--re` treats PATTERN as a Python regex, and both flags can be combined. Without `--re`, `*` matches any text. Color defaults to yellow and sound cooldown to 5 seconds. Use `//watch list` to inspect rules.
- `//unwatch`: disable highlighting without deleting saved rules; `//unwatch PATTERN` removes one rule. A new `//watch` re-enables highlighting.
- `//help`: list all client-side slash commands
- `//save file` / `//load [file]`: save and restore session profile settings
- `Ctrl+]`: quit
- `F2`: show log path
- `F3`: repeat last command
- `F4`: jump to bottom
- `F5`: show key help

Local commands start with `//` and are never forwarded to the MUD, including unknown `//` commands. Single-slash (`/`) input is sent to the server as normal. Watch rules and their enabled state are saved automatically in `realm_watches.json` per normalized `host:port`, shared by all character profiles connecting to that endpoint.
Logging is opt-in; the client does not write session logs unless `//log` is used.

### Watch events and gway-001 actions

The client produces structured, versioned `mud.watch.matched` events from **complete received server lines**, never from terminal redraws or local `[client]` confirmations. A bounded, asynchronous queue (128 pending events) isolates the MUD reader from external notification playback; drops are counted and recent delivery receipts are kept in memory. Each rule is scoped to the normalized realm host and port, saved to `realm_watches.json` and included in `//save` session profiles. Watches without actions continue to highlight exactly as before.

Examples to enter inside an aeon-mud session:

```text
//watch "You have mail" --color cyan --sound email
//watch "A dragon appears" --search --sound github.warning --lcd "DRAGON ALERT" --cooldown 30
//watch "^You (have|received) mail" --re --sound email
//watch "dragon.*attacks" --re --search --sound warning
//watch "Your character is sleeping" --sound notice
//watch "You receive [item] from [sender]." --email
//watch "^(?P<actor>\\w+) deals (\\d+) damage" --re --email
//watch list
//unwatch "A dragon appears"
```

`--sound notice` invokes the installed owner-level `gway-lcd-sound sound play notice`; `--sound github.warning` invokes `gway-lcd-sound sound play warning --motif github`. A motif is optional. The motif and sound names are validated as single identifiers (letters/digits/underscore/hyphen) and are **never** interpolated into a shell. The local command must be available in the session user's `PATH`; no action is sent to a remote machine or given privileged access. Motifs and sounds are validated by the installed sound provider at playback time.

`--lcd "DRAGON ALERT"` shows a 15-second overlay with `MUD WATCH` on line 1 and the label shortened to 16 characters on line 2. Actions are dispatched only through the installed `gway-lcd-sound` user CLI, not direct I²C access. If the provider is missing or fails, the MUD stays connected; failures are reflected in bounded in-memory receipts.

By default `//watch dragon` matches `dragon attacks` but not `A dragon attacks`. Add `--search` to find `dragon` anywhere in the line. Add `--re` to interpret regex constructs such as `^`, `.*`, and `(a|b)` rather than the default literal-with-`*` wildcard matching. `--re --search` is supported. These modes affect both color highlighting and sound/LCD event emission. Previously saved watch rules without these flags use the new anchored default; add `--search` to retain their former anywhere-in-line behavior.

### Structured watch captures and offline email notification

Without `--re`, each `[sigil]` identifier (letters, digits and underscores, starting with a letter or underscore) captures the corresponding variable text. For example, `//watch "You receive [item] from [sender]." --email` produces `captures.named.item` and `captures.named.sender`. A capture at the end of the pattern consumes the remaining line. Existing `*` wildcards still work, but do not capture values. Duplicate sigil names are rejected. A sigil capture takes precedence over a literal square-bracket phrase that matches the sigil grammar; to match brackets literally, use `--re` and escape them.

With `--re`, Python regular-expression groups are used **instead of sigils**. Numbered captures are in `captures.groups` and named regex groups additionally appear in `captures.named`. Every event contains `schema`, `event_id`, `timestamp`, `realm`, `character`, `pattern`, `message`, and `captures`. All fields are bounded to the existing logical-line length; secret login commands remain excluded.

`--email` is a flag, not an email address. Delivery settings are **offline configuration**, not commands to enter in a MUD session. Configure an authenticated, TLS-only SMTP relay in your normal Bash or PowerShell terminal (replace the example values):

```bash
aeon-mud notify email configure --smtp-host smtp.example.org --smtp-port 465 --tls ssl --from mud@example.org --to alerts@example.org --username mud-user --password-env MUD_SMTP_PASSWORD
aeon-mud notify email show
aeon-mud notify email disable
```

The client saves recipients and SMTP routing to `~/.aeon-mud/client/notify_email.json` on Unix (under its private configuration directory) and does not store the SMTP password there. Set the value of the named password environment variable in the process **launching** aeon-mud; avoid putting credentials in shell history. For servers without SMTP authentication, omit `--username` and `--password-env`. Use `--tls starttls` and the appropriate port when your relay requires STARTTLS. The command performs no test send; delivery occurs only after an enabled `--email` watch matches a completed server line. The email body is JSON with structured capture values; untrusted text is not used for mail headers. Notifications are capped at one email per minute and ten per hour per client session, in addition to each rule's cooldown. Sending failures are isolated from the MUD connection.

`--cooldown` is per realm and pattern (0–86400 seconds). The default 5-second interval prevents rapid duplicate alerts; `--cooldown 0` permits every matching completed line. Disabling watches with `//unwatch` also disables future event creation while keeping rules. Untrusted server text is **not** passed to the shell. This implementation provides local asynchronous sound/LCD actions and opt-in TLS SMTP email delivery. External webhook publishing is not enabled.


### Portable realm watch files

Realm watches can be transferred between computers without copying private credentials, SMTP configuration, or the rest of the client profile:

```bash
aeon-mud realm export realm-watches.json
aeon-mud realm export one-realm.json --realm mud.example:4000
aeon-mud realm import realm-watches.json
aeon-mud realm import realm-watches.json --replace
```

Export creates a **versioned JSON file**, containing the enabled state and saved watch rules for all realms by default. `--realm HOST:PORT` restricts export to a specific normalized endpoint. Export refuses to overwrite an existing file.

Import validates the entire file (patterns, sigils, regexes, notification flags, colors and limits) before writing anything. By default it **merges** imported endpoints into the local realm store, replacing only corresponding endpoint entries and preserving unrelated realms. `--replace` replaces the complete local realm-watch collection. The import is atomic and does not require that email, LCD, or sound be installed.

Imported rules retain `--email`, `--sound` and `--lcd` actions. On systems without `gway-lcd-sound`, sound/LCD actions silently do nothing; email without enabled local SMTP configuration or its required password environment variable also silently does nothing. Pattern matching, highlighting, captures and other actions continue to work. When providers are configured later, the saved actions become effective without re-importing. Neither the SMTP settings nor passwords are exported, so configure email separately on the destination.

## Transport Notes

Telnet is plaintext. The client adds guard rails around plaintext sessions, but
it does not make Telnet cryptographically safe. For secure hops, use a SOCKS5
proxy or a profile `proxy_command` such as OpenSSH `-W host:port`.

Normal outbound sends longer than 1024 characters are split automatically.
For input containing `=`, each chunk repeats the entire prefix through the
first `=` (for example, `description=`) rather than using `spoof `.
Inputs without `=` retain an unprefixed first chunk and use `spoof ` for
continuations. Both variants prefer the last whitespace within the limit,
preserve the whitespace without dropping text, and hard-split a word only when
it cannot fit. The 1024-character limit includes the repeated prefix.
Secret and login sends are not split.

The Rust bootstrap now compiles the matching outbound split policy and tests
it against the original unbroken-text contract cases. Python and Rust also
execute the same deterministic `contracts/v1/outbound_splitting.tsv` fixtures
for assignment prefixes, spoof continuations, natural word boundaries,
whitespace preservation, Unicode (including U+001C–U+001F), oversized words,
and exact 1024-character limits. Fixture checks compare exact chunk lengths
and reconstruct every original payload without loss. Rust explicitly treats
U+001C–U+001F as whitespace to agree with Python's `str.isspace()`.
The native Telnet sender is not implemented yet, so Rust parity here covers
the splitting policy, not live gameplay connections.

## Validate

```powershell
python -m py_compile .\aeon_mud_client.py
.\aeon-mud.bat self-test
python -m json.tool .\profiles.example.json
git diff --check
```


### TLS and protocol detection (Python client)

The default is **`--protocol auto`** for new connections and profiles, so
connections try certificate-verified TLS first. You may choose `--protocol tls`
for TLS-only or `--protocol plain` for explicit Telnet. For example:

```bash
./aeon-mud connect --host mud.example.org --port 4001
./aeon-mud connect --host 127.0.0.1 --port 4000 --allow-plaintext
```

`auto` retries on a **fresh connection** as plaintext only for recognizable
non-TLS framing, and only with `--allow-plaintext` (or saved profile consent).
It never downgrades certificate/hostname verification errors or TLS alerts.
A TLS probe may disrupt legacy Telnet servers, and network attackers may induce
non-TLS framing when plaintext fallback is authorized. Use `tls` without
plaintext permission for security-sensitive servers. There is no reliable
passive protocol auto-detection for servers that wait for clients to speak.

Profiles can save `protocol` as `plain`, `tls`, or `auto` (default). Existing
profiles without the field also resolve to `auto`; an existing plaintext
permission remains necessary to allow fallback. An explicit `plain` profile
continues to work as before. For `proxy_command`, auto uses the existing
plaintext stream **only** if plaintext was authorized, since a process stream
cannot be probed and reconnected transparently; explicit `tls` is rejected.
TLS verification uses system trust roots and the destination hostname; no
insecure-certificate bypass is provided. TLS over SOCKS5 is supported; TLS over
`proxy_command` is not. This change does not extend the Rust prototype.
