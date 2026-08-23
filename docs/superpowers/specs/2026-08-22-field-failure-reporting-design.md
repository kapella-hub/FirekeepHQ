# Field failure reporting — design

**Status:** approved in brainstorming; revised 2026-08-22 after adversarial review
(15-agent verification against both repos + external review). Not yet implemented.
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

**Coverage boundary, stated honestly.** On the release one-liner path the
download, checksum verification, Python provisioning, venv creation and all
wheel installs happen inside the shell bootstraps (`client/bootstrap/install.sh`
steps 1–7, `install.ps1` equivalently) *before any Firekeep Python code exists
on the machine*; `cmd_install`'s `create venv` / `pip install` steps only run on
checkout installs. A design that instruments only `cmd_install` is blind to the
motivating failure. This spec therefore instruments the bootstraps too
(decision 6); what remains invisible is a failure to fetch the bootstrap itself,
which no client-side design can see.

## Decisions, and why

**1. Consent: an explicitly recorded answer, or nothing.** Not "asked once,
automatic thereafter" — that phrasing hid four paths that never ask. The state
is tri-state:

- `[report] failures = true` — enrolled. Written only by a prompt a human
  actually answered (Enter accepts the default, which is yes), or by the
  explicit opt-in mechanisms below.
- `[report] failures = false` — declined. Written by the same prompt.
- **absent — NOT enrolled.** No emit, no spool, nothing.

This deliberately does **not** mirror `autoupdate.is_enabled`
(`client/firekeep_client/autoupdate.py:47` — missing section means ON). That
mirror was the first draft's central defect: every headless install
(`install.sh` falls back to `--non-interactive` when `/dev/tty` cannot be
opened), every join-code install (`cli.py` forces `non_interactive = True` even
on a TTY — and the minted `FIREKEEP_JOIN` one-liner is the standard teammate
onboarding), every `firekeep update` (the bootstrap passes `--non-interactive`
for any already-installed machine), and the **entire existing installed base via
background autoupdate** would have been enrolled with no prompt ever having
existed on the machine — precisely the "background reporter added without a
prompt" that decision 1 exists to forbid. `report.is_enabled(cfg)` returns True
only on an explicit recorded `true` (or the env opt-in);
`FIREKEEP_NO_FAILURE_REPORT` wins as off over everything.

Consequences accepted and disclosed: failures that occur before the prompt has
been answered are lost, except on the bootstrap path where the bootstrap asks
first (decision 6). Machines that are never prompted stay silent forever unless
their owner opts in. That is the honest trade; silence is never enrollment.

**Where the asks live:**

- *Interactive bootstrap install* — the bootstrap asks alongside its existing
  `/dev/tty` prompts, **before the first failure-prone step**, and passes the
  answer through to the config it hands off. This is what preserves decision
  3's property: the prompt appears during the install that is about to fail.
- *Interactive `firekeep install` from a checkout* — the wizard asks during
  `configure-config`. `bootstrap-home` precedes the wizard; a failure there is
  deliberately unreported (no consent exists yet).
- *Interactive `firekeep doctor`* with `[report]` absent — asks the same
  question once, records the answer, never re-asks. This is the migration path
  for the existing installed base and for machines first set up headless.
  The interaction is specified precisely because its edge cases are the consent
  model: the prompt appears **after** the diagnostic output (never delaying or
  interleaving the rows the user asked for); the answer applies to **future**
  failures only — connectivity failures the asking run itself observed are not
  retroactively emitted; **EOF or Ctrl-C leaves `[report]` absent** and
  preserves doctor's own exit code. That last property rules out the existing
  prompt helper: `wizard.console_ask` deliberately converts EOF into the
  default answer (wizard.py:95 — right for the install wizard, silent
  enrollment here), so the consent ask uses its own reader in which only a
  typed answer or a real Enter counts. The `status` alias runs the same
  command and behaves identically — one code path, no spelling-dependent
  surprises. `doctor --report` changes nothing about any of this: the flag is
  consent for that one summary only, never writes `[report]`, and the failure-
  channel ask fires (or not) exactly as it would without the flag.
- *Headless / CI / join-code* — never prompted, stays off. Explicit opt-in:
  `FIREKEEP_FAILURE_REPORT=1` (env, session-scoped, does not write config) or
  `firekeep install --report-failures` (writes `true`).

**Prompt idempotence:** the prompt fires only when the `[report]` key is absent
AND the session is interactive. Re-renders, updates and non-interactive runs
never ask and never rewrite a recorded answer. The config key is the durable
off-switch documented to users; the env var is for CI.

**The prompt text is part of this spec**, because one consent covers three
channels of different character (a one-shot install report, ongoing
connectivity events about the user's own Keep, ongoing runtime errors) and the
wording is the load-bearing element of an opt-out design:

> Send anonymous failure reports to firekeep.ai? When an install step fails, a
> connection to your own Keep fails, or a Firekeep background task errors,
> Firekeep sends category codes only — what failed, the error class, OS family,
> versions. Never paths, messages, addresses, or any persistent device, account
> or session identifier. Ongoing until you turn it off
> (`[report] failures = false`). [Y/n]

The wording claims exactly what the system guarantees and no more. "Anything
that identifies you or this machine" would overclaim: the hosting layer
processes IPs like any web server, and a low-cardinality field combination is
not categorically indistinguishable (decision 3, Privacy). What we can promise
absolutely — and do — is that no path, message, address, or persistent
device/account/session identifier is ever *included*.

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

The first draft carried an exception it did not notice: "every field is an enum
**or a version string**". An open version string is a free-text smuggling
channel and an unbounded signature-minting dimension (see Alerting). This
revision closes both open fields: `client` is validated against the released
version allowlist the site already knows how to produce, and `py` is bucketed
to a fixed vocabulary. **Every field is now a closed enum.**

**3. Default: opt-out at the prompt (Enter = yes) — never opt-out by silence.**
Defensible here specifically *because* of decision 2 — with no identifiers and
no free text there is nothing personal in the payload itself — and because the
bootstrap asks before the failure-prone steps, so the prompt genuinely appears
during the install that is about to fail. The defensibility argument is
deliberately narrower than the first draft's: "no personal data" is a claim
about what **our application code stores**, not about the system — the hosting
layer at Hostinger processes IPs and timestamps like any web server, exactly as
`privacy.html` already discloses for the other two collectors. The intended
lawful basis is legitimate interest in software quality, with the prompt as
transparency and a one-line objection mechanism; the retention section below is
part of that analysis, not a follow-up.

**4. Alerting: budgeted immediate mail for novelty, everything else in a daily
digest.** A signature is `kind|stage|error|os|client`. First sighting mails at
once **within a budget** (below); known signatures are counted. This avoids the
failure mode where one bad release generates hundreds of mails, they get
filtered, and nobody reads any of them — and, since the endpoint is
unauthenticated, it also caps what an attacker can do with the mail path. The
budget is not optional hardening; without it, "new signature mails immediately"
is an unauthenticated mail-amplification primitive (even with every field
closed, the enum cross-product is thousands of mintable signatures).

**5. Collection on firekeep.ai; the VPS pulls sealed segments.** The
obvious-looking option — run the collector on the VPS beside the Keep — is not
available: `BIND_ADDR` is `100.91.3.51`, every service binds to the tailnet or
localhost, and the only thing listening on `0.0.0.0` is sshd. Customer machines
cannot reach it. Publishing a port would put an unauthenticated write endpoint
on the host that runs Neo4j, Qdrant, Redis, the vault and minted admin keys,
when `docs/THREAT-MODEL.md` already carries memory poisoning as its largest
open finding. So collection stays on the public, already-hardened site, and the
VPS reaches *out* to fetch it. No new inbound surface anywhere.

The first draft paired "self-rotating log" with "byte-offset pull" without
connecting them; on the inherited `doctor-report.php` rotation model
(`rename` to `.1`, one generation kept) that combination silently skips,
truncates or double-reads data exactly when volume spikes. This revision
replaces both with **sealed immutable segments** (below): the puller never
reads a file that is still being written, and never tracks an offset.

**6. Bootstrap failures are in scope.** Both bootstraps
(`client/bootstrap/install.sh`, `install.ps1`) gain: the consent ask (folded
into their existing `/dev/tty` prompt flow, before provisioning; no TTY → no
consent → no reporting), and on `die` a single best-effort, fire-and-forget
POST (`curl` / `Invoke-RestMethod`, ~2s timeout, output discarded, never
affecting the exit path) carrying the same closed-enum payload. The bootstraps
cannot map Python exceptions; they map only what they know — their own step
that died, and a coarse error class from tool exit codes (curl's DNS / refused /
timeout / TLS exits; everything else `other`). No spool in the bootstrap: if
the POST fails, the report is lost, which is the accepted cost of keeping two
shell implementations minimal. The enum literals in both scripts are pinned by
a repo test that greps them against the canonical vocabulary, so the three
implementations (sh, ps1, py) cannot drift apart silently.

**7. Delivery is at-least-once, deduplicated by a per-event nonce.** Each event
carries `id`: 128 random bits, hex, minted at emit time. It is a delivery
nonce, not an identifier: it is never derived from machine state, and two
events from the same machine carry unrelated ids. The client truncates the
spool only for ids the collector acknowledged; a lost response is retried and
the collector's dedup window absorbs the replay. Honesty note: events flushed
together arrive together, so arrival adjacency (which timestamps already
expose) transiently reflects same-machine grouping in the log; nothing stored
links two reports beyond that adjacency, and nothing ever could reconstruct it
after the fact.

## Non-goals

- Tracebacks, messages, paths, addresses, or any free text. Not "redacted
  later" — never collected.
- Any device, account or session identifier. The per-event `id` is random per
  event (decision 7); nothing we store ties two reports to the same machine
  beyond the arrival-time adjacency any timestamped log has.
- Performing server updates. Separate spec, and the answer there is
  detect-and-tell.
- Replacing `doctor --report`. That stays exactly as it is, explicitly opt-in
  per invocation. This is a second, separate channel. (The design-record
  comment at `cli.py:1705` — "deliberately NO persisted config toggle" — must
  be amended to say it describes the doctor channel specifically, or it will
  contradict the `[report]` section one screen away.)

## Architecture

Three hops. Connections always initiate from the safer side; **data still flows
low-trust → high-trust on hop 3, so the VPS re-validates everything** (see VPS
ingest).

```
 customer machine            firekeep.ai (public)           VPS (tailnet only)
┌──────────────────┐        ┌──────────────────────┐      ┌──────────────────┐
│ report.py        │        │ failure-report.php   │      │ cron: pull       │
│  emit()          │──POST─▶│  validate enum VALUES│      │  ssh: fetch+del  │
│  spool.jsonl     │  HTTPS │  ack per event id    │◀─────┼── sealed segments│
│  flush: CLI start│ batch  │  append + seal log   │      │  re-validate all │
│  gateway start,  │        │  (failure-stats/,    │      │  aggregate       │
│  session_start   │        │   outside webroot)   │      │  POST /events ───┼─▶ Sentinel
└──────────────────┘        │  budgeted mail,      │      │  (authed, tailnet)│  → dashboard
 bootstrap: consent,        │  24h digest          │      └──────────────────┘
 fire-and-forget POST       └──────────────────────┘
```

The VPS never accepts inbound traffic from anyone outside the tailnet. It
already holds an SSH key (`/root/.ssh/id_ed25519`) and has real `crontab`,
unlike the Hostinger host.

Note on the Hostinger constraints this design leans on (no `crontab` binary,
`exec`/`shell_exec`/`popen`/`proc_open` disabled, `mail()` available): these are
live-host facts, recorded nowhere in the site repo. **Re-verify them on the
host before building**; the digest and segment-seal designs depend on them.

## Event schema

Every field is a closed enum (decision 2, as revised). There is no string field
whose value originates from an exception, a path, or user input.

```json
{
  "id":     "b3f2…32 hex chars…",
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

`id` — 128-bit random hex, minted at emit. Delivery-dedup nonce only
(decision 7).

`kind` — `install` | `connectivity` | `runtime`

`stage` (install, cmd_install) — slugified from the `step` strings that
**already exist** in `cmd_install`, so this is naming what the code does rather
than adding bookkeeping: `bootstrap-home`, `configure-config`, `create-venv`,
`pip-install-client`, `pip-install-dex`, `lock-config-perms`, `select-version`,
`render-adapters`, `render-adapter`, `add-to-path`, `join-server`.

Two existing steps interpolate a name (`render {name} adapter`,
`pip install {dex} (local checkout dir)`). Those become a fixed slug plus a
separate enum field (`runtime`, `dex`) — never an interpolated string.

`stage` (install, bootstrap) — slugified the same way from the bootstraps' own
die-sites: `detect-platform`, `fetch-manifest`, `verify-checksum`,
`provision-python`, `fetch-wheels`, `create-venv`, `install-wheels`,
`runnable-check`, `flip-current`, `handoff`. **Amended (implementation pass):**
`fetch-wheels` — downloading and checksum-verifying the client/dex wheels — is
its own stage between `provision-python` and `create-venv`, not folded into
`provision-python` as the first draft of this schema implied; the two die at
genuinely different points in both bootstraps. (Exact slugs pinned at build
time from the scripts' actual step labels; the cross-language grep test is the
guard.) The first two
stages fail **before the bootstrap knows the target version** — platform
detection (`die "unsupported platform"`) and the `latest.json` fetch both
precede version resolution (`install.sh:244-255`) — so events from those
stages carry the fixed `client` sentinel `unknown-bootstrap` (below). Baking
an "attempted version" into the script at publish time was considered and
rejected: the checkout-served copy would carry a stale literal, and the
templating step is a moving part whose failure mode is a lying field.

`stage` (connectivity) — the doctor check id: `cortex`, `bridge`, `sentinel`,
`relay`, `server` (the all-services-down row from `_check_server_connection` —
the single most interesting field signal, "no Keep reachable at all"),
`embeddings`, `backup`. The `client-version` doctor row is deliberately
excluded — it is an update check against firekeep.ai, not Keep connectivity,
and belongs to the update-visibility spec.

`stage` (runtime) — the hook-core names from the dispatcher registry plus
`gateway-call` and `gateway-dispatch`. Pinned at build time; exhaustiveness
test as for install stages.

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

**The mapper's input contract.** The doctor checks do not see raw
`socket.gaierror` — they see `TransportError`, which wraps `URLError` and
buries the classification in the `__cause__`/`reason` chain
(`client/firekeep_client/transport.py`). Left as-is, every connectivity error
collapses to `other`. Therefore: `TransportError` gains a structured
`category` attribute **assigned at wrap time in transport.py** from the table
above (plus its existing `.status` for the http classes), and the report mapper
consumes only `(category, status)` — it never traverses causes and never reads
messages. Tests exercise *real wrapped transport failures* (gaierror under
URLError under TransportError, `SSLCertVerificationError`, ECONNREFUSED), not
synthetic bare exceptions.

`os` — `darwin` | `linux-gnu` | `linux-musl` | `windows`. Family only; no kernel
or distro version string, which would narrow a machine.

`arch` — `x86_64` | `arm64` | `other`.

`client` — must match a **released version allowlist** the deploy publishes
beside the collector (a one-line-per-version file updated on every release; the
site already knows its releases), **plus the single fixed literal
`unknown-bootstrap`**, accepted only for `kind: install` with a pre-resolution
bootstrap stage (`detect-platform`, `fetch-manifest`) — the closed value for
failures that happen before any version exists to name. Everything else:
shape-checked with the anchored `/D` regex first, then membership. Unknown
version, or `unknown-bootstrap` outside its two stages → the event is
rejected. This closes the one dimension that let an attacker mint unbounded
signatures. (A pinned `FIREKEEP_VERSION` install reports the pinned value; the
allowlist membership check is what keeps that user-supplied string honest.)

`py` — bucketed to a fixed vocabulary: `3.9` … `3.14`, else `other`. Never the
raw `platform.python_version()` string (which can be `3.11.0rc1`, `3.11.0+`,
PyPy forms — precisely the odd environments we want to *see*, as `other`+os
rather than lose to a strict regex reject).

**Per-kind fields — a strict tagged union.** The base fields above are common
to every event; the remaining fields are legal only where a capture point
produces them, and the collector validates the union strictly — a field
present on a kind/stage that does not allow it, a required field missing, or
an out-of-range value rejects the event:

| field | values | legal only for |
|---|---|---|
| `exit` | integer 0–255 (process exit codes; anything else → field omitted client-side, rejected server-side) | `kind: install` |
| `runtime` | `claude` \| `codex` \| `kiro` \| `opencode` \| `claude-desktop` \| `generic` | `stage: render-adapter` |
| `dex` | `symdex` \| `docdex` \| `maildex` | `stage: pip-install-dex` |
| `backend` | `cortex` \| `bridge` \| `sentinel` \| `relay` | `kind: runtime`, `stage: gateway-call` |

The log line's `e` object carries this same union, so the two wire formats
(POST body, log line) stay one schema.

## Client

One new module, `client/firekeep_client/report.py`.

```python
def emit(kind: str, **fields) -> None:
    """Record a failure. Never raises, never blocks meaningfully."""
```

**Consent gate first — and the personal-mode gate beside it.** `emit` is a
no-op unless `report.is_enabled(cfg)` — explicit `true` or env opt-in, per
decision 1. There is no spool-but-hold: an un-consented event is never written
anywhere. Additionally, both `emit` and every flush point are no-ops while
`resolver.is_bypassed()` is true: personal mode promises "nothing logged,
recalled, or sent to the server" (`docs/guides/client-kit.md`), and consent to
failure reporting does not survive that stronger, later promise. Same single
gate every other sender consults; it fails toward not-bypassed, so a bug here
cannot silently stop reporting.

**Spool first, send second.** `emit` appends to `~/.firekeep/report-spool.jsonl`
and *then* attempts a flush with a short timeout (~2s). This ordering is the
point: the highest-value report is an install failure, and that is precisely the
case where the machine may have no working network. The spool is safe to leave
on disk because the payload is non-sensitive by construction.

The spool is capped (64 events / 32KB, oldest dropped) so a machine that is
offline for a month cannot grow a file without bound. `emit` also dedupes
locally: an event identical in every enum field to one emitted in the last 24h
(tiny last-sent marker beside the spool) is dropped, so a hot failing hook
cannot fill the spool with copies.

**Flush points — three, so every runtime has one.** The first draft flushed
only from the `session_start` hook; `contract/matrix.py` records codex,
claude-desktop and generic as "none (no hooks)", so on those runtimes the spool
would never flush, and on a machine whose *install failed* no hooks exist at
all — the flagship report had no delivery path. Flush now happens, batched, at:

1. **Start of every `firekeep` CLI invocation** (install, doctor, update — the
   commands a failed-install user runs on retry).
2. **Gateway startup** — the gateway mounts on every runtime including generic
   and Claude Desktop, making coverage uniform.
3. The `session_start` hook's daily pass (alongside `autoupdate`,
   `symdexindex`, `docdexsync`, `maildexsync`), as before.

All three are guarded by is_enabled + not-bypassed + spool-nonempty, so the
common case costs one stat call. The matrix gains an honest per-runtime row for
this channel.

**Spool concurrency — claim by rename, with crash recovery.** Concurrent
sessions (two windows opening at once; gateway + CLI) must not double-send or
drop events. A flusher claims the spool by atomic rename to
`report-spool.sending.<pid>`, sends from the claimed file, deletes it on full
acknowledgement, and merges it back on failure. `emit` keeps appending to a
fresh `report-spool.jsonl` untouched by the flush. No locks, works on Windows
and POSIX, kills both the whole-spool-duplication race and the
read-then-truncate lost-event window.

Two protocol details rename alone does not give you, specified because a crash
mid-flush must not strand or corrupt anything:

- *Stale-claim adoption.* A process killed after claiming leaves
  `report-spool.sending.<pid>` orphaned forever. At flush start, any
  `report-spool.sending.*` older than 10 minutes is adopted — by atomically
  renaming it to the adopter's own claim name, so when two flushers race for
  the same stale claim exactly one rename wins and the other sees ENOENT and
  moves on. Adopted events are sent (or merged back) like any claim; a replay
  of events the dead process had already delivered is absorbed by the
  collector's dedup ring.
- *Merge is per-line append, never rewrite.* Merging a failed claim back into
  a spool that `emit` is concurrently appending to uses the same primitive
  `emit` uses — one O_APPEND single-line write per unacknowledged event, cap
  enforced — then deletes the claim file. There is no whole-file rewrite
  anywhere in the protocol, so there is nothing a concurrent append can
  corrupt in principle; a crash mid-merge leaves a (now smaller) stale claim
  for the next adoption pass. **Amended (implementation pass): the atomicity
  behind that claim is platform-split, and the first draft stated it as if it
  held uniformly.** On POSIX, O_APPEND's seek-plus-write is one atomic kernel
  syscall, so two processes appending concurrently can never interleave a
  torn line — the claim holds with no further mechanism. On Windows, the CRT
  implements O_APPEND as a *separate* seek then write, which is **not**
  atomic across processes; measured under two concurrent appenders, this
  silently dropped 12.7% of lines. `report.py` therefore additionally
  serializes every Windows append with a short, non-blocking `msvcrt`
  byte-range lock on byte 0 of the spool file (bounded retries, ~500ms
  ceiling; on contention past that ceiling the event is dropped rather than
  the write blocking) — a mechanism this section did not originally
  disclose, because the "nothing a concurrent append can corrupt" framing
  was written as a cross-platform kernel-syscall property when it is only
  true on POSIX by construction.

A two-process concurrency test and a kill-mid-flush recovery test are required
alongside the single-process one.

**Wire shape.** One POST per flush: `{"events": [...]}`, at most 64 events /
32KB (the spool caps guarantee this fits the collector's body cap). Response:
`{"accepted": [ids], "rejected": [ids]}`. The client removes accepted ids from
the claimed file; rejected ids (invalid enums — a client bug) are removed too
and never retried; anything unacknowledged (lost response, 5xx) merges back for
the next flush, where the collector's dedup window absorbs replays
(decision 7). Delivery is at-least-once with bounded duplication; downstream
counters are documented as approximate.

**Capture points — five, all existing chokepoints.**

1. `cli.py:577`, the top-level `except Exception` in `cmd_install` — it already
   knows `step`. **Also** the sibling `TimeoutExpired` handler at `cli.py:572`,
   which equally knows `step` and would otherwise silently exempt every timeout
   from reporting.
2. The doctor connectivity checks, via the `TransportError.category` contract
   above; `_ep_url` is never read.
3. `hooklog.log_failure` — the seam every hook core already routes its caught
   failures through. The dispatcher's own top-level handler
   (`hooks/__main__.py`) sees only *uncaught* crashes; instrumenting only it
   would miss most real hook failures. `log_failure` gains an optional
   structured `(stage, error_class)` pair from its callers; its free-text
   message is logged locally as today and **never** forwarded.
4. The gateway's per-tool-call backend-failure handler (`gateway.py`, the
   -32000 path) and the serve-loop dispatch handler (the -32603 path):
   `stage = gateway-call` / `gateway-dispatch`, plus the backend service as an
   enum (`cortex|bridge|sentinel|relay`).
5. The bootstraps' `die` paths (decision 6).

**Hard requirement:** reporting must never change an exit code, never delay a
command noticeably, and never print a traceback of its own. `_send_doctor_report`
already documents this discipline ("Never raises — a failed report must never
affect doctor's own exit code") and `report.py` follows it.

## Collector — `failure-report.php`

Built to the discipline `doctor-report.php` already documents and was
adversarially reviewed for on 2026-08-20 — and hardened beyond it, because this
endpoint has three things the doctor endpoint does not: mutable shared state,
an outbound mail side effect, and a downstream consumer.

State lives in `domains/firekeep.ai/failure-stats/` — a **new** sibling of the
webroot, following the existing one-directory-per-collector convention
(`doctor-stats/`, `dl-stats/`; the first draft's `report-stats/` name implied
an existing directory that does not exist).

- POST only; `Content-Type: application/json` required, so the CORS preflight
  fails and no web page can make a visitor's browser write here.
- No IP, no User-Agent, no cookie, no identifier — ever written by our code.
  (The hosting layer logs IPs; `privacy.html` discloses that, and this spec's
  privacy language inherits the same qualification rather than claiming
  "no IP anywhere".)
- Body cap 40KB (envelope over the 32KB max batch); event-count cap 64.
- **Validate enum VALUES, not just types.** Every field is checked against its
  fixed list — `client` against the released-versions allowlist, `py` against
  the bucket vocabulary — and an unrecognised value means that event is
  **rejected, not logged** (and its id reported in `rejected`). This is the
  single most important rule in the file: if any field accepts any string, it
  is a free-text smuggling channel and decision 2 is undone by a client bug or
  a crafted request.
- **One flock()'d critical section** (dedicated `.lock` file, `LOCK_EX`) wraps
  everything mutable: the dedup-ring check, the log append, the seal check and
  rename, the signatures state update, and the digest decision. The inherited
  pattern — `LOCK_EX` on the append only — was safe when the append was the
  only state; it is not sufficient here. `signatures.json` is written via
  temp-file + rename (a plain `file_put_contents` truncates before it locks; a
  concurrent reader seeing half a file would classify *everything* as new and
  resurrect the mail storm decision 4 exists to prevent). A corrupt or missing
  state file rebuilds empty — safe *because* the mail budget bounds the
  consequences.
- **Dedup ring:** the last 8192 accepted event ids (order of arrival), checked
  inside the lock. A replayed id is acknowledged as accepted but not re-logged
  and not re-counted. Beyond the ring, a duplicate may count twice — disclosed;
  counters are approximate.
- **Log line schema** (a second wire format, specified as such):
  `{"ts": "<UTC ISO>", "first": true|false, "id": "…", "e": {…}}`, where `e`
  is exactly the tagged union from the Event schema — base fields plus only
  the per-kind fields legal for that event. The `first` flag is stamped at append
  time by the PHP — the mailer already computed novelty, and stamping it makes
  the log the single source of truth so the VPS stays stateless about novelty
  (two independently-derived novelty states would diverge the first time state
  resets, and nobody could say which was right).
- **Sealed segments, not self-rotation.** Inside the lock: when the active
  `failures.log` exceeds 4MB **or** its first line is older than 6h, it is
  renamed to `failures.<UTC-timestamp>-<generation>.log` — the timestamp plus
  a monotonic counter from the state file, so a reset or rebuilt state file
  cannot re-mint a name a previous generation already used — and a fresh
  active file begins. Sealed segments are immutable; the puller fetches and
  deletes them (below) and never touches the active file. Total sealed bytes
  are capped at 256MB — if the VPS stops pulling, oldest segments are dropped
  and the drop is counted in the digest. This keeps the disk-safety property
  (an unauthenticated unlimited write endpoint must not fill the disk that
  also holds the support mailboxes) without the offset-vs-rotation data-loss
  interaction of the first draft.
- **The empty batch is the maintenance ping.** `{"events": []}` is a valid
  request: it enters the same locked critical section, runs the seal-by-age
  check and the digest check, appends nothing, and returns collector health —
  `{"accepted": [], "rejected": [], "sealed": <count>, "active_bytes": <n>}`.
  This exists because the seal and digest checks otherwise run only when a
  report happens to arrive: one failure followed by weeks of silence would
  leave a segment unsealed (and unpullable) indefinitely. The VPS cron sends
  the ping at the start of every run, before listing segments — sealing under
  the collector's own lock (an ssh-side `mv` racing PHP's flock is exactly
  the kind of cross-process race this file exists to avoid), and giving the
  watchdog an unambiguous health signal (Alerting). Harmless from anyone
  else's hands: it writes nothing and reveals only counts.
- `signatures.json` is itself bounded: at most 4096 signatures,
  oldest-evicted. The same cannot-fill-the-disk reasoning the log has applies
  to every state file the endpoint grows.
- `checks`-style arrays logged as arrays, never maps keyed by id — the PHP
  canonical-decimal-key cast that silently turns an object into an array is a
  hazard this codebase has already been bitten by.

## Alerting

Signature = `kind|stage|error|os|client`, hashed. State in
`failure-stats/signatures.json` (bounded, locked, atomic — above).

- Unseen signature → mail immediately, **within the budget**: at most 5
  immediate mails per rolling hour. Overflow novelty is not lost — it is
  flagged in state and enumerated in the next digest ("N new signatures
  suppressed"). The budget is what makes an unauthenticated endpoint unable to
  weaponise the mail path (decision 4), and what makes a state reset safe.
- "Seen" semantics: the signature is recorded durably (locked, atomic)
  **before** the mail attempt. A failed `mail()` leaves it seen-but-unmailed;
  the next digest sweeps unmailed novelties. Novelty notification is therefore
  at-least-once via the digest, without mail retry loops.
- Known signature → increment a counter.
- On every request, if >24h since the last digest and anything has happened,
  send the digest and stamp the time — **stamp only after `mail()` returns
  true**, or a transient mail failure silently eats a day.
- **Mail composition is its own attack surface** — the doctor review never
  covered mail because the doctor endpoint sends none. Fixed recipients, fixed
  subject; report-derived values appear only in the body, CR/LF-stripped;
  every regex anchored with `/D` (the exact class of bug the doctor file
  documents); unit tests include embedded-newline payloads.

**No cron.** The Hostinger host has no `crontab` binary — cron there is managed
through hPanel — and PHP has `exec`, `shell_exec`, `popen` and `proc_open`
disabled. `mail()` is available and `sendmail`/`msmtp` exist, so mail works.
(Re-verify on the host before building; see Architecture.) The digest
self-triggers on inbound traffic instead — where "traffic" includes the VPS's
maintenance ping (Collector), so seal and digest checks run at least once per
cron interval even through weeks of report silence. No digest on a day with
nothing to report remains the correct behaviour, not a workaround. The
watchdog lives on the side that can keep time, and the ping makes it
unambiguous: the VPS cron raises a Sentinel `warning` when the **ping itself
fails** for 7 days (collector down — previously indistinguishable from a
healthy quiet week), and separately notes ping-healthy-nothing-fetched as the
true quiet it is.

## VPS ingest

A cron job on the VPS host (not inside a container — no key mounting):

1. Send the maintenance ping (Collector) — seals any age-ripe segment under
   the collector's lock and reports health for the watchdog.
2. Over the existing outbound ssh path: list sealed segments, fetch each into
   a **durable local inbox** (`/var/lib/firekeep/failure-inbox/`, write +
   fsync, named by segment name — a refetch overwrites idempotently), and
   **delete the remote segment only after the local copy is durably
   verified**. A crash between local write and remote delete refetches the
   same immutable bytes; nothing is lost and nothing new is minted. No byte
   offsets, no reading files being written, nothing to misalign.
3. Ingest from the inbox; move each segment file to `done/` only after its
   Sentinel POST succeeds. The Hostinger→VPS hop is therefore exactly-once
   per segment; the **VPS→Sentinel hop is honestly at-least-once** — a crash
   after Sentinel accepts but before the `done/` move re-POSTs that segment's
   aggregates, and Sentinel's `xadd` does not dedup. Each aggregate event
   carries a deterministic key in `details.batch`
   (`"<segment-name>|<signature-hash>"`) so the dashboard view and any later
   consumer can collapse replays; counters remain approximate by design
   (decision 7), and this hop is one of the documented reasons why.
4. **Re-validate every field of every line against the same enum tables** the
   collector uses, discarding non-conforming lines. This is not belt-and-braces
   politeness: the data on hop 3 flows from a shared PHP host (lower trust)
   into an authenticated tailnet ingest that feeds agent context
   (`sentinel_get_events`), the same threat class `docs/THREAT-MODEL.md`
   carries as its largest open finding. The Hostinger log is untrusted input.
   `summary` and `event_type` are composed on the VPS purely from
   re-validated enum values; no string from the log is ever forwarded.
5. **Aggregate before POSTing:** one Sentinel event per (signature, pull) with
   a `count`, not one per log line. Sentinel's event stream is a single
   `XADD … maxlen≈10000` shared by every collector; a flood posted line-by-line
   would evict the entire environment history. Per-pull ceiling of 500 events;
   any remainder collapses into one summary event that says how much was
   folded. No silent truncation.
6. POST to a **new authenticated route on Sentinel**: `POST /events` on the
   tailnet (`http://100.91.3.51:8060`), which does not exist today —
   Sentinel's `/events` is GET-only and its only write path is the MCP
   `tools/call` surface. The route: guarded by the existing auth middleware
   (X-API-Key; note `AUTH_ENABLED` defaults to false on a personal VPS — the
   deploy that enables this feature must enable auth, or accept that
   tailnet-only exposure is the boundary, and say which in the deploy doc);
   request body is the existing `EventIngest` model, **finally wired in**
   (today it is dead code — intake validation lives inline in the MCP tool)
   and tightened: `severity` as a Literal of `info|warning|error|critical`,
   `details` accepted. Validation failures return 4xx — never a catch-all
   that degrades to a default (a known gotcha in this codebase: a swallowed
   Literal mismatch surfaces as dozens of tests failing on the safe default).
   Response contract: `202 {"stored": n}`.

Mapping: `source = "firekeep.ai/failure-report"`,
`event_type = "<kind>-failure"` (kind re-validated first). Severity:
`info` for a known signature, `warning` for a first sighting — **not** the
first draft's `warn` (Sentinel rejects it; the vocabulary is
`info/warning/error/critical`) and **not** `error` (which matches
`ALERT_SEVERITIES` and would fan every attacker-influenceable first sighting
out as a Relay `alerts` broadcast plus a Cortex webhook — the mail path is the
alerting channel; the Sentinel path is the analytics channel). All event
dimensions land structurally in `details`
(`{kind, stage, error, os, arch, client, py, first, count, batch}`, plus any
union fields present), so the dashboard view reads fields, never parses
summary text.

This buys a property worth naming: `sentinel_get_events` is an existing MCP
tool, so once reports land in Sentinel an agent can be asked "what is breaking
for users?" with no new tooling. One caveat belongs next to that property: the
collector is unauthenticated, so this data is *low-integrity by construction* —
an attacker can fabricate failure patterns or bury a real one in noise. The
dashboard view and any agent-facing summary label the source as unverified
field telemetry, corroborated before it drives action.

## Dashboard

The dashboard already runs at `100.91.3.51:8040` and already renders Sentinel
events, so field failures appear with no new service. A dedicated view (counts
by stage × os × client from `details`, new signatures, trend since last
release) is a follow-up, not a prerequisite.

## Privacy

The page changes are enumerated exhaustively because the first draft amended
one sentence while three others went stale:

- `privacy.html`'s doctor bullet: *"This never happens unless you type the
  flag"* is rescoped to `doctor --report` specifically, **and** *"Running
  `firekeep doctor` … never leaves your machine"* is amended — once failure
  reporting is enabled, a plain doctor run whose connectivity check fails
  *does* send an enum event. Both sentences in that bullet change, not one.
- The collector enumeration ("Neither the download counter nor the
  install-health report writes an IP address, cookie, or identifier…") gains
  the third collector.
- A new bullet describes this channel: what is sent (the enum list, verbatim),
  when (on failure, if enabled), the tri-state consent model and how to turn
  it off (`[report] failures = false`; the env var is for CI), that the
  collector timestamps arrivals server-side, and the same
  no-identifier-written-by-us statement the other two collectors carry — with
  their same hosting-layer qualification, and without claiming the
  os/arch/client/py combination is categorically non-identifying (the doctor
  bullet's careful phrasing — "not necessarily indistinguishable … simply
  never linked to one by anything we store" — is the model).
- The page's Scope section is widened (or a distinct software-telemetry
  section added) so "Website privacy" remains a true title for a page that now
  also covers telemetry emitted by installed software.
- The effective date is bumped — the page itself promises a revised date on
  material changes.

## Retention (ship gate, not open item)

Retention is part of the lawful-basis analysis (decision 3), so it is decided
here, before collection begins:

- **Hostinger:** sealed segments exist only until the next successful pull
  (nominal: hours; hard cap 256MB with oldest-dropped). The active file seals
  by 4MB or 6h. `signatures.json` ≤ 4096 entries; dedup ring ≤ 8192 ids.
  Nothing on the shared host is long-lived.
- **VPS/Sentinel:** events age out via the existing `trim_by_age`
  (`EVENT_RETENTION_HOURS`, default 72). The follow-up trend view needs
  longer-lived *aggregates*; that is its own decision, made when that view is
  built — raw events do not outlive the existing window.
- **Mail:** the digest and novelty mails contain enum values only and live in
  the recipient mailbox like any mail.

## Testing

- Enum exhaustiveness: every `step` string in `cmd_install` maps to a `stage`
  slug; every hook-core name and every doctor connectivity id likewise. A new
  step with no mapping fails the test — otherwise steps drift and reports
  silently land in `other`.
- Cross-language enums: a repo test greps `install.sh` and `install.ps1` for
  their stage/error literals and asserts membership in the canonical
  vocabulary.
- Structural: a property test asserting no emitted event contains any value not
  drawn from the fixed vocabularies, given hostile inputs (paths, tokens and
  URLs injected into every exception the mapper sees).
- Transport contract: mapper tests run against *real wrapped* failures —
  `gaierror` inside `URLError` inside `TransportError`,
  `SSLCertVerificationError`, ECONNREFUSED, HTTP statuses — asserting
  `category`/`status` mapping, never message parsing.
- Endpoint: rejects unknown enum values (including unlisted `client`
  versions, `unknown-bootstrap` outside its two stages, and union fields on a
  kind/stage that does not allow them), rejects non-JSON content type,
  rejects malformed shapes and oversized bodies, writes no IP, seals at the
  ceiling and by age, acks and dedups by id, answers the empty-batch
  maintenance ping (runs the seal/digest checks, returns health, appends
  nothing), survives concurrent requests (state intact, no double mail for
  one signature, no lost counts — a real multi-process test), and strips
  CR/LF from anything reaching mail composition (embedded-newline payloads).
- Client discipline: `emit` never raises and never changes an exit code, proven
  with a collector that is refusing connections, hanging, and returning
  garbage.
- Spool: capped, survives an offline machine, claim-rename protocol proven
  with two concurrent flushers (no duplication, no lost events), accepted ids
  removed exactly once, and a kill-mid-flush test proving stale-claim
  adoption: the orphaned `sending.<pid>` file is adopted exactly once by
  racing flushers and its events are neither stranded nor merged unsafely.
- Consent: `is_enabled` is False with no config, no section, and empty
  section; env off-switch wins over config true; prompt writes exactly once
  and non-interactive re-runs never write; the doctor ask on EOF/Ctrl-C
  leaves `[report]` absent and doctor's exit code unchanged; with
  `resolver.is_bypassed()` true, `emit` writes nothing and no flush point
  sends (personal-mode promise upheld).
- Real-terminal acceptance: drive both bootstraps under a real PTY (POSIX
  `pty`; ConPTY on Windows) covering Enter (default yes recorded), explicit
  no, EOF (consent absent, install proceeds), headless (no controlling tty →
  no prompt, no `[report]` written), a forced provisioning failure (the POST
  fires only when consent was recorded first), and the answer persisting
  through the wizard handoff into `~/.firekeep/config`. The literal-grep test
  alone cannot prove any of this, and the existing bootstrap e2e deliberately
  detaches its controlling terminal (`test_e2e_bootstrap.py:36`) precisely to
  avoid the interactive branch — so the prompt path currently has zero
  coverage by construction.
- VPS ingest: discards non-conforming log lines; aggregates; respects the
  per-pull ceiling; novelty comes from the `first` flag, never recomputed; the
  inbox protocol proven under kill-and-retry at every step boundary — a crash
  before remote delete refetches idempotently, a crash before the `done/`
  move re-POSTs (at-least-once, deterministic `details.batch` key present),
  and no sequence of crashes loses a segment.

## Risks

- **Opt-out-at-the-prompt is a judgement call.** It is defensible because the
  payload is structured-only and the prompt precedes the failure-prone steps;
  if the payload ever gains a free-text field, the default must be revisited
  in the same change. Worth a comment at the config site.
- **Enum drift.** A new install step or hook without a mapping degrades to
  `other` and quietly loses signal. The exhaustiveness tests are the guard.
- **Bounded duplication.** At-least-once delivery beyond the dedup ring, and
  segment retry edges, can double-count. Counters are approximate by design
  and documented as such wherever they surface (digest, dashboard).
- **Signal integrity.** The unauthenticated collector yields low-integrity
  data; labelling and corroboration (VPS ingest section) are the mitigation,
  not a fix. Raising the cost of fabrication (proof-of-work, install-time
  nonce) would reintroduce identifier-shaped machinery and is deliberately
  not done at this volume.
- **Silence ambiguity.** No reports still means "no failures, or nobody
  enrolled, or collector down". The VPS-side 7-day watchdog distinguishes the
  last; the first two are indistinguishable by design (consent).
- **Coverage honesty.** Headless machines, decliners, and never-prompted
  installs are invisible. The matrix row and docs state per-runtime and
  per-path coverage so nobody mistakes this channel for census data.

## Consistency checklist (Change Consistency Checklist instances)

This feature touches, and its plan must include: `client/firekeep_client/`
(`report.py`, `cli.py` — including the `cli.py:1705` design-record comment,
`transport.py`, `hooks/`, `gateway.py`, `wizard.py`, `autoupdate.py`
untouched), `client/bootstrap/install.sh` + `install.ps1`,
`client/firekeep_client/contract/matrix.py` (per-runtime flush row),
`sentinel/app/` (new POST route, `EventIngest` wiring, tests),
`docs/guides/client-kit.md` (config key + env vars; doc-default tests),
`dashboard/index.html` (source labelling; later the dedicated view),
`CLAUDE.md`, `docs/THREAT-MODEL.md` (new endpoint + mail surface), and in the
site repo: `failure-report.php`, the released-versions allowlist in the deploy
flow, `privacy.html` (every bullet enumerated above), and a
`failure-stats` variant of the stats scripts.

## Open

- Whether new-signature mail should also fire once the same signature crosses a
  volume threshold (e.g. 50 in a day), which is a different signal from
  novelty — and how it composes with the mail budget.
- Long-lived aggregate retention for the trend view (decided with that view;
  see Retention).

## Review record

Revised 2026-08-22 after a 15-agent adversarial review (claims verified against
`client/`, `sentinel/`, and the firekeep-site checkout; findings independently
skeptic-verified) plus an external review pass. The load-bearing changes over
the first draft: tri-state consent replacing the autoupdate mirror (silent
enrollment of headless/join/update paths and the entire installed base);
bootstrap instrumentation (the motivating failure predated the old capture
point); closed `client`/`py` fields plus a mail budget (unauthenticated
mail-amplification and unbounded signature state); sealed segments replacing
byte-offset-over-rotation (silent loss/duplication); a single locked critical
section and atomic state writes in the collector; an explicit batch/ack/nonce
wire contract with claim-by-rename spool semantics; VPS-side re-validation and
aggregation (trust boundary, stream eviction, Relay/webhook fan-out);
`hooklog.log_failure`/gateway seams replacing a top-level-only runtime handler;
the `TransportError.category` mapper contract; `warning` not `warn`;
`info`/`warning` not `error` severities; the full `privacy.html` enumeration;
and retention as a ship gate.

Second review pass, same day, nine further findings addressed: the consent
prompt no longer overclaims anonymity (persistent-identifier phrasing);
pre-version bootstrap failures get `detect-platform` and the closed
`unknown-bootstrap` client sentinel; the doctor ask is fully specified
(after output, future-only, EOF/Ctrl-C leaves consent absent — the wizard's
EOF-takes-the-default helper is explicitly ruled out — `status` alias
identical, `--report` never durable); `resolver.is_bypassed()` gates emit and
every flush (personal-mode promise); stale-claim adoption and per-line-append
merge give the spool crash recovery; the empty-batch maintenance ping seals
sparse-traffic segments under the collector's lock and disambiguates the
watchdog; the pull uses a durable inbox with delete-after-durable-fetch,
collision-resistant segment names, and an honestly at-least-once Sentinel hop
with deterministic `details.batch` keys; the event schema is a strict tagged
union (`exit` bounded, `runtime`/`dex`/`backend` scoped); and real-PTY
acceptance tests cover the prompt paths the terminal-detached e2e cannot.

**Implementation pass (2026-08-23).** Executed against this spec end to end
(client, sentinel, deploy tooling, and the firekeep-site collector). Material
deviations from the text above, each amended in place where it matters and
named here as one list for anyone diffing spec against build:

- **`fetch-wheels` is its own bootstrap stage**, not folded into
  `provision-python` — wheel download/verify dies at a different point than
  Python provisioning in both bootstraps (Event schema, above).
- **The spool append's atomicity claim was platform-split, not uniform.**
  POSIX's O_APPEND is a single atomic kernel syscall; Windows' CRT O_APPEND is
  seek-then-write and is not — measured at 12.7% line loss under concurrent
  appenders unlocked, closed with a bounded, non-blocking `msvcrt` byte-0 lock
  that drops the event on a ~500ms contention timeout rather than blocking
  (Client, above).
- **The collector never acks an unwritten event**, and the mail budget is a
  true rolling hour, not a fixed window. The first implementation of
  `failure-report.php` acked ids before confirming the log write/seal/state
  writes had succeeded, and used a fixed `mail_window_start`/`mail_count` pair
  that could admit more than the budget across a window boundary; both are
  fixed (Collector, above covers the corrected mail-budget behaviour;
  `failure-report.php`'s own commit history in the site repo has the fix).
- **The VPS→Sentinel hop's honesty needed a sharper description than "at
  least once."** It is at-least-once specifically because the storage loop
  that POSTs each aggregate to Sentinel and then moves the segment to `done/`
  is unguarded against a mid-batch failure — a crash after some POSTs succeed
  but before the segment moves leaves it in `inbox/` for a full retry next
  run. `details.batch` (`"<segment-name>|<signature-hash>"`) is the key any
  downstream consumer — the dashboard view, a future aggregate reader — needs
  to dedup those replays; nothing on the VPS side does that collapsing itself.
- **The privacy bullet enumerates the union fields**, not just the base
  event shape — `runtime`/`dex`/`backend`/`exit` are each named as fixed
  category values, not summarized as "and a few more enums" (Privacy,
  above; `privacy.html` in the site repo carries the actual enumerated text).
- **The VPS-side re-validation regexes are `.fullmatch()`, not `^...$` with
  `.match()`.** Python's bare `$` matches before a single trailing newline
  even under `.match()`, so an `id`/`ts`/`client` value carrying a trailing
  `\n` would have passed the independent re-validation in `ingest.py` — the
  same class of anchoring bug the PHP collector's own `/D` flag exists to
  close (VPS ingest, above; `deploy/failure-ingest/ingest.py`).
- **The privacy page's effective date is bumped at deploy, not in this
  branch.** The page carries a comment marking where the date changes when
  the site is actually deployed, rather than a speculative date landing with
  code that has not shipped yet.
