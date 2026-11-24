# Machine stage 7 — integral acceptance and release boundaries

Tracking: issue #53, arthexis/aeon-mud.

## CI acceptance gates

| Gate | Acceptance evidence |
|---|---|
| V1 compatibility | Existing merchant definition plus advanced expedition definition validate unchanged |
| Receiver integration | Simulated MUD text flows through Transcript, watch bus, TriggerEngine and MachineBridge; incomplete logical lines never match |
| Scheduler integration | Puppet, Trigger and Machine share bounded foreground sends and journal origin/source cursors |
| `//watch` | Matching and persistence preserved; local confirmation messages excluded |
| `//trigger` | One-shot spent receipt, no duplicate sends, `--immediate` retained |
| Puppet | Owner-only socket supports read-only diagnostics; no gameplay writes |
| `//log` | Log switching, source cursor continuity, Puppet journal and offline replay parity |
| Recovery | Restart/disconnect yields `recovery_required`; no silent replay of commands |
| Failure isolation | Injected checkpoint failure blocks machine sends while independent Puppet/Trigger work remains usable |
| Replay | JSONL reaches expected state without dispatch, hashed proposed commands and Mermaid diagram |
| Linux | Full Python suite, wheel install, installed `aeon-machine` smoke |
| Windows | Maintained launchers, machine validate/replay smoke |
| Rust | fmt, clippy, unit tests, Windows executable smoke and artifact |
| Aggregate | Required CI gate green at the exact PR HEAD |

## Reproducible offline checks

~~~sh
python -m pip install -e .
python -m unittest discover -s tests -v
aeon-machine validate examples/merchant.machine.yaml
aeon-machine validate examples/expedition.machine.yaml
aeon-machine replay examples/merchant.machine.yaml examples/merchant.replay.jsonl
aeon-machine diagram examples/expedition.machine.yaml
~~~

The merchant replay should reach `completed` through `idle`,
`awaiting_price` and `purchasing`. The expedition fixture demonstrates
inherited/global transitions, scoped submachine calls/returns, and bounded
timeout retries.

## Release posture

- This acceptance suite uses a simulated receiver, a fake outbound controller
  or an owner-only loopback Unix-domain Puppet socket. It does not contact a
  real MUD server or use live character credentials.
- A merge into GitHub `main` is code acceptance, **not a deployment**.
- Machine replay remains read-only and masks command bodies by default.
- A running instance never resumes after a crash or reconnect without an
  explicit operator recovery decision; uncertain sends are never retried
  implicitly.
- Real-gameplay acceptance remains optional and unperformed until a
  non-production realm, test character and harmless command set are authorized.
- Watch email/LCD/sound providers are independent from MUD sends; tests inject
  local fakes so there are no unexpected external notifications.

## Controlled field rollout — requires a separate identified target

1. Select a non-production realm/character and approve harmless commands such
   as `look`, not purchasing, trading, travel or combat.
2. Capture package version, exact HEAD, machine definition digest, Puppet
   journal/log destinations and current running processes.
3. Back up installation, client settings, Puppet event journal and durable
   machine ledger; do not overwrite active journals.
4. Install a CI-accepted build through the established deployment path.
5. Start with `--observe`; then test one explicitly approved harmless send.
6. Inspect receipts via `aeon-machine CHARACTER diagnose` and
   `aeon-puppet CHARACTER --read-last` and check reconnect behavior.
7. Roll back code if needed; retain the ledger and reconcile any
   `recovery_required` instances before another run.

A later real-world rollout must record its own independent evidence.
