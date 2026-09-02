# Fleet-as-GPU MVP — design

**Status:** approved in brainstorming (decision board 2026-09-02, founder accepted
all four recommended forks and delegated the scope boundary); not yet implemented.
**Date:** 2026-09-02
**Scope:** turn the Fleet-as-GPU *seam* — a `distill_session` queue nobody drains
unless a human types `firekeep night-shift` — into a *system*: a scheduler that
runs the drain where the free compute lives, a job catalog with two new job types
(stale-skill re-author, contested-verdict proposal), a server-side enqueue path
that feeds them, and a kill metric (approval rate per job type) so the whole thing
can be judged, kept or shut off on evidence. Every output stays a draft or a
proposal behind human review. Nothing here generates on the server.

## Problem

`docs/STRATEGY.md` names Fleet-as-GPU as *the* bet: the connected agents' own
machines are a swarm of strong models allowed to reason over the team's record.
What shipped is the deliberately-built seam and one drain:

- the `stop` hook enqueues one `distill_session` relay task per session
  (`client/firekeep_client/hooks/stop.py`);
- `firekeep night-shift` drains up to five of them against a **local** model
  (LM Studio / Ollama), writing `memory_learn` + `skill_create(status="draft")`
  attributed to the original session, leased, honestly counted, never raising
  (`client/firekeep_client/nightshift.py`).

Three things are missing, and they compound: **nothing schedules the drain** (the
live queue held 200 pending tasks on 2026-08-02, of which 193 were duplicates from
a dedup bug and the rest simply undrained); **there is exactly one job type**, so
the fleet's compute is only ever spent re-describing sessions; and **there is no
measurement** of whether what the fleet produces is any good — the dashboard's
Autopilot inbox shows drafts, not where they came from or how often a human
accepted them. Meanwhile the server-side nightly passes already *find* the work a
fleet could do: `skill_staleness_pass` flags active skills nobody has recalled in
90 days; `deep_contradiction_pass` marks unconfirmed contradictory pairs
`contested` and leaves them for a human. Both sit in the inbox until someone gets
to them. Nobody does.

## Decisions, and why

**1. The chassis is Night Shift, extended — not a headless-agent janitor.** The
Macbook proposal made a scheduled headless `claude -p`-style janitor the default
tier. That silently inverts Night Shift's shipped posture: session content never
leaves the machine, cloud models are refused by hard default, and the worker is a
stdlib JSON-in/JSON-out program that structurally cannot edit files or run shell.
The headless tier also carries an unresolved seat-billing/ToS question the brief
itself flagged. So the local-model worker grows a **job catalog**, and headless
stays a named later tier behind an explicit opt-in. The tool-restriction the
proposal asked for ("worker cannot edit/shell") is satisfied by construction.

**2. The scheduler is an opportunistic drain from `session_start`.** Auto-update,
symdex auto-index, docdex and maildex auto-sync already run this way: a detached
background process spawned from the hook, throttled by an atomic O_EXCL claim
file, one env-var off-switch, one banner line naming the command. A night shift
joins that row (`nightshiftdrain.py`, same `background.popen_kwargs()` spawn, same
claim shape, same `is_enabled` semantics). It adds one precondition the siblings
do not need: **a local LLM must be listening** — a ≤250 ms TCP connect to the
configured base or the two default ports (`:1234`, `:11434`) — so a machine
without a local model spawns nothing and prints nothing. Weighed against an OS
scheduled task (true "night", works with zero sessions, but a new install and
uninstall surface per OS) and a `--daemon` mode (a long-lived process to babysit).
Both remain possible add-ons; neither is needed to make the drain *happen*.

**3. The server enqueues; the client stays a pure drain.** The autopilot inbox
that lists stale skills and contested pairs is **admin-scoped**, so a client
worker holding a member key cannot read it, and inventing a non-admin worklist
endpoint would be a new API surface with its own scope questions. Cortex already
has the findings in hand inside the nightly passes. A new pass,
`fleet_enqueue_pass`, posts one relay task per finding. Relay has **no HTTP route
that creates a task** (`create_task` is reachable only through the
`relay_task_post` MCP tool), so this spec adds `POST /tasks`. Cortex reaches it the
way the briefing already reaches `GET /tasks`: `settings.RELAY_URL` +
`FIREKEEP_INTERNAL_KEY`.

**4. `POST /tasks` is gated by key registration, not by `relay:write`.** Verified
at the source: `FIREKEEP_INTERNAL_KEY` is minted with
`memory:write, session:read, eval:read, eval:write` and **no relay scope**
(`deploy/bootstrap-keys.sh:197`), and `ensure_env_key` never reconciles scopes on
an already-provisioned key, so a `relay:write` gate would 403 every existing
deployment with no migration path. It would also be inconsistent: relay's whole
write posture is the `FirekeepKeyAuthMiddleware` (a registered key), enforced
identically on `POST /dm/{agent_id}`, `DELETE /tasks/{id}`, `DELETE /presence/{id}`
and on every MCP tool including `relay_task_post` — only the Scope-session routes
gate on `relay:*`. Any agent key in the Keep can already create tasks over MCP; the
REST route grants nothing new. Documented in the guide so the choice is visible,
with the scope-reconciling bootstrap named as the follow-up that would let it be
gated later.

**5. Dedup is state-based, not marker-based.** Relay tasks have no idempotency and
expire after 7 days (`TASK_TTL_SECONDS`). A transition-only enqueue ("post when
newly stale") loses the finding forever if the task expires undrained; a
marker-only enqueue re-posts weekly and duplicates drafts a human has not acted on
yet. The pass therefore asks the store what is *true*: a stale skill is enqueued
only if **no skill with `reauthor_of == its id` exists in any status** and no
rejected-draft marker names it; a contested pair only if **neither side carries a
`proposed_verdict`**. A short live marker (`SET NX EX 7d`, cortex Redis) prevents
double-posting while a task is in flight and expires with the task. Drained work
never re-enqueues; expired work does; rejected work is retried once the 90-day
rejection marker lapses.

**6. Member-private points never enter a relay task.** Relay tasks are
Keep-global — no workspace scoping, readable by every registered key via
`relay_task_list`. The task `context` must carry the text the worker needs (there
is no non-admin read path back to the pair), so the pass excludes every point with
`visibility == "member"` outright (the docdex/maildex member-private tier —
`cortex/app/db/visibility.py`). Workspace-visible text is already recallable by
every agent key in the workspace, so the audience does not widen. Multi-workspace
Keeps get a server-side guard rather than client trust: `reauthor_of` must resolve
to a skill in the caller's workspace (404 otherwise), and `POST
/memory/contested/propose` validates the pair exactly as `resolve` does, so a
worker enrolled in a different workspace fails visibly instead of writing across
the boundary.

**7. The metric lives in a small ledger, because the store forgets.** There is no
approval timestamp anywhere (`stale_reviewed_at` is stamped by two unrelated
events — `digest.py` already documents the ambiguity) and **rejection is
deletion**, so an approval rate read purely from Qdrant would lose every rejected
draft and flatter the fleet. A `fleet:ledger:<job>` hash in cortex Redis counts
`produced / approved / rejected` (skills) and `proposed / resolved / matched`
(verdicts), all-time and per UTC day (400-day TTL) so the digest can window it.
Activation also stamps a real `approved_at` on the skill — the timestamp gap
closed on the way past. The metric is read-only visibility on the existing
Autopilot tab, exactly like every other section there.

**8. Fleet numbers ride the existing digest call; proposals ride the existing
inbox rows.** `tests/test_dashboard_autopilot.py` pins the Autopilot panel to
*exactly* three fetch URLs and zero write verbs — by design, so round-1's
read-only promise is mechanically checked. A fourth fetch would break the pin for
no gain: the digest is already "what changed this week" and per-job counts belong
in it; a proposed verdict is a property of a contested pair and belongs on that
pair's row. No new fetch, no new verb, the pin holds unchanged.

**9. Drafts and proposals only — the human resolves.** `POST
/memory/contested/propose` writes `proposed_verdict / proposed_rationale /
proposed_by / proposed_at` onto both points and nothing else. The only thing that
supersedes or coexists a pair remains `POST /memory/contested/resolve`, called by
a human. A re-authored skill is a **new draft** naming its origin in
`reauthor_of`; the stale original is untouched until a human activates the draft
and deprecates the old one. When the model judges a stale skill *still valid* or
*retire it*, it writes nothing — the verdict rides the relay task's result string
and the run summary, and the stale flag stays in the inbox for the human. That is
an accepted MVP limitation, stated in the guide: the honest place for that verdict
is the skill's inbox row, and it is a named follow-up.

**10. Default on, bounded, silent when it cannot help.** `FLEET_ENQUEUE_ENABLED`
defaults to true because every output sits behind review; the pass posts at most
`FLEET_ENQUEUE_MAX_PER_RUN` (20) tasks a night; the drain runs five tasks a shift
and only when a local model answers. A Keep whose members never run a local model
sees stale skills and contested pairs exactly as today, plus up to 20 pending
relay tasks that expire in a week.

## Out of scope (recorded, not built)

The Dreaming port onto the queue; capability tags on jobs; per-job token budgets
recorded in trace; the five other catalog jobs (handoff brief, doc-drift,
evidence-pack narrative, calibration review, merge-near-duplicates); the headless
agent tier; an OS scheduled task; writing a *still valid / retire* verdict back
onto the skill; any repositioning or marketing framing. The founder explicitly
asked that the README and firekeep.ai be updated to describe **what ships**, and
that is the extent of the site change.

## Components

### A. Relay — `POST /tasks`

`relay/app/routes.py::route_post_task` + `@mcp.custom_route("/tasks",
methods=["POST"])` beside the existing GET/DELETE. Body: `title` (required,
non-empty, ≤500), `assignee`, `assigner` (default `"unknown"`), `description`,
`priority` (default `"normal"`), `files`, `context`. Returns `201 {"status":
"created", "task": {...}}`; `400 {"error": ...}` on bad JSON or an invalid title;
`500 {"error": ...}` otherwise. Parity with the MCP tool is **three** side effects,
factored into one helper both paths call: `create_task`, the `tasks`-channel
broadcast, and the `coordination/task_created` replay emit. Auth: the key
middleware, deliberately no per-route scope (decision 4). Guide: the Task Queue
section of `docs/guides/relay-coordination.md` gains the REST line the sibling
sections already carry.

### B. Cortex — skills carry origin; a real approval timestamp; the ledger

- `SkillRequest` gains `origin_job: str | None` (`^[a-z][a-z0-9_]{0,63}$`) and
  `reauthor_of: str | None` (≤128). Both stored on the payload when present.
  `reauthor_of` must name an existing skill (any status) visible to the caller's
  workspace, else 404. `skill_create` (MCP) gains the same two optional params,
  forwarded only when truthy.
- `PATCH /skills/{id}` with `skill_status="active"` on a point whose status was
  not already active stamps `approved_at` (every skill, not just fleet ones).
- Ledger (`cortex/app/fleet/ledger.py`, async, takes the app's Redis client):
  `produced` on create with `origin_job`; `approved` on the draft→active
  transition of a point with `origin_job` (guarded by `approved_at` absence so a
  re-PATCH cannot double-count); `rejected` on DELETE of a **draft** with
  `origin_job` — which also sets `fleet:rejected:reauthor_stale_skill:<reauthor_of>`
  (90-day TTL) so the pass does not retry a rewrite a human just threw away;
  `proposed` on a first proposal for a pair; `resolved` + (when the human verdict
  equals the proposal) `matched` on resolve. Keys: `fleet:ledger:<job>` (all-time)
  and `fleet:ledger:<job>:<YYYY-MM-DD>` (400-day TTL). Ledger writes are best-effort
  and never fail the request that triggered them.

### C. Cortex — `POST /memory/contested/propose`

In `lifecycle.py` beside `resolve_contested`, `Depends(require_not_frozen)`,
`require_scope("memory:write")`, rate-limited like its sibling. Body:
`winner_id`, `loser_id`, `action: Literal["supersede","coexist"]`,
`rationale` (≤1000). Validation copies `resolve`'s 409 guard verbatim (the pair
must be mutually `contested_with`, read from `metadata`). Writes to both points:
`proposed_verdict = {"action": ..., "winner_id": <id or null>}`,
`proposed_rationale`, `proposed_by` (the caller's `X-Agent-Id`), `proposed_at`.
A second proposal overwrites the first and is not counted again. `resolve` learns
two things: clear the four `proposed_*` fields together with the contested flags,
and update the ledger (`resolved`, `matched` when
`action == proposal.action and (action == "coexist" or winner_id ==
proposal.winner_id)`).

### D. Cortex — `fleet_enqueue_pass`

`cortex/app/fleet/enqueue.py::fleet_enqueue_pass(client=None, settings=None,
redis_client=None, post=None, now=None) -> dict`, **sync** (the memory agent is a
sync Celery task under a SETNX lock; every relay POST uses `httpx.post` with a
short timeout, the `_embed_sync` precedent), registered in `memory_agent.py`'s
`passes` list **after** `skill_staleness` so it sees tonight's flags. It returns
`{"status", "reauthor_enqueued", "verdict_enqueued", "skipped_pending",
"skipped_private", "skipped_rejected", "capped", "failed"}` and never raises out
of the per-pass try/except.

Selection (one Qdrant scroll each, `limit=1000`, `must_not visibility=member`):

- **stale skills**: `memory_type=skill, skill_status=active, stale=True`; skip if
  the id appears in the `reauthor_of` set (a second scroll: skills with
  `reauthor_of` set, any status), or a rejection marker names it, or the live
  marker `fleet:enqueued:reauthor_stale_skill:<id>` is already set.
  Task: `title=reauthor_stale_skill`, `assigner=cortex-fleet`,
  `description=skill_id=<id> workspace_id=<ws or ->`, `context` = JSON
  `{skill_id, trigger, symptoms, content, domain, project, timestamp,
  last_recalled_at, stale_detected_at, access_count, skill_efficacy,
  skill_efficacy_n}` (content truncated to 6000 chars).
- **contested pairs**: `status=active, contested=True`; pair by
  `(min(id, contested_with), max(...))` so one task per pair; require **both**
  points present, active, workspace-visible and free of `proposed_verdict`; live
  marker per pair key. Task: `title=propose_contested_verdict`,
  `description=pair=<a>,<b> workspace_id=<ws or ->`, `context` = JSON
  `{a: {id, text, domain, timestamp, confirmed_count, contradicted_count},
  b: {...}, contested_at}` (each text truncated to 3000 chars).

Bounds: stop after `FLEET_ENQUEUE_MAX_PER_RUN` posts (`capped` counts the rest).
Gate: `FLEET_ENQUEUE_ENABLED` (default `True`) — disabled returns
`{"status": "disabled"}` before any I/O. A POST failure is logged at warning,
counted `failed`, and the live marker for that item is released so the next
night retries.

Config (`config.py`, documented in `docs/guides/cortex-configuration.md`,
mirrored in both compose files): `FLEET_ENQUEUE_ENABLED: bool = True`,
`FLEET_ENQUEUE_MAX_PER_RUN: int = 20`.

### E. Cortex — Autopilot surfaces

- `GET /autopilot/digest?days=N` gains `"fleet"`: `{"enabled": bool, "jobs":
  {"distill_session": {...}, "reauthor_stale_skill": {...},
  "propose_contested_verdict": {...}}}`. Skill jobs carry `window` and `all_time`
  blocks of `{produced, approved, rejected, approval_rate}` where
  `approval_rate = approved / (approved + rejected)` or `null` when the
  denominator is zero (a rate is never invented from a prior), plus all-time
  `pending = produced − approved − rejected`. The verdict job carries
  `{proposed, resolved, matched, match_rate}` with the same null rule. Read from
  the ledger only; fault-isolated like every other digest component.
- `contested_memories` inbox rows gain `proposed_verdict`, `proposed_rationale`,
  `proposed_by`, `proposed_at` (null when absent).

### F. Client — Night Shift job catalog

`nightshift.py` keeps its chassis (backend detection, Ollama native path,
cloud refusal, personal-mode no-op, leases with fencing tokens, honest counting,
transient-vs-malformed handling, the `run()` injection seams) and gains a
dispatch table `JOBS` keyed by relay task title. `run()` lists each title FIFO
(exact-title `relay_task_list`, oldest first) under one `max_tasks` budget,
distill first, and hands each task to its handler. Summary dict gains
`reauthored`, `proposed`, `noop`. New jobs lease `fleet.<task_id>`; distill keeps
`distill.<task_id>`.

- **`reauthor_stale_skill`**: evidence = the task's `context` JSON. Prompt asks
  for STRICT JSON `{"verdict": "rewrite"|"still_valid"|"retire", "reason": str,
  "skill": {trigger, symptoms, steps, gotchas, domain} | null}`; `rewrite` with a
  non-empty trigger → `skill_create(status="draft",
  origin_job="reauthor_stale_skill", reauthor_of=<skill_id>, agent_id=<worker>)`
  → task completed `"night-shift: re-authored draft awaiting review"`, counted
  `reauthored`. `still_valid` / `retire` → no write, task completed with the
  verdict and reason, counted `noop`. An unconfirmed `skill_create` (older server,
  cross-workspace 404, in-band error) **fails the task** — never a silent
  completion.
- **`propose_contested_verdict`**: evidence = both memories from `context`.
  Prompt asks for `{"action": "supersede"|"coexist", "winner_id": <one of the
  two ids or null>, "rationale": str}`; a `supersede` naming an id outside the
  pair is malformed (one retry, then failed). Written via `transport.post_json`
  to cortex `POST /memory/contested/propose` with the member key headers from
  `resolver.resolve("cortex")` (REST only — there is no MCP tool for propose, as
  there is none for resolve). Confirmed → task completed, counted `proposed`.
- Unknown titles are never listed, so an older client and a newer server
  coexist: new tasks wait for a new client or expire and re-enqueue.

The CLI (`firekeep night-shift`) prints per-job counts and, after every run,
writes `state.write_scratch("night_shift_last", {at, counts, error})` (7-day
TTL) — the record the next session start reads back.

### G. Client — opportunistic drain at session start

`client/firekeep_client/nightshiftdrain.py`, the fifth entry in
`session_start.py`'s nudge chain (after the dex syncs; the briefing is what the
user is waiting for):

- `is_enabled(cfg)`: off when `FIREKEEP_NO_AUTO_NIGHTSHIFT` is set to anything but
  `"", 0, false, no, off`, or `[nightshift] auto_drain` is explicitly false —
  identical semantics to the four siblings.
- `local_llm_listening()`: TCP connect (≤250 ms) to the host:port of
  `FIREKEEP_NIGHTSHIFT_LLM_BASE` if set, else `127.0.0.1:1234` then `:11434`.
  False → return `""` with no spawn and no line.
- `maybe_spawn(cfg, slot)`: atomic O_EXCL claim `night_shift.<slot>` where
  `slot = floor(now / interval)` and `interval = [nightshift] auto_drain_hours`
  (default 6); on first claim `Popen([<firekeep.exe next to sys.executable>,
  "night-shift", "--max", "5"], **background.popen_kwargs())`; a failed spawn
  unlinks the claim. Returns True when a shift is in flight.
- `drain_nudge(cfg) -> str`: on spawn, `\n\n[firekeep] night shift draining the
  fleet queue in background (local model; disable with
  \`FIREKEEP_NO_AUTO_NIGHTSHIFT=1\`)`. Independently, if `night_shift_last`
  records unreported drafts or proposals, one line: `[firekeep] night shift: N
  re-authored skill draft(s) and M verdict proposal(s) await review — dashboard →
  Autopilot`, then the record is marked reported so it prints once.
- Personal mode: the dispatcher already short-circuits `session_start` while
  bypassed, and `nightshift.run()` refuses independently — two layers, no new
  check needed.

### H. Dashboard (`dashboard/index.html`)

- `renderAutopilotDigest` gains a **Fleet** table under the digest counts: one
  row per job type — produced / approved / rejected / approval rate (window and
  all-time), the verdict row showing proposed / resolved / match rate; `null`
  rates render as `—` with a "not enough verdicts yet" title, never `0%`.
- `apContestedRow` shows, when present: `Night Shift proposes: keep <short id>,
  supersede <short id>` or `… both true (coexist)`, the rationale, and who/when.
  Still read-only: resolving remains an API call for now (there is no contested
  UI anywhere in the dashboard today — a pre-existing gap, out of scope).
- Incidental fix while in that block: `low_efficacy_skills` is emitted by the
  API and documented but missing from `AUTOPILOT_SECTIONS`, so the headline
  `total_actionable` already counts rows the panel never lists. Added.
- The read-only pin (`tests/test_dashboard_autopilot.py`) must pass unchanged:
  same three fetch URLs, no write verbs.

## Data flow, end to end

Night: `memory_agent` → `skill_staleness_pass` flags → `deep_contradiction_pass`
contests → `fleet_enqueue_pass` scrolls, dedups against the store, `POST
{RELAY_URL}/tasks` ×≤20 (internal key). Morning: a developer opens Claude Code →
`session_start` → local model port answers → claim → detached `firekeep
night-shift --max 5` → lists `distill_session`, `reauthor_stale_skill`,
`propose_contested_verdict` FIFO → lease → local LLM → `skill_create(draft,
origin_job, reauthor_of)` / `POST /memory/contested/propose` / `memory_learn` →
task completed → `night_shift_last` written. Cortex: ledger `produced` /
`proposed`. Human: dashboard → Autopilot → digest Fleet table + contested rows
with proposals → Skills tab *Activate* (→ `approved_at`, ledger `approved`) or
*Delete* (→ ledger `rejected`, rejection marker) / `POST
/memory/contested/resolve` (→ ledger `resolved`, `matched`). Next night: the
store says the draft exists / the proposal exists → nothing re-enqueued.

## Error handling

Every new seam fails toward *today's behaviour*: the enqueue pass swallows and
counts (a relay outage means no fleet tasks tonight, nothing else); the ledger is
best-effort inside the request that triggered it; the drain nudge never raises
and never blocks the briefing (a socket probe with a hard timeout is its only
I/O); the worker's existing contract holds — transient LLM loss defers and stops
the shift, malformed output retries once then fails the task visibly, an
unconfirmed write fails the task rather than completing it. A new client against
an old cortex fails `reauthor_stale_skill` tasks loudly (unknown MCP argument →
in-band error string → task failed with that text), and an old client simply
never sees the new titles.

## Testing

- **Relay**: `POST /tasks` 201 with all three side effects (create, broadcast,
  replay emit — spy the helper), 400 on empty/oversize title and bad JSON, 401
  through the real middleware without a key, and parity: the MCP tool and the
  route share the helper (one test asserts both call it).
- **Cortex**: `SkillRequest` accepts/rejects `origin_job` patterns; `reauthor_of`
  404s across workspace and stores otherwise; `approved_at` stamped once; ledger
  counters per transition including the double-PATCH guard and the rejection
  marker; `propose` 409 on a non-pair, stores four fields on both points, second
  proposal not double-counted; `resolve` clears proposal fields and records
  `resolved`/`matched` for supersede-match, supersede-mismatch and coexist;
  `fleet_enqueue_pass` with a fake Qdrant client + fakeredis + a recording `post`:
  selects stale/contested, excludes member-private, skips pending drafts, existing
  proposals, rejected markers and live markers, pairs once, truncates context,
  caps per run, releases the marker on POST failure, `disabled` before I/O;
  digest `fleet` block math including the null-rate rule; inbox rows carry
  proposal fields; memory_agent registers the pass after staleness.
- **Client**: `test_nightshift.py` grows per-job cases through the existing
  `_Recorder` seam — FIFO across titles under one budget, `fleet.` lease id,
  reauthor rewrite → draft with `origin_job`/`reauthor_of`, `still_valid` →
  `noop` with no write, unconfirmed `skill_create` → failed task, propose →
  `post_json` to `/memory/contested/propose` with member headers, out-of-pair
  winner → malformed path, unknown-title isolation; `test_nightshiftdrain.py`
  mirrors `test_docdexsync.py` (spy `Popen`, `_forbid_spawn`, claim rotation by
  slot, socket probe monkeypatched, env/config gates, banner text, reported-once
  record); `test_session_start.py` asserts the nudge is in the chain via the
  spawn-seam pattern; `test_cli.py` covers the new summary line and the
  `night_shift_last` write.
- **Repo**: `tests/test_dashboard_autopilot.py` unchanged and green; the
  docs-vs-code guards (compose env ↔ config defaults, client-kit forbidden
  tokens) green; a new test pins `AUTOPILOT_SECTIONS` ⊇ the API's section keys
  so the `low_efficacy_skills` class of drift cannot recur.

## Documentation (the consistency checklist, applied)

`cortex/app/mcp_server.py` (skill_create params), `cortex/app/lifecycle.py` +
`skills/api.py` + `autopilot/{api,digest,inbox}.py`, `relay/app/{routes,
mcp_server}.py`, `docker-compose.yml` + `docker-compose.office.yml`
(`FLEET_ENQUEUE_*`), `docs/guides/client-kit.md` (Night Shift section rewritten:
catalog, auto-drain, config), `docs/guides/knowledge-autopilot.md` (proposals in
§3, Fleet section + ledger + honesty rules), `docs/guides/relay-coordination.md`
(REST line + the auth decision), `docs/guides/cortex-api-endpoints.md` and
`cortex-configuration.md`, `client/firekeep_client/cli.py` help text,
`dashboard/index.html`, root `CLAUDE.md` (kit summary + guide table),
`README.md` (feature table, kit bullets, dashboard Autopilot row), and the
firekeep.ai `docs.html` ("knowledge lifecycle, on autopilot" paragraph and the
CLI reference row) — deployed by the documented backup-then-tar-over-SSH recipe.
