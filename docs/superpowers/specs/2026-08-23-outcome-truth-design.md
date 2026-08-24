# Outcome truth — design

**Status:** approved in review (two adversarial verification passes over the proposal,
12-agent codebase verification 2026-08-22/23); revised 2026-08-23 eight times after
external adversarial review rounds (9 findings; 7 cross-task defects; 8 defects incl.
scope-leak and impersonation blockers; 7 defects incl. auth-off hint breakage and the
claim-protocol hole; 7 defects incl. takeover re-impersonation and the stale-claim
reproduction; 6 defects incl. bridge-side grade erasure, non-snapshot-stable paging,
and the tip-shown dominance bypass; 7 defects incl. terminal-side-effect impersonation,
lost authoritative redundancy, non-executable gates, stale webhook fallback, and
serial hydration; and the final sibling-terminal abandon-impersonation gap) — every
finding verified against code before adoption. **Revision: v9.** Not yet implemented.
**Date:** 2026-08-23
**Scope:** one subsystem — the session completion → eval → grading chain. Artifact
receipts (PR2), the experiment rebuild + PatternCard provenance (PR3), and any
instruction-text nudge to drive adoption (PR4) are separate specs.

## Problem

Session "success" currently means "the completion RPC worked", not "the task worked".
`ctx_complete_session` stamps every completion's `session_end` replay event with the
hard-coded literal `outcome="success"` (`bridge/app/mcp_server.py:657-661`), the agent's
free-text summary rides along unread, and every downstream success signal derives from
that stamp:

- `_failure_rate` reads 0.0 for essentially every session (`cortex/app/evals/scorers.py:151-173`
  documents the measured state: ~48 events per session, ONE carrying an outcome —
  "the session reporting its own success").
- `owm.session_success` grades from that failure_rate (≤0.2 → success), so
  `owm_efficacy` converges toward "everything worked" (`cortex/app/owm.py:46-63`).
- Living Procedures Tier B inherits the same grade via `harden._resolve_outcome`.
- The pattern engine coerces every ungraded session — even the reaper's `"partial"` —
  to `outcome="success"` in stored SessionFeatures (`cortex/app/patterns/extractor.py:96-105`)
  and fabricates "success" for sessions with zero outcome-tagged events.

The scorers.py docstring is the standing decision record that deliberately left
`_failure_rate` at 0.0 — this spec is the measurement pass that supersedes it.

## Decisions, and why

**1. The agent grades the task at completion, optionally.** `ctx_complete_session` gains
`task_result` (`"success" | "partial" | "failure"`) and `task_evidence` (≤10 claims,
≤300 chars each). `outcome` keeps its free-text role; the grade is never inferred from
prose. An invalid grade STRING is coerced to ungraded with a note — completion never
fails over a bad grade value. Evidence without a grade is dropped.
**Invariant scope:** wrong-TYPED arguments are rejected by FastMCP validation *before
the tool body* — a recoverable client error, session intact (verified by wire probe on
fastmcp 3.1.1). Wire tests pin both halves. A verified-principal mismatch is an
authorization failure, NOT a bad grade value: D13 refuses the whole terminal mutation.

**2. The grade/source pair is atomic — enforced, not asserted.**
`task_result_source="self_reported"` is server-stamped, `Literal["self_reported"] | None`,
and the pair has ONE implementation in `cortex/app/evals/models.py`:
`recognized_grade_pair(result, source)` (pair or `(None, None)`),
`grade_from_events(events)` (terminal-event scan), and `binary_outcome(result)`
(success/failure pass through; partial/None → "unknown" — the projection helper, so no
consumer inlines membership checks). Enforcement points: (a) `EvalResult` carries a
`model_validator(mode="before")` over the RAW mapping — `mode="after"` would never run
for invalid literals because field validation raises first (verified under Pydantic
2.12.5), so normalization happens before field parsing: junk values and mismatched
pairs degrade both fields to None instead of failing the whole record; (b) raw-dict
consumers that bypass the model (`owm.session_success` reads stored JSON) call the
normalizer themselves; (c) BOTH bridge terminal events (`session_end`, `session.completed`)
carry the full pair. A sourceless grade is not evidence anywhere. `grade_from_events`
guards `isinstance(payload, dict)` before `.get` — a non-empty list/string payload
degrades to `(None, None)` (its junk-handling contract) instead of raising into the
eval catch-all and DLQing the whole computation (round-6 finding 6). Bridge likewise
treats an existing session-hash grade as authoritative only when the session has a
non-empty verified `owner_member`, its value is in the producer vocabulary, AND its
stored source is exactly `self_reported`; it never
manufactures a source for sourceless/corrupt state, and a later valid first grade can
repair that unrecognized pair.

**3. Ungraded and legacy read as unknown, never success.** The `session_end` emit kwarg
carries the grade or `None`. `session_success` reads ONLY the recognized pair (plus
`abandoned → False`): success → True, failure → False, partial/sourceless/absent →
None. The failure_rate heuristic and eval-`outcome` fallback are removed. Accepted
consequences: OWM's stale-reset neutralises legacy `owm_efficacy`; Tier B reports
"insufficient outcome signal" until graded volume accumulates.

**4. The reaper's manager-level abandon path is byte-identical.**
`bridge/app/reaper.py` continues to call `SessionManager.abandon_session` directly and
then `after_abandon`; neither production path changes, and its direct-path tests in
`bridge/tests/test_reaper.py` stay unchanged. Abandoned sessions still grade False via
the existing `bridge_status` override. D13 adds verified-principal authorization only
to the public `ctx_abandon_session` wrapper; the one public-tool fixture that happens
to live in `test_reaper.py` must gain the wrapper's new read setup.

**5. The replay event contract is untouched; the reader gains one additive helper and
its existing batch reader is pipelined.** No new event types or outcome values;
`replay/models.py` untouched. `replay/reader.py` gains
`get_session_event_ids(r, sid, *, limit)` — the newest `limit` event IDs oldest-first
(one `zrange(idx, -limit, -1)`) — which D7's lift SNAPSHOTS once and then hydrates
locally, because `get_session_timeline` paginates oldest-first with post-pagination
filters (a filtered read returns `[]` for any session longer than the limit) AND live
rank-relative paging is not snapshot-stable (D7). The current `get_event_batch` issues
one `GET` plus one `XRANGE` serially PER ID: the 5,000-ID cap can therefore mean about
10,000 network round trips, while Bridge times the compute request out at 15 seconds.
It is rewritten contract-preservingly as one `MGET` plus one pipeline of indexed
`XRANGE`s per 200-ID hydration window (requested order and duplicate behavior remain
unchanged). Existing replay behavior assertions remain green; the batch benchmark is
extended with a real-Redis budget test pinning the 5,000-ID hydration path comfortably
below the trigger timeout.

**6. `_failure_rate` is freed and made symmetric** — `None` on no-outcome input, docstring
records the supersession. Policy's `.get("failure_rate", 0.0)` default already treats
absence as allow.

**7. EvalResult carries the grade; the lift snapshots then scans BACKWARD until it
finds it.** New
Literal-typed optional fields (old records parse; the D2 validator normalizes every
parse). Precedence: (1) the `task_result_hint` on the eval-trigger request; (2)
`find_terminal_grade(replay_redis, session_id)` — which SNAPSHOTS up to the last 5,000
event IDs once (`get_session_event_ids`), then hydrates them in local windows of 200
from newest backward through `grade_from_events`, because a FIXED live tail window
re-creates the truncation bug: ≥N post-completion events (re-completions, late gateway
reconciles, stale-header emits) would push the terminal pair out of a `limit=N` read.
**Snapshot-first is load-bearing on two counts (round-6 finding 2):** live
rank-relative paging is NOT stable — events appended between page reads shift negative
ranks, so a later page can repeat a prior page's IDs and skip the grade entirely
(reproduced); and hydrated-event count under-reports the scan because `get_event_batch`
silently omits IDs whose bodies were stream-trimmed or whose `rp:eid` index expired. A
one-shot ID snapshot fixes both: the ID list is fixed regardless of concurrent
appends, and iteration walks IDs (not hydrated bodies), so a window with missing bodies
is scanned past, never treated as terminal. `get_session_event_ids` is called exactly
 once; an append injected between two hydration windows cannot change the frozen list.
Either terminal event type
recovers the grade — `session_end` and `session.completed` are emitted by different
code paths and fail independently. The dead `outcome` parameter is REMOVED (zero
callers); `EvalResult.outcome = task_result or ("failure" if failure_event_ids else "unknown")`.

**8. Durable handoff: the grade rides the eval trigger — behind a SERVICE-ONLY scope on
a DEDICATED Bridge credential.** Registering a scope in `auth/keys.py` leaks it
everywhere by construction (verified): `ENROLLABLE_SCOPES = SCOPES - {"admin", "*"}`,
enrollment stamps the full ceiling, old enrolled credentials are retroactively unioned
to the CURRENT ceiling at validation (keys.py:495-507), `ANONYMOUS_SCOPES` derives from
`SCOPES` too, and the enrollment parity test ratchets every new scope into
`firekeep-admin`'s teammate literal. Therefore:
(a) **`SERVICE_ONLY_SCOPES`** — a new withheld tier in `auth/keys.py`, subtracted from
BOTH derivations (`ENROLLABLE_SCOPES`, `ANONYMOUS_SCOPES`); the retroactive union then
excludes it by construction. `eval:grade` is its first member: registered in the scope
vocabulary but mintable ONLY by `deploy/bootstrap-keys.sh`'s `register_hash` (which
writes Redis directly) — never enrollable, never anonymous, never retro-granted, and
per (e) REJECTED by `create_key`, so admin `POST /auth/keys` cannot mint it either.
(b) **`FIREKEEP_BRIDGE_KEY`** — a dedicated credential (`agent_id=firekeep-bridge`),
because `FIREKEEP_INTERNAL_KEY` is shared by bridge + sentinel + cortex-api and
`ensure_env_key` is create-only (a scope edit on an existing key propagates to NO
existing deployment). The new key is minted on the next `update.sh` everywhere (the
create branch fires), carries the bridge outbound set + `eval:grade`, and only the
bridge compose service receives it; sentinel/cortex keep the old key WITHOUT
`eval:grade`. Relay's dedicated `RELAY_INTERNAL_API_KEY` is the existing precedent.
(c) **The route gate is EXACT**: only `eval:grade` honors the hint — not `admin`, not
`"*"` — so "only Bridge may assert a grade hint" is literally true. An unauthorized
hint is DROPPED with an ERROR log (never a 403 — the tail lift keeps correctness; the
12-day silent-403 lesson sets the log level). Human grade corrections are a later,
explicitly-designed surface, not a side door.
(d) **The gate works with enforcement OFF.** The disabled-mode FastAPI scope dependency returns the
anonymous identity and ignores any presented key entirely
(`auth/middleware.py:131-143`), so a scope check against the enforced identity would
drop every hint on auth-off deployments — including Bridge's valid dedicated key. The
route therefore validates the PRESENTED `X-API-Key` directly against the key store for
the service-only assertion when the enforced identity lacks `eval:grade` — the same
doctrine the vault established (withheld scopes stay authenticated even with
enforcement off; the middleware's own docstring records why that branch is
load-bearing). Bootstrap registers key hashes in Redis DB 7 regardless of
`AUTH_ENABLED`, and Cortex initializes the DB-7 auth client before calling
`init_auth(..., enabled=auth_settings.ENABLED)` in both modes
(`cortex/app/main.py:714-724`), so the direct check has a live store when enforcement
is off as well.
(e) **`eval:grade` is not member-mintable.** `create_key` accepts every scope in the
registry (`auth/keys.py:314`) and admin `POST /auth/keys` passes an arbitrary
caller-supplied list — so without a check an admin could mint an ordinary credential
carrying `eval:grade`. `create_key` explicitly rejects `SERVICE_ONLY_SCOPES`
(bootstrap's `register_hash` writes Redis directly and is unaffected — re-running
bootstrap remains the sanctioned mint path), and `GET /auth/scopes` returns member and
service-only scopes as separate lists.
(f) **The secret reaches ONLY the bridge container.** Seven compose services import
the whole root `.env` via `env_file`, so writing `FIREKEEP_BRIDGE_KEY` there exposes
the plaintext to cortex-api/mcp/worker/beat, sentinel, and relay regardless of who
maps `NB_FIREKEEP_API_KEY`. Every non-bridge `env_file` service explicitly blanks it
(`FIREKEEP_BRIDGE_KEY: ""` — the `cortex-mcp` confused-deputy pin is the precedent),
and a static compose-parsing test asserts the blanking stays complete.
**Residuals:** emit+trigger double-failure leaves the grade only in the Bridge hash;
the window before `update.sh` mints the new key runs on the event channel only.

**9. Race-safe store with DECLARED conflict semantics; a rejected write never drives
downstream state.**
(a) Ungraded writers use `SET ... NX` and therefore have NO overwrite path under any
interleaving. Graded writers take every create/upgrade decision through the CAS in (b).
(b) **Graded conflicts are first-graded-wins via WATCH/MULTI CAS — the time-limited
claim is GONE.** Two prior protocols failed review: claim-on-replace left the create
leg unguarded, and claim-first with a fixed-value 60s claim plus unconditional release
is not a correctness primitive at all (reproduced: writer A stalls past the TTL, B
acquires the expired claim, A resumes, overwrites from a stale read AND deletes B's
claim — the successor-lock-deletion bug the repo already documents and solves with
fencing tokens in `relay/app/leases.py`). Protocol now: an ungraded writer is
NX-create-only (D9a — no overwrite path exists for it, period); a graded writer runs a
WATCH(key) → GET → decide → MULTI/SET(EX) → EXEC loop, retrying on WatchError — the
decision (replace only a missing-or-ungraded record) and the write are one atomic
step, so a stale writer's EXEC fails instead of clobbering, with no expiry window and
nothing to fence. Sequential re-grades: rejected by the graded-record check. Pinned by
a concurrency harness (asyncio-gathered mixed graded/ungraded writers with
postconditions: final record graded; exactly one graded writer reports True) plus a
forced-WatchError test that installs a competing grade before the retry and proves the
stale writer re-reads, loses, and returns False.
(c) `compute_session_eval` checks `store_eval`'s return; on rejection it reloads the
authoritative record for features, webhooks, and the response — and when NEITHER the
write NOR the reload yields a record (infra failure), it ABORTS downstream, records the
eval-DLQ entry (`failure_type="store"`), and returns None: an unpersisted candidate
must never drive derived state.
(d) `harden._resolve_outcome` recomputes a stored ungraded eval only when
`find_terminal_grade` returns a recognized pair — never a truthy sniff, so result-only
payloads cannot loop futile recomputes.
(e) **Derived features obey grade dominance.** `store_features` is an unconditional
SET, so an ungraded writer that stalls after storing its eval can resume and overwrite
the GRADED features a concurrent upgrade already extracted — silently regressing
stored provenance to legacy/unknown. `store_features` therefore refuses to replace a
`outcome_source="task_result"` record with a non-graded one, using the same
WATCH/MULTI CAS shape as the eval store. The reload in (c) can still briefly observe
the pre-upgrade ungraded record mid-CAS. The graded computation normally re-extracts
and overwrites it; any later compute also heals it, and dominance makes that heal
monotonic. If the graded computation dies after its eval CAS but before feature
extraction, the old legacy feature remains excluded (missing signal, never a false
grade) until another compute or its TTL — there is no automatic harden retry once the
stored eval is already graded. **`record_tip_shown`'s in-place features rewrite is DELETED, not just
CAS-guarded** (round-6 finding 3): it read a whole `SessionFeatures`, mutated
`tips_shown`, and wrote the whole object back — so even under XX+KEEPTTL it would
clobber a concurrently-stored graded record with the stale legacy object it read,
bypassing the dominance guard entirely. A repo-wide search finds NO reader of
`SessionFeatures.tips_shown` (only the field definition and this writer), so the
rewrite is dead and removed; the per-card `times_shown` counter write stays (XX+KEEPTTL
— not provenance-bearing).
(f) **Webhook ordering is explicitly non-authoritative, but a notification never falls
back to a known-stale candidate.** An ungraded computation can
NX-store successfully (True), stall, let a graded upgrade fire its webhook, then resume
and fire a stale `session.completed`/`eval.computed` last — reloading only REJECTED
writes does not cover this accepted-then-superseded case. Two mitigations, and an
honest boundary: each computation re-reads the authoritative record immediately before
firing and builds the webhook payload from ITS complete atomic grade/source pair (so a
superseded computation emits the winner's pair, not its own stale one). If that final
read returns None for ANY reason — missing, parse failure, or Redis outage, all of which
the current fail-soft `get_eval` collapses to None — the webhook is SUPPRESSED and an
ERROR is logged; there is no `or result` fallback. This collapses the common case; but
cross-process delivery ORDER still cannot be guaranteed without an outbox, so D9's
authority guarantee is narrowed and DISCLOSED: the eval store (read via
`GET /evals/sessions/{id}`) is the sole source of truth. Webhooks are best-effort,
zero-or-more notification attempts carrying `session_id` for re-fetch: a failed
delivery is swallowed, while an eval-trigger retry may duplicate a successful one.
A consumer must never treat webhook arrival order as grade order.
A sequenced outbox is PR2/PR3 territory. Tests deterministically supersede an accepted
ungraded candidate before the final read and separately prove unreadable authority
fires nothing.

**10. Living Procedures drops the I4 timeline gate** (it excluded every >1,000-event
session — exactly Tier B's biggest samples). `_resolve_outcome`: stored eval →
tail-evidence recompute (D9d) → `session_success`.

**11. The pattern engine gets `"unknown"` + provenance; nothing refreshes legacy cards;
the dead auto-analysis path STAYS dead.** `SessionFeatures.outcome` grows `"unknown"`
(new default) and `outcome_source: Literal["task_result", "legacy"] = "legacy"` — the
default is load-bearing because old cached JSON has no provenance and must parse as
legacy rather than inherit a fabricated grade. The
extractor derives via the shared `binary_outcome`. Graded-only rates: `analyze_patterns`
hands its six detectors the graded subset only; `_success_rate` → None on empty graded;
experiment counting, tip effectiveness, dataset counters (`unknown_count`,
`success_rate=None` when nothing graded), `RecentFailureRule` + its `main.py` copy all
filter; an `outcome_filter` additionally requires `outcome_source == "task_result"`.
**Legacy-card aging, made true this time:** the claim "cards age out via TTL" was false
THREE times over — the dead auto-path would have refreshed them if revived; the
dashboard's `GET /patterns/effectiveness` call persists qualifying cards with a fresh
30-day TTL on every visit (store.py:432-440); and `record_tip_shown` — reached from
briefing generation (validation-enabled deployments) and the non-admin
`/patterns/tip-shown` route — rewrites every shown PatternCard (`times_shown += 1`,
store.py:288-296) AND the session's features record (store.py:277-286) with fresh
30-day TTLs, keeping frequently-shown legacy cards immortal. Fixes: the dead import at
`compute.py:205` stays dead with a KNOWN-DEAD comment (revival needs PatternCard
provenance — PR3), `compute_tip_effectiveness`'s PatternCard persist and
`record_tip_shown`'s remaining PatternCard counter persist use
**`xx=True, keepttl=True`**, and `record_tip_shown`'s whole-SessionFeatures rewrite is
DELETED (D9e). A stats/counter update never extends a record's life, and bare KEEPTTL
alone would be a new bug (reproduced: SET KEEPTTL on a key that expired after the GET
creates a PERSISTENT key, TTL −1 — XX makes the rewrite update-only, so an expired
record stays expired). Remaining TTL refreshers are admin-triggered only (manual
analyze, quarantine writes) — which is the claim, now accurate. PTTL tests compare the
actual before/after values; separate deterministic tests delete the card after its GET
but before its SET and prove both writers cannot resurrect it.

**12. No instruction-text changes.** Adoption is PR4's experiment; only the bridge tool
docstring changes (verified to trigger no client test and survive gateway slimming).

**13. Public terminal authority and grades bind to the VERIFIED principal, not the
agent label.** Session "ownership" is currently a self-declared label: `_default_agent_id`
lets an explicit param win, `complete_session` compares stored-label == supplied-label,
and the ungated `ctx_list_sessions` returns every `(session_id, owner label)` pair — so
any authenticated member can enumerate a teammate's session and complete it with the
matching label. Merely dropping the grade is NOT a fix: completion also overwrites the
free-text outcome, clears pointers, queues distillation of that outcome into memory,
and lets the caller request skill synthesis. The sibling `ctx_abandon_session` has the
same enumerate-and-replay attack: it accepts the known label, clears pointers, writes
`status="abandoned"`, emits the terminal signal, and thereby makes
`session_success(..., bridge_status="abandoned")` return False. Bridge's `/mcp` IS authenticated
per-request and the middleware attaches the verified
`{workspace_id, member_id, credential_id, scopes}` to `scope["state"]["identity"]`;
no bridge tool reads it today. Fix:

(a) `ctx_start_session` resolves the verified `member_id` via
`principal_from_scope(get_http_request().scope)` and stores it as `owner_member`.
The binding is written once and immutable in PR1.

(b) **A bound session's whole public terminal mutation is authorized, not just its
grade.** `complete_session` requires a non-empty verified member equal to
`owner_member` before ANY hash write, pointer delete, distill XADD, replay emit, eval
trigger, or skill synthesis. Mismatch or missing principal returns a recoverable tool
error with the session byte-for-byte unchanged. `ctx_abandon_session` applies the same
rule before calling either `SessionManager.abandon_session` or `after_abandon`: it
resolves and freezes the exact target (`explicit param > X-Session-Id > active
pointer`), reads that session's immutable `owner_member`, and refuses a bound missing
or mismatched principal. Freezing the fallback target is load-bearing — authorizing one
session and then passing `None` to the manager would let a pointer race select another.
The immutable binding makes this preflight authorization stable; the manager-level
method deliberately stays principal-agnostic because the trusted reaper calls it
directly. This does not violate D1: an invalid grade VALUE from the verified owner still
coerces to ungraded and completion proceeds; an attacker is an authorization failure,
not malformed grade input. A pre-deploy session with no `owner_member` keeps the
existing label-authorized complete/abandon path for compatibility, but completion is
grade-ineligible.

(c) Auth-disabled deployments collapse to the single anonymous owner principal
(`deployment_owner_member_id()`), so bound completion and public-abandon checks pass
trivially — one principal, no cross-member threat.

(d) **Receiver-authorized cross-member takeover is refused for bound sessions.**
`resume_session` gains `verified_member`; when `owner_member` is present and differs,
`takeover=True` does not help and nothing is mutated. The same verified member may
resume under another agent label, and legacy unbound sessions retain existing takeover
behavior. A real cross-member transfer needs owner consent — an owner-issued grant,
one-time handoff token, or audited admin operation — deferred beyond PR1. Until then,
the safe product is no transfer, not "transfer the work but leave the taker unable to
complete it." `owner_member` is never rewritten.

(e) This is the first verified-principal read inside an MCP tool. A real integration
test uses `AuthSettings(ENABLED=True)`, `mcp.http_app(..., stateless_http=True)`, the
real `FirekeepKeyAuthMiddleware`, `httpx.ASGITransport`, and
`app.router.lifespan_context(app)`; its fake `validate_key` accepts the real
`redis_client=` keyword. A raw `tools/call` proves
seeded key → middleware identity → `get_http_request()` → manager kwarg on FastMCP
3.1.1. The unsupported `Client(transport="http", httpx_client_kwargs=..., url=...)`
constructor from v7 is gone.

(f) **The Bridge grade is monotonic, un-erasable, and returned authoritatively.** Every
completion attempt WATCHes the session key, re-reads the FULL meta inside the watched
section, then WATCHes and reads every relevant active-pointer key before it decides;
that second watch is load-bearing because a concurrent start/resume can repoint one
after the read, and an unfenced completion would delete the NEW pointer. Every retry
re-runs label/principal/status-independent authorization and the pointer decision,
then commits under MULTI. The three grade fields are added only for the first accepted
grade; every other completion omits them. On WatchError the whole decision is retried;
retry exhaustion returns an error before any terminal replay/tool-layer side effect —
it never reports an uncommitted completion. After EXEC, the manager emits AND returns
the authoritative stored `(task_result, task_result_source)` pair, whether newly
written or pre-existing; an unbound session's pair is never authoritative even if
corrupt/partial-deploy state happens to contain recognized strings. The tool-layer
`session_end` and eval hint use THAT pair, not the caller's attempted grade and not
`task_result_dropped`, so an authorized re-completion re-emits the stored winner and
can heal a lost manager event instead of destroying the redundant channel.
Tests force two clients to WATCH the same empty grade, race conflicting values, and
assert both callers observe the one stored winner; sequential invalid/ungraded/re-grade
attempts cannot erase it.

This CAS protects the authoritative state; it does not make authorized completion
effects exactly-once. Two same-owner completions can both eventually EXEC after the
loser retries, producing a duplicate distill queue row and duplicate terminal events.
The grade remains the same first-committed winner, and the existing distill worker
already documents re-distillation as "idempotent enough". PR1 accepts this pathological
at-least-once behavior rather than expanding into a terminal-operation outbox.

**Residual:** sessions started before this deploy have no member binding and can still
complete or be publicly abandoned through the legacy label check — one bounded
generation, preferred over stranding in-flight work. Their completions are always
ungraded; their abandonments retain the existing False `bridge_status` signal. D13
closes bound complete/abandon/takeover authorization. A broader principal ACL for
non-terminal session reads and `ctx_update` is a separate security pass and is not
silently claimed here.

## Non-goals and disclosed residuals

- Receipts / evidence verification (PR2); experiment machinery, PatternCard provenance,
  auto-analysis revival (PR3); instruction nudge (PR4).
- Distiller prose changes (grade reaches the distill dict for free).
- Dashboard changes: none (the failure_rate chip already guards absence — verified).
- Pre-existing head-window truncation of Tier-1 metrics/Brier and OWM's memory_read
  join (PR2 territory).
- D8 residuals (double-failure; key-minting window), D9 residuals (graded-vs-graded
  CAS is first-committed-wins even when self-reports conflict; bounded retry budget;
  5,000-event snapshot cap; webhook delivery is zero-or-more and ORDER is
  non-authoritative — D9f), D13 residuals (legacy-unbound public complete/abandon remains
  label-authorized; authorized concurrent completion effects are at-least-once; no
  in-PR ownership-transfer surface; broader non-terminal session ACL audit deferred).

## Ship gates

- Suites green: `bridge/tests`, `cortex/tests`, `replay/tests`,
  `client/tests` (unmodified), `auth/tests`, root `tests/`, deploy shell tests updated
  for `SERVICE_ONLY_SCOPES` + `FIREKEEP_BRIDGE_KEY`.
- Pinned by test: both D1 halves (wire); "ungraded can never overwrite", the
  WATCH/MULTI CAS concurrency harness + WatchError retry (D9a/b), and feature grade
  dominance (D9e); abort-on-no-authority (D9c); a superseded computation's webhook
  carries the winner's atomic pair and an unreadable final authority fires nothing
  (D9f); sourceless grade → None everywhere and a non-dict
  payload degrades without DLQ (D2); grade recovery with ≥200 events appended BETWEEN
  hydration windows (with one ID-snapshot call) AND past a window with missing event
  bodies; pipelined hydration of 5,000 indexed bodies stays below 10 seconds on real
  Redis (D5/D7); impersonated completion makes ZERO terminal mutations, explicit-ID
  and active-pointer forms of impersonated bound abandon call neither the manager nor
  `after_abandon`, cross-member `resume_session(takeover=True)` on a bound session is
  refused, owner completion/abandon keep working, the reaper still drives the unchanged
  manager path, a pointer repointed between completion's read and EXEC is not deleted, AND
  sequential/concurrent re-completion cannot erase a stored grade while both callers
  observe the authoritative winner (D13); the real middleware→tool
  identity propagation integration path; `eval:grade` not in
  ENROLLABLE_SCOPES/ANONYMOUS_SCOPES, absent from
  teammate minting, REJECTED by `create_key`, a real initialized key store remains
  usable with enforcement off, and the hint is honored there via direct key
  validation (D8); `FIREKEEP_BRIDGE_KEY` blanked in every non-bridge
  env_file service (D8f, static compose test); PTTL asserting
  `0 < after <= before` plus delete-between-GET-and-SET resurrection tests at both the
  `compute_tip_effectiveness` card persist and the remaining PatternCard write in
  `record_tip_shown` (D11).
- `tests/test_procedure_docs.py` green — OWM bullet keeps `outcome=` and `_failure_rate`.
- A first no-param completion behaves as today except `session_end` carries no grade;
  an authorized re-completion carries the already-stored authoritative pair. Old
  EvalResult / SessionFeatures JSON round-trips.
