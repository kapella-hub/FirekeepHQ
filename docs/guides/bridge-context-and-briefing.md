# Bridge shadow residency and the briefing endpoint

> Moved out of the root `CLAUDE.md`, which is a prompt prefix loaded into every
> session. This content is reference and decision history: read it when you are
> working on this area, not on every task. Nothing was reworded in the move.

## Briefing Endpoint (Cortex, SP1b-server)
Server-side pre-flight aggregator that consolidates the checks the retired `briefing.sh` bash script previously assembled into a single authenticated call. `rendered` is a plain-text pre-flight briefing intended to be authoritative for thin clients; it replaces the now-retired `briefing.sh` bash assembly; the `session_start` hook core is a thin fetch of this endpoint.

**REST Endpoints (on Cortex :8100):** `GET /briefing?agent_id=&goal=&project=` — returns an envelope `{generated_at, server_version, agent_id, goal, project, briefing_id, degraded, sections{...}, instructions, rendered}` covering 11 sections (`environment`, `tasks`, `bulletins`, `quality`, `strategy_tips`, `cross_agent`, `skills`, `vault`, `resumable_sessions`, `discipline`, `dlq`): 7 assembled in-process on Cortex (quality, strategy_tips, cross_agent, skills, vault, discipline, dlq), 4 via outbound fan-in (environment ← Sentinel, tasks + bulletins ← Relay, resumable_sessions ← Bridge). Every outbound call carries the internal key. Each section always reports `{status: ok|empty|unavailable, error, data}`; `degraded=true` if any section is unavailable, but the endpoint returns HTTP 200 whenever the briefing host itself is up — fail-loud per-section, never fail-open. Gated with `require_scope("session:read")` as a deliberate aggregator-level check; individual sub-sections do not re-check their own per-scope permissions. The `vault` section is populated only for callers whose scopes include `admin` or `*`, otherwise it reports `omitted_reason: insufficient scope`. Router: `cortex/app/briefing/`.
**Relay REST additions (on Relay :8050):** `GET /tasks?assignee=&status=&limit=` → `{tasks, count}`; `GET /bulletin?limit=` → `{posts, count}` — thin wrappers over `list_tasks`/`read_bulletin` added to feed the briefing aggregator, behind SP1a auth.
**Sentinel REST additions (on Sentinel :8060):** `GET /environment` → `{status, redis, collectors, event_count, healthy, containers, container_count}` (named `/environment` rather than `/health/full` because the auth skip-list check is a prefix match on `/health`); `GET /events?source=&event_type=&severity=&limit=` → `{events, total_in_stream, returned}`.
**Config:** `RELAY_URL` (default `http://relay:8050`), `SENTINEL_URL` (default `http://sentinel:8060`) — briefing aggregator's outbound targets, passed to both cortex-api and cortex-mcp in docker-compose (mirrors `BRIDGE_URL`).
**Bridge:** `ctx_start_session` (MCP tool) and `SessionManager.start_session()` gained an optional `briefing_id`, threaded into the session hash to link a briefing to the session it originated — closing the strategy-tip A/B feedback loop (see carryover note in `docs/superpowers/specs/2026-07-08-team-activity-hub-master-design.md`). `GET /briefing`'s `instructions` field renders the server-minted `briefing_id` into every branch's suggested `ctx_start_session(goal=..., briefing_id='<id>')` call, so an agent that follows the printed instruction supplies it; the join in `GET /patterns/effectiveness` (see Feedback Loop below) only closes for sessions whose agent actually passed it along — Bridge has no way to force that, it's a documented instruction, not an enforced contract.

**Session attribution (Living Instructions round 2 — `bridge/app/mcp_server.py::_attribution_from_headers`, 2026-08-12).** `ctx_start_session` reads five `X-Firekeep-*` headers via the existing `get_http_headers` fallback pattern (the `X-Agent-Id` precedent, case-insensitive) and `start_session()` persists them on the session hash as five fields: `runtime`, `client_version`, `instr_rendered`, `instr_expected`, `instr_gateway` — absent → `""`, the `briefing_id` shape, because an unattributed session is a normal state, never an error. The headers are attached by the gateway and hook cores from client 0.1.41 (see `client-kit.md`'s Instruction attribution section for what each hash means); their trust level is exactly `X-Agent-Id`'s — **untrusted observability labels, never gates** — so nothing in Bridge authorizes, branches, or filters on them. They also ride the `session_start` replay payload the same way `briefing_id`/`tags`/`project` already do — only the headers that actually arrived, so absence in the payload is absence on the wire, not a defaulted `""` — which is deliberately the ONLY place eval attribution reads them: `compute_session_eval` loads the timeline anyway, so no eval-time Bridge call is added (see `replay-evals-patterns.md`'s EvalResult attribution entry). Sessions from clients predating 0.1.41 carry no headers and read as unattributed — honestly; nothing backfills.

**Session targeting (cross-terminal clobber fix, 2026-08-12 — `bridge/app/mcp_server.py::_header_session_id`).** `ctx_complete_session`, `ctx_abandon_session`, `ctx_get_shadow` and `ctx_update` resolve their target session with the precedence **explicit param > `X-Session-Id` connection header > active pointer** (mirroring Cortex's `_resolve_identity`; `ctx_update`'s public tool signature is unchanged — the header threads through a new optional `SessionManager.update(session_id=)`). `complete_session`/`abandon_session` additionally refuse a session whose owner differs from the caller, mirroring `resume_session` (whose docstring used to *claim* this check existed — it did not, and a parallel terminal sharing the machine's agent_id completed a sibling's in-flight session on 2026-08-11 via the shared `nb:active:{agent_id}` pointer). The client side is load-bearing: the shim's bridge client sources `X-Session-Id` **only** from what its own process observed (never the shared per-agent disk stash — a fresh terminal falling back to it would deliver its sibling's live session to a destructive call), injects that id into no-arg complete/abandon, and the stop-hook's completion nudge instructs completing only a session the model itself started or resumed. Honest residual: a no-arg, no-header complete from a terminal that never started a session still resolves via the shared pointer, and ownership cannot distinguish same-agent siblings — narrowed to a case the instruction layer now tells models not to create, not eliminated. A header-named session that no longer exists refuses (`Unknown session`) instead of materializing a ghost hash.

## Prior art at the moment of intent (Bridge — `bridge/app/prior_art.py`)

`ctx_start_session` is the only call in the stack that knows what an agent is
about to do *before* it does it, and the whole memory product otherwise waits to
be asked. That is the failure this closes: an agent that does not know the team
has already built the thing has no trigger to call `memory_recall`, so the
knowledge stays retrievable and never retrieved — the same shape as the "deploy
to my vps" incident that produced the MCP `instructions` block. Declaring a goal
*is* the trigger, so the answer is pushed into the tool result rather than
offered.

Two legs run concurrently under one deadline (`NB_PRIOR_ART_TIMEOUT_SECONDS`,
2.5s). **Team memory** POSTs `/memory/recall` on Cortex with the goal text,
`format: "raw"`, the internal key, and `trigger: "prior-art"` — the marker that
lets the compliance measurement separate pushed recall from deliberate recall
(the `prompt-hook` precedent). Matches are filtered on `metadata.raw_score`
against `NB_PRIOR_ART_MIN_SCORE` (0.55), never on `score`, which
`_min_max_normalize` pins to 1.0 for the best entry in any result set — a floor
on that number filters nothing, measured live (see `cortex/app/main.py`). The
floor sits *above* proactive recall's 0.35 on purpose: that one feeds the
shadow, which an agent reads when it goes looking, while this one is pushed
unasked into the first thing it reads in a session. **In flight now** lists
Bridge's own `active` sessions belonging to OTHER agents, newest first, capped at
three, with no similarity filtering at all — teams are small, a wrong omission
costs a duplicated week and a wrong inclusion costs one line.

The response gains `prior_art: {memories, in_flight}` and `prior_art_text`, the
rendered block, always together and only when non-empty. Bridge tools return
dicts (FastMCP serialises them), so `prior_art_text` *is* the rendered text an
agent reads; it leads with the instruction rather than the data, because a block
that merely listed matches is a fact a model is free to skim:

```
[prior art] the team may have been here before — recall before building:
- Shipped Keep Backup end-to-end... (raw 0.63)
in flight right now: agent-x — "harden backup retention" (2h ago)
```

**Fail-open is structural, not defensive.** Assembly runs strictly *after*
`start_session` has committed, so nothing in it can cost the caller the session
it asked for; each leg swallows its own errors and returns `[]`, so a dead
Cortex still yields the in-flight line; both are wrapped in `asyncio.wait_for`
(httpx's own timeout applies per phase, so a host that accepts and then stalls
can spend it twice); nothing found returns `{}` rather than empty lists, making
"nothing to say" and "Cortex was down" the same shape on the wire; and the call
site has a final `try/except` under all of it. Bridge already carried
`NB_FIREKEEP_API_URL` and `NB_FIREKEEP_API_KEY` (the SP1a internal key) for
proactive recall and the eval trigger — no new compose env. `NB_PRIOR_ART_ENABLED`
gates it. Guards: `bridge/tests/test_prior_art.py` (23 tests, including the
byte-exact trigger, the pinned block, and a hanging Cortex bounded by the
deadline); the suite's `disable_prior_art` autouse fixture keeps every other
session-start test off the network.

## Shadow Residency Contract (Bridge — Phase C, `bridge/app/residency.py`)
`ctx_get_shadow()` with no argument is a FULL restore, byte-identical to what it has always returned. **That is the default and it is always correct.** A caller may opt into a delta by passing back `since=<shadow_cursor>` — the opaque cursor from an earlier response in the SAME conversation — which asserts exactly one thing: *the earlier shadow is still visible in my context*. `residency.py` is pure functions, no I/O; the wiring is in `mcp_server.py`'s `ctx_get_shadow`.

**Every doubtful path returns the full document — by construction, not by inspection.** There are **seven** fail-safes in `filter_since`, and each one falls back to a complete restore: no cursor; a cursor that does not decode as ours (bad base64/JSON, wrong version, wrong field types); a cursor minted for a different `session_id`; an **unreadable epoch**; a **stale epoch** (precompact bumped it); a cursor with no high-water mark; and a **filterable container of the wrong shape** (M1). Behind those sit two structural guarantees that make the claim total rather than enumerated: `ctx_get_shadow` wraps the whole filter-and-render pair in a `try/except` that answers any unenumerated failure with `assemble_shadow(data)`, `delta: False` and **no cursor** (the C2 shape); and `assemble_shadow` itself is **total** — no session shape can make the renderer raise, because a fallback that can itself raise is not a floor.

The seventh fail-safe earns its place in `residency.py` rather than at the call site, which is where the task that added it initially proposed putting it. `residency.py`'s element-level `isinstance(e, dict)` guards protect against a bad *entry*, not a bad *container*, and the two container defects fail in opposite directions: a list-shaped `files` **raises** on `.items()`, but a dict-shaped `decisions` **raises nothing** — it iterates the dict's KEYS, keeps the bare strings, discards every value and reports `0` omitted, producing a delta that looks correct while having thrown the content away. No `try/except` can see that one, which is why detection has to live in the function that knows the shapes. Symmetrically, the call-site guard cannot be dropped in favour of the shape check alone: a guard only covers the shapes somebody enumerated. Neither is reachable from today's writers (`get_session_data` builds both containers by construction), so this is a floor, not a live fix — worth having because `ctx_get_shadow` is the post-compaction lifeline, called precisely when an agent has lost its working state, where a traceback is strictly worse than any token cost. Guards: the three `TestShadowDelta` container/render cases in `test_shadow_delta.py` and `TestAssembleShadowIsTotal` in `test_shadow.py` (19 hostile shapes × raises/preserved, plus a byte-identical no-regression check on well-formed data). **Totality is not the same property as content preservation, and the renderer originally delivered only the first.** `_rows` refuses to iterate a dict-shaped CONTAINER, but the ENTRY renderers then projected an unrecognised dict through `.get(key, "")` — so a dict-shaped `decisions`/`progress` rendered `- [] `, `proactive_memories` rendered `- [0.00] `, and a `files` info-dict carrying no `summary` key rendered `- **a.py** — `: four rows that exist, satisfy "not denied", and contain nothing. `_is_recognised(entry, *keys)` closes it — a dict carrying NONE of the keys a branch reads is rendered literally, while one carrying at least one is the shape and is projected as before (byte-identical on well-formed data; a file entry's `last_action` is still dropped by design). The test that was supposed to catch this asserted only that the section's denial placeholder was ABSENT, which `- [] ` satisfies; it now asserts a fragment of the malformed value is PRESENT. A document that neither denies the content nor contains it is the same loss as a denial, only harder to see. The failed epoch read is the subtle one and is handled twice: `ctx_get_shadow` returns early without ever calling `filter_since` **and mints no cursor at all** — a response carrying no cursor cannot seed a later delta, which is the safe outcome on a session whose epoch was never readable — while `filter_since`'s own `epoch is None` guard is defence-in-depth for a future caller that forgets. `""` (never bumped) and `None` (read failed) must never collapse into each other: `""` is a real, matchable epoch that every pre-first-compaction cursor legitimately carries, so folding a read FAILURE into it would silently match a stale cursor to a failed read and hand a delta to an agent that had just lost its context.

**Comparison is inclusive (`>=`)** — the boundary entry is re-sent, because duplication beats omission. `_keep_entry` keeps an entry on any doubt: unknown stamp, unparseable stamp, or a naive-vs-aware comparison that raises. It parses with `datetime.fromisoformat` rather than comparing strings, which removes a silent-drop class of its own — a naive stamp (`2026-07-30T10:00:00`) is a lexicographic PREFIX of the same instant carrying an offset, so raw string comparison sorts it LESS and would drop an entry that is actually newer-or-equal.

**Timestamps, not indices.** `decisions`/`progress` are stored `LPUSH` + `LTRIM` (`bridge/app/session.py`), so the OLDEST entries are evicted. "I have seen the first 47" stops meaning anything once that window shifts and would silently SKIP entries; timestamps are stable under eviction, because eviction only ever removes entries the agent already received.

**What can never be filtered:** `scratch` is always sent in full (no per-entry timestamp exists to filter on) and `proactive_memories` passes through untouched (it is replaced wholesale, not appended). Neither is ever counted as omitted, because nothing was omitted. The plan is withheld only when its `plan_sha` matches, and `omitted["plan"]` additionally requires that a plan actually exists — a session that never had one matches its own empty hash trivially, and telling the reader "your plan is unchanged" about a plan that never existed is a false claim.

**The omission must be legible inside the document.** `assemble_shadow` gained a keyword-only `omitted=` so a withheld section renders a line SAYING it was withheld instead of the "*No decisions recorded*" placeholder — an affirmative denial rendered over the agent's own work. `omission_notice` builds the same statement for the response's `note` field: it names what was withheld, says it still exists, and says to call `ctx_get_shadow()` with no arguments for the full document. An agent reading a delta must never be able to conclude the omitted content DOES NOT EXIST — that inference is the degradation, not the omission.

**Cursors are always minted from the FULL data**, never the filtered copy: a cursor describes what the caller now holds in total, not what this response carried. The mint is exception-guarded (a malformed timestamp must not crash the post-compaction lifeline — a response with no cursor is a safe dead end). `ctx_resume_session` mints a cursor too but deliberately takes no `since` parameter and emits no `delta` key: a resumed session is by definition one the agent cannot vouch for, and an always-false flag would invite a caller to start passing `since` to a tool that must never accept it.

**The client may never supply `since`.** Passing it is an assertion about what is still in the model's context, which a separate process cannot observe — so no hook, shim, or sidecar sends it. Only the agent can, which means the instruction has to reach the runtime's instruction layer, not just the tool docstring (the `decision_board` lesson): `adapters/base.py`'s rendered firekeep instruction block carries the "pass `since` ONLY if the earlier shadow is still visible in your context; if unsure, omit it" line alongside the tool's own `Args:` documentation.

**Measured before shipping:** 39.8% aggregate saving, 50.7% on sessions ≥1000 tokens, across 26 real sessions with a real tokenizer (`tiktoken` cl100k_base, not `chars/4`), simulating a cursor 75% of the way through each session. 80% of the saving comes from filtering decision/progress/file entries and only 20% from omitting an unchanged plan; the unfilterable scratchpad is just 14.1% of all shadow tokens, which is what retired the concern that it would dominate and erase the gain. A first pass measured **0.4%** and would have cancelled the phase — a bug in the measurement, not the design (it split sections on every `### ` line, so agent-authored markdown headings inside scratch values were mis-counted as unfilterable sections). Full write-up and caveats in `docs/HISTORY-NOTES.md`. Guards: `bridge/tests/test_residency.py`, `test_shadow_delta.py`, `test_shadow_omission.py`.

## Session Reaper (Bridge, Knowledge Autopilot round 1 — `bridge/app/reaper.py`)

A session whose agent died or walked away never calls `ctx_complete_session`,
so it sat `status="active"` forever: no TTL, never distilled, never evaluated —
it did not fail, it *vanished*. Since OWM's load-bearing failure signal is
Bridge's `abandoned` status, the sessions most likely to have gone badly were
exactly the ones that never counted (the outcome-degeneracy finding in
`memory-and-recall.md`).

The reaper is a second lifespan worker (same loop shape as the distiller):
every `NB_REAPER_INTERVAL_SECONDS` (3600) it scans the `nb:sessions` index —
which is scored by *last activity*, so a long session still being written to is
never a candidate — for entries idle beyond `NB_REAPER_IDLE_HOURS` (72;
conservative because reaping is not usefully reversible), capped at
`NB_REAPER_MAX_PER_PASS` (500) per pass so the first pass on an old deployment
drains its backlog oldest-first across hourly passes instead of firing an eval
POST per session in one burst, and abandons each
`active` one through `SessionManager.abandon_session` (owning pointer cleanup,
TTL and the `session.abandoned` event) followed by the shared `after_abandon`
helper (the `session_end` event with `outcome="partial"` — payload gains
`reaped: true` on this path only — plus the eval trigger). Dangling index
entries are self-healed; `paused`/`completed`/`abandoned` are skipped, each
already carrying somebody's decision. Per-session fault isolation: one racing
session never strands the sweep. `NB_REAPER_ENABLED=false` no-ops per pass;
the loop registration is unconditional, the stack's standard gating idiom.

Deliberate tradeoff, documented where it bites: abandonment does not distill,
so a reaped session's content is discarded when its TTL lapses. The outcome
signal is the point of this round; recovering knowledge from failed sessions
is future work. Guards: `bridge/tests/test_reaper.py` (19 tests, including
the byte-identical human-abandon payload and the per-pass cap).
