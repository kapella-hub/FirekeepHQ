# Enforced Runbooks — Living Procedures round 2

_Status: DESIGN PRE-REGISTRATION (2026-08-15), written before the first line of
enforcement code, per the house rule the Living Instructions rounds taught.
Incorporates two external reviews in full: the badge-system review (2026-08-14,
"a ledger without owned doors is advisory reputation") and the round-2 design
review (2026-08-15, five findings + four tightenings, all accepted — see
"Review dispositions"). Round 1 (observe/advise, `PROCEDURE_ENABLED`, shipped)
is described in `docs/guides/living-procedures.md` and is unchanged except
where stated._

## What ships

A skill whose steps carry command matchers becomes a **runbook**. Runbooks
have a human-set **enforcement mode** — `advise` | `require_ack` | `block` —
and the system observes agents executing them on the real shell path,
scores evidence by **success, not permission**, and (per mode) warns, demands
an acknowledged reason, or refuses commands whose load-bearing predecessors
have not succeeded. Change management for agents, dogfooded on our own VPS
deploys before anything is announced.

## Review dispositions (2026-08-15 review, all accepted)

1. **Allow is not success** — round 1 commits observations at gateway-allow.
   For commands that is unsound (a permitted-but-failed backup must not
   unlock the deploy). Command evidence is **pending at decide(), committed
   only by a successful reconcile carrying a real exit status**. File steps
   keep round-1 commit-at-allow semantics, documented as an approximation.
2. **require_ack needs a real protocol** — challenge → explicit ack → one-use
   permit bound to (verified principal, session, command hash, skill, step,
   bundle version). No permit, no allow; permits are consumed atomically.
3. **Fail-closed must survive `@never_raise(0)`** — the block-mode branch in
   `pre_tool` initializes its exit code to 2 and only an authenticated allow
   lowers it; the branch is exception-tight so the outer fail-open wrapper
   never sees a failure from it.
4. **Tenancy precedes enforcement** — procedure lookups, evidence, permits and
   modes are scoped to the VERIFIED workspace (the API-key principal resolved
   server-side). Self-reported `agent_id` never authorizes anything; it stays
   an observability label. Round-1's machine-global index keying changes to
   workspace-scoped; acceptable break, `PROCEDURE_ENABLED` has been default-off
   and the feature unannounced.
5. **Execution boundaries** — one execution per (workspace, session, skill);
   an execution CLOSES when its terminal step commits successfully, and the
   next match opens a new execution with a fresh evidence scope. Explicit
   overlapping-execution ids are future work, out of scope.

Tightenings, also accepted: step ids survive via normalized-text match or an
explicitly retained id — not arbitrary prose edits; ordering enforcement is
"missing load-bearing predecessor" exactly as round 1 detects it, NOT strict
next-step order; the transport default timeout is 10s so the escalation call
pins its own (5s); Bash success detection uses the actual exit status, and an
UNKNOWN status is not success.

## The command's journey (normative)

```
Bash tool call
 → pre_tool (PreToolUse, already registered for Bash, already blocking-capable)
   → existing local destructive check (unchanged: snapshot-then-allow, no network)
   → local match against the session's runbook BUNDLE (command string; cwd sent for audit)
       no match  → proceed exactly as today (zero added network)
       match     → POST /agent/action/before (adapter "shell-hook", 5s explicit timeout)
                   server: verified-workspace lookup → step match → verdict:
                     advise      → allow + advisory (warning text reaches the agent)
                     require_ack → valid permit? consume (one-use) → allow
                                   else → rethink + challenge_id (exit 1 → blocking at seam)
                     block       → missing successful load-bearing predecessor
                                   → block (exit 2); else allow
                   decide() writes a PENDING command observation keyed by action_id
 → command executes
 → post_tool (PostToolUse) → POST /agent/action/after {action_id, success, exit_status}
     exit_status == 0            → pending observation COMMITS (step evidence exists)
     nonzero / unknown / absent  → attempt recorded; NOTHING satisfied
     no reconcile before TTL     → pending expires; nothing satisfied
```

Failure postures: `advise`/`require_ack` escalations fail OPEN (hooklog + one
stderr line). `block` escalations fail CLOSED — server unreachable means those
specific commands wait, with stderr naming the runbook; that is the price the
human accepted when choosing block, and only for block-mode patterns.

## Wire contract

**StepSpec** gains `kind: "command"` with `pattern` = bounded glob (existing
`MAX_PATTERN_CHARS`) matched against the whitespace-normalized command string.
`file_glob` and `unobservable` unchanged. Command matching is
mistake-catching, not adversary-proof — stated on every surface that shows it.

**Pending evidence**: `proc:pending:{action_id}` — {workspace, session, skill,
step_id, execution_no, command_hash, created}; TTL = the gateway's existing
reconcile deadline. Commit moves it into the execution's observed set (round-1
store shape, now keyed by workspace + execution_no). Attempts:
`proc:attempt:*` retained for the ledger/audit; satisfy nothing.

**ActionAfterRequest** gains optional `exit_status: int | None`. The client
sends the REAL Bash exit code when the harness provides it; absent → not
success. (`success` alone no longer commits command evidence.)

**Permit protocol**: challenge `proc:challenge:{id}` minted on require_ack
rethink, advisory carries the id; TTL 10 min. Ack via new MCP tool
`runbook_ack(challenge_id, reason)` (+ REST `POST /procedures/ack`): verifies
the challenge belongs to the caller's verified workspace + session, records
the reason (audit + future ledger), mints `proc:permit:{challenge_id}` bound
to (workspace, member/key id, session, command_hash, skill, step, bundle
version), TTL 10 min, consumed with an atomic GETDEL on the retried command.
Different command ⇒ different hash ⇒ no reuse. Loops are impossible by
construction: the retry either consumes the permit or re-challenges.

**Bundle**: `GET /procedures/bundle` (session scope) →
`{version, workspace_id, entries: [{skill_id, step_id, pattern, mode,
load_bearing, fail_posture}]}` — command-kind steps of all runbooks, all
modes (advise entries escalate too; the gated set is small and curated).
`version` = sha256[:12] of the canonical entry list. The client stores it
ATOMICALLY (temp + rename) as last-known-good, workspace-scoped, independent
of the briefing (which Codex never receives). `POST /procedures/bundle/ack
{version}` records which sessions hold which version; a runbook in block mode
whose recent sessions lack acks is surfaced on the dashboard as NOT ACTIVELY
ENFORCED — the server still enforces whatever reaches it, but coverage is
reported honestly, never assumed.

**Modes**: `proc:mode:{workspace}:{skill_id}` — {mode, set_by, set_at};
default `advise`. `GET/PUT /procedures/{skill_id}/mode`, PUT **admin scope
only**, surfaced as a dashboard control. The skill PATCH path cannot touch
mode; agents may propose runbooks, never arm them.

**Tenancy**: decide() threads the auth layer's verified workspace into every
procedures lookup/write. Index keys gain the workspace dimension. Permits and
modes are workspace-keyed. `agent_id` appears in audit records only.

## What round 1 keeps unchanged

Recognition, advisory texts, warn latch, two-phase plan/commit, the nightly
hardening pass, proposals-to-inbox, file_glob semantics, and the
cannot-raise guarantee on the pre-edit path. Enforcement lives in a new
`procedures/enforce.py` consulted from decide() — deliberately NOT a policy
rule, for the reason observe.py already documents (PolicyContext carries no
action type; ActionBeforeRequest does).

## Runtime coverage, stated

Claude Code / Kiro (blocking hooks): full enforcement. OpenCode: hook parity
per its adapter. Codex: no hooks — runbooks stay advisory via MCP-declared
actions; documented, not hidden. MCP-only runtimes have no shell, so this
domain does not apply. The owner can always bypass by reconfiguring their own
client — badges bind staff, not the building's owner.

## Rollout gates (all must hold, in order)

1. Spec committed before enforcement code (this document).
2. `PROCEDURE_ENABLED` on for our workspace; the VPS deploy runbook authored
   as a real skill (backup → pull → build with APP_VERSION → IMAGE_TAG check
   → health verify) with command steps and load-bearing flags.
3. ≥5 real deploys observed with correct recognition and zero false
   load-bearing warnings → `require_ack`.
4. require_ack catches or correctly waves through ≥1 real deviation → `block`
   on load-bearing steps.
5. Nothing on firekeep.ai until block mode has prevented one real mistake.

## Phases

- **A — cortex core**: command StepKind + matcher; workspace threading;
  pending/commit evidence lifecycle; enforce.py verdicts; permit store +
  `runbook_ack`; bundle + mode endpoints on the procedures router (NOT
  main.py — concurrent install-story work owns that file today); tests.
- **B — client**: bundle fetch/store/ack (state.py atomic pattern);
  pre_tool local match + escalation + the exception-tight fail-closed
  branch; post_tool exit-status capture and reconcile; tests. Avoids
  cli.py/wizard.py (foreign-owned this week).
- **C — dashboard + docs + dogfood**: runbook cards (mode control, execution
  and deviation views, bundle-ack coverage warning), inbox deviations,
  living-procedures.md round-2 section, the deploy runbook itself.

## Effort, honest

A: ~4 days. B: ~3 days. C: ~2 days. Then the observation window measured in
real deploys. Total 1.5–2 weeks, as re-estimated after review — the permit
protocol, evidence reconciliation, bundle handshake and tenancy threading are
exactly the parts the first draft undercounted.
