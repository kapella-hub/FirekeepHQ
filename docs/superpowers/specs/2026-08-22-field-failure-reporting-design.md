# Field failure reporting — design

**Status:** approved in brainstorming, not yet implemented
**Date:** 2026-08-22
**Scope:** one subsystem. Update visibility (server-behind detection, published
release notes) is a *separate* spec and is deliberately not covered here.

## Problem

We cannot see failures that happen on other people's machines. A user whose
install dies at `create venv`, whose client cannot reach their Keep, or who hits
a runtime error in the gateway simply goes away. Nothing reaches us unless they
choose to write in, and the data says almost nobody does: 157 downloads all-time
against 5 `doctor --report` submissions, four of which were our own testing.

The goal is to learn what is breaking, in the field, without asking users to do
anything per incident.

## Decisions, and why

Five decisions were taken during brainstorming. They constrain everything below.

**1. Consent: asked once at install, automatic thereafter.** Not silent.
`privacy.html` currently promises, of the doctor report, *"This never happens
unless you type the flag"* — a background reporter added without a prompt would
make a published privacy commitment false. Asking once preserves the property
that matters to us (no per-incident user action) while keeping the page honest.

**2. Payload: structured fields only. No free text, ever.** This is the load
bearing decision. `_redact_for_report` in `client/firekeep_client/cli.py` drops
the human-readable `detail` field entirely and documents why:

> `detail` is where paths, hostnames, and config contents live … so the
> redaction is structural — no field to forget to strip — rather than a scrub
> applied after the fact.

Failure detail is exactly that field. A scrubber was considered and rejected:
denylist scrubbing fails *open* on the payload nobody anticipated, and this
codebase already rejected that approach once on the doctor path. Structured
enums keep the structural property: there is no field in which a path, a
hostname, an address or a token can travel.

Concretely, `_check_health` formats `f"{_ep_url(svc, cfg)}: {exc}"` — a
connectivity failure message contains the user's own server address. We send the
error *class*, never the message.

**3. Default: opt-out (prompt defaults to yes).** Defensible here specifically
*because* of decision 2 — with no identifiers and no free text there is nothing
personal in the payload to consent about. It is also the only default that
surfaces install failures, since the prompt appears during the install that is
about to fail and a user in that state does not opt in.

**4. Alerting: new signature immediately, everything else in a daily digest.**
A signature is `kind|stage|error|os|client`. First sighting mails at once; known
signatures are counted. This avoids the failure mode where one bad release
generates hundreds of mails, they get filtered, and nobody reads any of them.

**5. Collection on firekeep.ai; the VPS pulls.** The obvious-looking option —
run the collector on the VPS beside the Keep — is not available: `BIND_ADDR` is
`100.91.3.51`, every service binds to the tailnet or localhost, and the only
thing listening on `0.0.0.0` is sshd. Customer machines cannot reach it.
Publishing a port would put an unauthenticated write endpoint on the host that
runs Neo4j, Qdrant, Redis, the vault and minted admin keys, when
`docs/THREAT-MODEL.md` already carries memory poisoning as its largest open
finding. So collection stays on the public, already-hardened site, and the VPS
reaches *out* to fetch it. No new inbound surface anywhere.

## Non-goals

- Tracebacks, messages, paths, addresses, or any free text. Not "redacted
  later" — never collected.
- Any device, account or session identifier. We keep the existing property:
  two reports can never be tied to the same machine by anything we store.
- Performing server updates. Separate spec, and the answer there is
  detect-and-tell.
- Replacing `doctor --report`. That stays exactly as it is, explicitly opt-in
  per invocation. This is a second, separate channel.

## Architecture

Three hops, each crossing a trust boundary in the safe direction.

```
 customer machine            firekeep.ai (public)           VPS (tailnet only)
┌──────────────────┐        ┌──────────────────────┐      ┌──────────────────┐
│ report.py        │        │ failure-report.php   │      │ cron: pull       │
│  emit()          │──POST─▶│  validate enums      │      │  ssh/rsync ──────┼──┐
│  spool.jsonl     │  HTTPS │  append log          │◀─────┼── (outbound only)│  │
│  flush on hook   │        │  (outside webroot)   │      │                  │  │
└──────────────────┘        │  new-sig → mail      │      │ POST /events ────┼──┘
                            │  24h → digest mail   │      │  → Sentinel      │
                            └──────────────────────┘      │  → dashboard     │
                                                          └──────────────────┘
```

The VPS never accepts inbound traffic from anyone. It already holds an SSH key
(`/root/.ssh/id_ed25519`) and has real `crontab`, unlike the Hostinger host.

## Event schema

Every field is an enum or a version string. There is no string field whose value
originates from an exception, a path, or user input.

```json
{
  "kind":   "install",
  "stage":  "create-venv",
  "error":  "permission-denied",
  "exit":   1,
  "os":     "linux-musl",
  "arch":   "x86_64",
  "client": "1.5.2",
  "py":     "3.11"
}
```

`kind` — `install` | `connectivity` | `runtime`

`stage` (install) — slugified from the `step` strings that **already exist** in
`cmd_install`, so this is naming what the code does rather than adding
bookkeeping: `bootstrap-home`, `configure-config`, `create-venv`,
`pip-install-client`, `pip-install-dex`, `lock-config-perms`, `select-version`,
`render-adapters`, `render-adapter`, `add-to-path`, `join-server`.

Two existing steps interpolate a name (`render {name} adapter`,
`pip install {dex} (local checkout dir)`). Those become a fixed slug plus a
separate enum field (`runtime`, `dex`) — never an interpolated string.

`stage` (connectivity) — the doctor check id: `cortex`, `bridge`, `sentinel`,
`relay`, `embeddings`, `backup`.

`error` — a fixed table, mapped from the exception. Anything unrecognised maps
to `other`; the exception text is never consulted for the value.

| class | mapped from |
|---|---|
| `permission-denied` | `PermissionError`, errno EACCES/EPERM |
| `disk-full` | errno ENOSPC |
| `not-found` | `FileNotFoundError`, errno ENOENT |
| `dns-failure` | `socket.gaierror` |
| `connection-refused` | errno ECONNREFUSED |
| `network-unreachable` | errno ENETUNREACH/EHOSTUNREACH |
| `tls-verify-failed` | `ssl.SSLCertVerificationError` |
| `timeout` | `TimeoutError`, `subprocess.TimeoutExpired` |
| `http-401` / `http-403` / `http-404` / `http-429` / `http-5xx` | HTTP status |
| `unsupported-platform` | explicit platform guards |
| `other` | everything else |

`os` — `darwin` | `linux-gnu` | `linux-musl` | `windows`. Family only; no kernel
or distro version string, which would narrow a machine.

`arch` — `x86_64` | `arm64` | `other`.

`client`, `py` — versions we already publish. Non-identifying.

## Client

One new module, `client/firekeep_client/report.py`.

```python
def emit(kind: str, **fields) -> None:
    """Record a failure. Never raises, never blocks meaningfully."""
```

**Spool first, send second.** `emit` appends to `~/.firekeep/report-spool.jsonl`
and *then* attempts a flush with a short timeout (~2s). This ordering is the
point: the highest-value report is an install failure, and that is precisely the
case where the machine may have no working network. Send-only loses exactly the
reports we most want. The spool is safe to leave on disk because the payload is
non-sensitive by construction.

The spool is capped (64 events / 32KB, oldest dropped) so a machine that is
offline for a month cannot grow a file without bound.

**Flush opportunistically.** The `session_start` hook already performs daily
work (`autoupdate`, `symdexindex`, `docdexsync`, `maildexsync`); it flushes the
spool in the same pass, batched. No new scheduler.

**Consent.** `[report] failures = true` in `~/.firekeep/config`, written by the
install prompt (default yes), with `FIREKEEP_NO_FAILURE_REPORT` as the env
override — the same shape as `auto_update` and `auto_sync`, so
`report.is_enabled(cfg)` mirrors `autoupdate.is_enabled`.

**Capture points — three, all existing chokepoints.**

1. `cli.py:577`, the single `except Exception` in `cmd_install`. It already
   knows `step`; it gains one `report.emit(...)` call.
2. The doctor connectivity checks. The error class is derived from the
   exception; `_ep_url` is never read.
3. The gateway / hook top-level handler for `kind: runtime`.

**Hard requirement:** reporting must never change an exit code, never delay a
command noticeably, and never print a traceback of its own. `_send_doctor_report`
already documents this discipline ("Never raises — a failed report must never
affect doctor's own exit code") and `report.py` follows it.

## Collector — `failure-report.php`

Built to the discipline `doctor-report.php` already documents and was
adversarially reviewed for on 2026-08-20:

- POST only; `Content-Type: application/json` required, so the CORS preflight
  fails and no web page can make a visitor's browser write here.
- No IP, no User-Agent, no cookie, no identifier — ever.
- Log outside the webroot: `domains/firekeep.ai/report-stats/failures.log`.
- Self-rotating past a byte ceiling. An unauthenticated unlimited write endpoint
  must not be able to fill the disk that also holds the support mailboxes.
- `checks`-style arrays logged as arrays, never maps keyed by id — the PHP
  canonical-decimal-key cast that silently turns an object into an array is a
  hazard this codebase has already been bitten by.

**Validate enum VALUES, not just types.** This is the single most important rule
in the file. If `error` accepts any string, it is a free-text smuggling channel
and decision 2 is undone by a client bug or a crafted request. Every field is
checked against its fixed list; an unrecognised value means the report is
**rejected, not logged**.

## Alerting

Signature = `kind|stage|error|os|client`, hashed. State in
`report-stats/signatures.json`.

- Unseen signature → send immediately. This is the "fix issues as they appear"
  path.
- Known signature → increment a counter.
- On every request, if >24h since the last digest and anything has happened,
  send the digest and stamp the time.

**No cron.** The Hostinger host has no `crontab` binary — cron there is managed
through hPanel — and PHP has `exec`, `shell_exec`, `popen` and `proc_open`
disabled. `mail()` is available and `sendmail`/`msmtp` exist, so mail works. The
digest self-triggers on inbound traffic instead, which also means no digest on a
day with nothing to report. That is the correct behaviour, not a workaround.

## VPS ingest

A cron job on the VPS host (not inside a container — no key mounting):

1. `rsync`/`ssh` the failures log from Hostinger over the existing outbound
   path, tracking a byte offset so only new lines are read.
2. POST each new event to Sentinel's intake on the tailnet
   (`http://100.91.3.51:8060`), using the existing internal-key auth
   (`_internal_key_headers`).

Sentinel is the right home rather than a new service: it *is* the environment
observer, it already has `EventIngest(source, event_type, summary, severity)`,
`push_event`, `get_events`, `trim_by_age`, and a collector pattern
(`collectors/docker.py`, `files.py`, `git.py`) to follow.

Mapping: `source = "firekeep.ai/failure-report"`,
`event_type = "<kind>-failure"`, `severity = warn` for a known signature and
`error` for a first sighting. `summary` is composed from the enum values only —
it is generated by us from a fixed vocabulary, not received text.

This buys a property worth naming: `sentinel_get_events` is an existing MCP
tool, so once reports land in Sentinel an agent can be asked "what is breaking
for users?" with no new tooling.

## Dashboard

The dashboard already runs at `100.91.3.51:8040` and already renders Sentinel
events, so field failures appear with no new service. A dedicated view (counts
by stage × os × client, new signatures, trend since last release) is a
follow-up, not a prerequisite.

## Privacy

`privacy.html` gains a bullet describing this channel: what is sent (the enum
list, verbatim), when (on failure, if enabled), how it is turned off, and the
same no-identifier statement the other two collectors carry. The existing doctor
bullet is amended so *"never happens unless you type the flag"* remains true of
`doctor --report` specifically rather than reading as a claim about the product
as a whole.

## Testing

- Enum exhaustiveness: every `step` string in `cmd_install` maps to a `stage`
  slug. A new step with no mapping fails the test — otherwise steps drift and
  reports silently land in `other`.
- Structural: a property test asserting no emitted event contains any value not
  drawn from the fixed vocabularies, given hostile inputs (paths, tokens and
  URLs injected into every exception the mapper sees).
- Endpoint: rejects unknown enum values, rejects non-JSON content type, rejects
  malformed shapes, writes no IP, rotates at the ceiling.
- Client discipline: `emit` never raises and never changes an exit code, proven
  with a collector that is refusing connections, hanging, and returning
  garbage.
- Spool: capped, survives an offline machine, flushes and truncates exactly
  once.

## Risks

- **Opt-out default is a judgement call.** It is defensible because the payload
  is structured-only; if the payload ever gains a free-text field, the default
  must be revisited in the same change. Worth a comment at the config site.
- **Enum drift.** A new install step without a mapping degrades to `other` and
  quietly loses signal. The exhaustiveness test is the guard.
- **The digest depends on traffic.** No reports means no digest — fine — but it
  also means a *collector outage* looks identical to silence. A weekly
  heartbeat, or an alert on "no reports at all in 7 days", is worth adding.

## Open

- Retention on the firekeep.ai log (rotation ceiling is set; total history is
  not).
- Whether new-signature mail should also fire once the same signature crosses a
  volume threshold (e.g. 50 in a day), which is a different signal from novelty.
