# Lossless token reduction — `precompact` + the residency contract

**Date:** 2026-07-29
**Status:** design, approved for planning
**Constraint:** Firekeep is a commercial product. A paying customer must get lower token
cost with **zero degradation** of accuracy, capability, or behaviour. Any saving whose
mechanism is "give the agent less of what it asked for" is out of scope, however large.

## 1. Problem

Firekeep is sold on surviving context compaction, but the whole mechanism is reactive. The
rendered instruction layer tells the agent to notice its own amnesia and self-repair
(`client/firekeep_client/adapters/base.py:347-348`):

> Your own earlier plan or decisions missing from context (after compaction) →
> `ctx_get_shadow` before asking the user to repeat themselves.

Two consequences follow, and they are the whole of this spec:

1. **Every restore is a full re-injection.** `bridge/app/shadow.py::assemble_shadow` returns
   the entire session document on every `ctx_get_shadow` / `ctx_resume_session`, including
   content the agent itself authored via `ctx_update` minutes earlier and still holds.
2. **No compaction signal exists on any runtime.**
   `client/firekeep_client/contract/matrix.py:41` reads
   `"precompact": {"claude": "none", "kiro": "none", "codex": "none", "opencode": "none"}`,
   enforced by `client/tests/contract/test_matrix.py:25-39`. So there is no safe moment to
   checkpoint, and no signal that would let a cheaper restore know whether it is safe.

## 2. What was measured, and why the obvious savings were rejected

Two adversarial measurement passes (2026-07-29) audited every layer of reused input on the
customer-facing surface. Recording the rejections here so they are not re-derived.

**Rejected — each trades information for bytes:**

| Candidate | Why rejected |
|---|---|
| Shadow read-side token budget / summary-tier restore / paths-only files | All silently drop working state, which is the one thing Bridge exists to preserve |
| `trim_to_budget` truncation (`cortex/app/engine/rag.py:82-95`) | Truncating memory content mid-entry cuts exactly the file paths and commands recall exists to carry |
| Session-scoped recall exclusion | `cortex/app/mcp_server.py:178` uses `top_k=3`. Excluding already-delivered memories promotes the 4th/5th best — a *different, worse* result set, not the same one cheaper |
| MCP tool gating | Hiding a tool removes capability, and dynamic gating would break the byte-stable tool block (§6) |
| Collapsing the 4× duplicated 750-char MCP `initialize` block | Deliberate, documented redundancy: `bridge/app/mcp_server.py:55-66` — "the ONLY instruction channel that needs no client-side adapter, so it is the only one that reaches Codex and a user who has deleted the rendered block" |
| Deleting the recall query-echo footer (`rag.py:967`) | A shipped tool consumes it: `symdex/src/firekeep_symdex/tools/recall_with_code.py:122` runs `_extract_keywords(context_block)` over the whole block, `:153` builds cross-references from it |
| One-shot `stop` reminder via scratch marker | Fail-unsafe: markers never expire (§5), the key only exists after `ctx_start_session`, and whether a Stop `systemMessage` reaches model context is unvalidated for Claude |
| Renaming MCP server keys (`firekeep-cortex` → `fk-cortex`) | `docs/INTEGRATIONS.md:104` documents `mcp__firekeep-cortex__action_before` to customers; permission allowlists and CI matchers key on the literal |
| Wiring `RECALL_TOP_K` | `docker-compose.yml:283` is `RECALL_TOP_K: ${RECALL_TOP_K:-3}` so the env var is always 3, while `cortex/app/models.py:26` defaults to 5. Wiring it drops every REST recall from 5 memories to 3 on every existing deployment |

**Headline finding:** the provable lossless saving on the customer-facing tool surface is
~119 tokens against a ~15,400-token baseline — **0.77%** — and tool schemas sit in first
position, byte-stable per request, so they bill at roughly the cache-hit rate anyway.
Trimming is not a viable product story. The savings that *are* large all died on the same
root cause: no compaction signal. That is what this design builds.

**Method caveat.** Every token figure in the audits is `chars/4`. That systematically
*under*-counts JSON tool schemas, which tokenize nearer 3.2–3.7 chars/token. The 94-tool
block is realistically 15,000–17,000 tokens. Correct upward before any customer-facing
claim, and use a real tokenizer for anything load-bearing.

## 3. Architecture

Three components, in dependency order.

```
  scratch TTL fix  ──►  precompact hook core  ──►  residency contract
  (prerequisite)        (checkpoint + invalidate)   (delta restore)
```

Plus seven independent free wins (§7) that share no state with the above.

## 4. Component — the `precompact` hook core

New: `client/firekeep_client/hooks/precompact.py`, a **dict core** wired through the
existing generic dispatcher (`client/firekeep_client/hooks/__main__.py`), joining
`session_start` / `stop` / `prompt` on the dict side.

**Scope is deliberately narrow.** A PreCompact hook fires *before* compaction but cannot
read the agent's unstated reasoning, so it **cannot** recover decisions the agent never
wrote via `ctx_update`. Claims to the contrary are wrong. It does four cheap, certain
things:

1. **Bypass gate first.** `resolver.is_bypassed()` → return `{}` immediately, before any
   config resolution or network call. Same contract as every other core; personal mode must
   reach nothing.
2. **Workspace checkpoint.** `_git.workspace_snapshot()` →
   `ctx_update(category="scratch", key="workspace_snapshot")`. Cheap, real, already
   implemented.
3. **Invalidate the shadow cursor**, locally and server-side. Load-bearing for §5. The
   server-side half reuses existing plumbing rather than adding a tool: Bridge keeps a
   per-session `shadow_epoch`, and `precompact` bumps it via an ordinary
   `ctx_update(category="scratch", key="shadow_epoch", ...)`. A cursor carrying a stale
   epoch is refused and answered with a full restore. **No new MCP tool** — §2 rejects
   growing the tool surface, and that applies to our own additions.
4. **Stamp `compacted_at`** so `stop` and the next `session_start` know a compaction
   occurred, and emit one short line telling the agent its working state is in Bridge.

**Non-functional requirements:** budgeted like `session_start` (~15s ceiling), decorated
`@never_raise`, best-effort on every call. A slow hook stalls the customer mid-compaction,
which is worse than a missed checkpoint.

**Deferred, with a reason.** `transcript_path` is present in the hook payload and currently
never read. The core *could* push a raw transcript tail to Bridge before compaction
destroys it — genuine state preservation. It also ships a customer's raw conversation to
the server. That is a privacy decision for a sold product, not an engineering one, and it
is out of scope here.

### 4.1 Runtime availability

Only Claude exposes a compaction event. `matrix.py` changes to
`"precompact": {"claude": "hook", "kiro": "none", "codex": "none", "opencode": "none"}`
with the reason recorded inline, in the file's established honest-degradation style.

## 5. Component — the residency contract

The mechanism that makes a cheaper restore lossless, **without** depending on a compaction
signal — which matters because only Claude could ever have one.

The insight: the only party that can observe whether earlier content is still in context is
the agent looking at its own context. So do not detect compaction. Make residency the
agent's affirmation, with the safe answer as the default.

- `ctx_get_shadow()` — no argument → **full restore**, byte-identical to today. This is the
  default and it is always correct.
- The full response carries an opaque `shadow_cursor`.
- `ctx_get_shadow(since=<cursor>)` → delta. The tool docstring states the contract plainly:
  *pass `since` only if the earlier shadow is still visible in your context; if unsure,
  omit it.*
- The delta response names what it withheld and why:
  *"Omitted 47 decisions and 100 files delivered above. If they are no longer visible to
  you, call `ctx_get_shadow()` without `since`."*

That last point is the whole ballgame. An agent reading a delta must never be able to
conclude the omitted content **does not exist** — that inference is the degradation, not
the omission.

### 5.1 Fail-safe analysis

Three independent safeguards, all pointing toward a redundant full restore:

| Safeguard | Failure mode it covers |
|---|---|
| Default is full restore | Agent never opts in → today's behaviour exactly |
| Cursor obtainable only from a prior full response | Fresh session, different agent, cleared transcript → no cursor to pass |
| `precompact` invalidates the cursor server-side (Claude) | Agent wrongly passes a stale cursor after compaction |

The design has no path that omits content the agent lost. Where it can be wrong, it is
wrong in the direction of sending too much.

### 5.2 Cursor storage

The cursor must **not** live in bare `write_scratch` — see §6. Use the session-stash
pattern (`client/firekeep_client/state.py:280-297`), which self-enforces a TTL via a
timestamp embedded in its JSON payload.

### 5.3 Blast radius on `assemble_shadow`

`assemble_shadow` is not private to the tool. Its output also feeds
`GET /sessions/{session_id}`, the replay context snapshots written on every plan/decision
`ctx_update` (`bridge/app/mcp_server.py:293-302`), and cortex's skill scorer and
synthesizer. Any signature or header change must be traced through all four consumers.

Related, and worth fixing while in this code: `cortex/app/skills/scorer.py:157-161` and
`cortex/app/skills/synthesizer.py:288-294` both call `.get()` on the value
`GET /sessions/{id}` returns as a **markdown string** — an `AttributeError` swallowed
upstream. Pre-existing, unrelated to token cost, but adjacent.

## 6. Prerequisite — scratch TTL

`state.reap_stale` (`client/firekeep_client/state.py:214-232`) iterates only
`_ACTIONS_SUBDIR` and `_PRESTATE_SUBDIR`. **Scratch markers never expire.**

This must be fixed before anything keys correctness on a scratch marker. It also fixes a
live customer-facing bug: `tasks_digest_{agent}@{profile}` (`prompt.py:70`) has no session
component and no TTL, so an unchanged pending-task set is suppressed *forever, across every
future session on that machine* — the customer silently stops being told about their own
tasks. Fixing it is accuracy-positive and independent of everything else here.

## 7. Free wins — shipping alongside

Independently verified lossless, no shared state, any order:

| # | Change | Files |
|---|---|---|
| 1 | `output_schema=None` on the four `-> str` FastMCP tools. Today a `-> str` tool delivers `{"result": "<JSON-escaped markdown>"}` and that wrapped copy is what the runtime renders, so every newline ships as `\n`. Highest-volume item, on the highest-frequency output path | `memory_recall`, `skill_recall`, `skill_list`, `vault_list` |
| 2 | Content-comparing writes — skip the write when content is byte-identical. See §8 | `adapters/base.py:147-149` + the seven `write_text` sites |
| 3 | Remove the dead `knowledge_sources` tool reference (3 hits) | `cortex/app/mcp_server.py:1078,1103,1140` |
| 4 | Correct three corpus tool **descriptions** documenting an entity-extraction feature that always returns zeros. Descriptions only — the response line is an output contract | `cortex/app/mcp_server.py`; dead feature at `corpus/pipeline.py:73-77,130-136` |
| 5 | Complete `relay_register`'s dangling sentence fragment ("The presence entry") | `relay/app/mcp_server.py:602` |
| 6 | Strip auto-generated Pydantic `title` annotations via a post-process dict walk — **not** by migrating to fastmcp v3, which would add a heavyweight dependency to the client kit | `client/firekeep_client/decision/server.py` |
| 7 | Hoist `"RESUMABLE SESSIONS:"` out of its loop. Do **not** touch the sibling `_bull` at `:97-99` — that label is a per-item prefix, not a header | `cortex/app/briefing/render.py:168` |

Also in scope, mechanism revised: strip the predecessor `nexus:instructions` block from the
customer's global instruction file. Measurements confirmed on a live machine (3,213 chars,
0.998-similar to the current block). The mechanism must **archive to `.bak`** in the manner
of `adapters/kiro.py::_migrate_legacy`, not delete content-blind from a user-owned prose
file. Add `LEGACY_INSTRUCTION_MARKERS` alongside the three existing legacy tuples, and
respect the `DO NOT RENAME` warning at `base.py:25-33`.

## 8. Cache integrity

The customer's prompt cache is the largest single lever in this whole analysis, and it is a
correctness property rather than an optimization: anything Firekeep does that invalidates it
re-bills everything downstream at full rate.

**Audited clean.** All `@mcp.tool()` decorators are at column 0 with no flag-gated
registration; symdex's `pkgutil` walk is sorted; the full tool block serializes
byte-identical across independent processes. No timestamps, uuids, or unsorted iteration in
any adapter. **Preserve this** — it is why dynamic tool gating is rejected in §2.

**One self-inflicted hazard.** `base.write_json` (`:147-149`) and every adapter
`write_text` rewrite unconditionally, even when content is byte-identical. Background
auto-update is on by default and `firekeep update` re-execs `firekeep install`, which
re-renders `~/.claude/CLAUDE.md` and `~/.claude/settings.json` **mid-session**. Whether the
host re-reads on mtime cannot be determined from this repo — which is exactly why an mtime
touch for zero content change is indefensible. Free win #2 closes it.

## 9. Testing

Standard coverage (a new dict core, dispatcher wiring, adapter rendering, matrix row) plus
three obligations specific to the zero-degradation constraint:

1. **Residency fail-safe tests.** For each of: no cursor, unknown cursor, expired cursor,
   cursor issued to a different agent, cursor invalidated by `precompact` — assert the
   response is a **full** shadow. These are the tests that make the losslessness claim
   real; write them first.
2. **Byte-stability regression test.** Serialize the rendered surface (tool block + every
   rendered file) twice and assert byte-identity. Guards §8 against future drift.
3. **Delta-union equivalence.** For any session, assert that a full restore and
   `full-then-delta` deliver the same set of entries. No entry may be reachable only via
   one path.

Note the existing test that must change: `client/tests/adapters/test_claude.py:231-243`
asserts `len(hooks["PreCompact"]) == 1`. Update it to assert the legacy echo hook
**survives alongside** the rendered firekeep group. The test's intent — never clobber a
foreign hook — is preserved; only its arithmetic changes. Amend the rationale comment at
`base.py:25-33` (whose stated premise is "the kit renders no PreCompact hook of its own")
without touching the legacy token strings.

## 10. What this delivers

Not "fewer tokens per turn" — §2 shows that claim is worth about 1% and sits in the cached
region. What Firekeep can honestly deliver under a zero-degradation constraint is **more
turns before compaction, and no lost working state when it happens.** That is the thing a
customer feels, and the claim this design supports.

## 11. Open risks

- **Agent compliance.** The primary safeguard rests on the agent honestly reporting what it
  can still see. Mitigated by making the safe answer the default and the unsafe answer
  require an explicit opt-in, but not eliminated.
- **Runtime coverage.** kiro, codex and opencode get the residency contract but no
  server-side belt, since none can signal compaction.
- **Measurement.** Every figure here is `chars/4` (§2). Before making a customer-facing
  savings claim, instrument a real session with a real tokenizer and real cache hit/miss
  accounting.
