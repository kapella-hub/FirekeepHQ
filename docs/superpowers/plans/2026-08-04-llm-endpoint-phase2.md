# LLM endpoint selection — phase 2 (implementation record)

Design: `docs/superpowers/specs/2026-08-04-llm-endpoint-selection.md`, the
"PHASE 2 (separate change)" item.
Phase 1 record: `docs/superpowers/plans/2026-08-04-llm-endpoint-phase1.md`.

Scope: `cortex/app/decision/synthesize.py` only. Phase 2(b) — `sleep_cycle.py` —
is **deliberately not here**; see "What was left out". Phase 3 (`dreams/`) is
untouched.

Commits, in order:

| SHA | What |
|-----|------|
| `4ceef0b` | `fix(cortex): decision suggestions via app/llm; a failed pass is not a healthy board` |
| `fac81b7` | `fix(cortex): decision synth budget 20 -> 30, the number the client already assumed` |
| (this file) | `docs(cortex): record the phase-2 LLM endpoint change` |

---

## The claim, checked

The task was to verify rather than assume the audit's conclusion: *the Decision
Board's `suggested_answers`/`suggested_actions` have almost certainly never once
succeeded on an Ollama deploy.*

**Verdict: correct for the deployment that matters, and overstated as a blanket
claim about "an Ollama deploy". It also understates the defect in a different
direction — the failure was invisible, not merely silent.**

### Where it is right

On the production VPS (ollama 0.32.4, **qwen3:4b — a thinking model**, 4 vCPU),
the suggestion pass could not have completed. `_llm_suggest` posted to
`{LLM_BASE_URL}/chat/completions`, and ollama **ignores `think:false` on `/v1`**
(phase 1, probes D/E), so the model generates its whole reasoning block before
emitting a character of JSON. Phase 1 measured a comparable `/v1` JSON-mode call
at **83.19s** (1978 chars of reasoning, 26 content tokens); the audit measured
288.9s on a larger body. The budget was **20.0s**. Generation on that box runs at
5.9–7.2 tok/s, so 20s buys ~120–145 generated tokens — not even out of the
reasoning block. There is no code path that shortens this: the endpoint is fixed,
`think:false` is not sent on `/v1` and would be ignored if it were, and there was
no `max_tokens` to bound the reasoning.

### Where it is overstated

The claim is about *thinking models*, not about *Ollama*. The lever is the flag,
not the vendor — the same distinction phase 1 had to learn the hard way in the
opposite direction ("native does not imply fast"). A **non-thinking** model
emits no reasoning, so a small board can finish inside 20s on `/v1`. The office
deploy runs `llama3.2:3b`, which has nothing to disable. So:

- thinking model on `/v1` (the VPS): **never succeeded**, and could not have;
- non-thinking model, or any fast hosted OpenAI-compatible backend: succeeds for
  small boards, plausibly fails for a full 8-question one on CPU;
- **generation-less deploy** (the office embed-only ollama image, a real shipped
  configuration): never succeeded, and reported success — see below.

### What the audit missed

Retrieval is fine, exactly as the design requires: `synthesize_board` runs the
recalls *before* the LLM pass and *outside* its `try`, so `evidence` and
`knowledge_found` are returned whatever the suggestion call does. Confirmed by
reading and now pinned by a test.

The real damage is one level up. `_llm_suggest` ended in

```python
    except Exception as exc:
        logger.warning("decision suggestion LLM call failed, returning empty: %s", exc)
        return {}
```

An empty dict is an **ordinary return**. `synthesize_board` therefore saw a
successful call, took the non-degraded branch, and answered
**`degraded: false, note: ""`** on a board that had produced nothing. Every
non-timeout failure took that path: connect errors (the generation-less deploy —
*every call, forever*), 4xx/5xx, malformed completions, wrong-shaped JSON.

Only the timeout escaped, and only by accident: `asyncio.wait_for` cancels the
inner coroutine, `CancelledError` is a `BaseException`, and `except Exception`
does not catch it. That accident was also a race — `wait_for` and the httpx
client were given the *same* number, so which deadline fired first was decided by
the connect time (`wait_for` starts its clock earlier and normally wins by
milliseconds). Had httpx won, the timeout would have been swallowed too. A
correctness property should not rest on a millisecond.

So the honest summary is worse than the audit's: not just *"suggestions never
arrived"* but *"suggestions never arrived and the board said it was fine"*.

---

## What shipped

**Converted** `_llm_suggest` to `llm.chat(json_mode=True, temperature=0.2)`,
matching `knowledge/classifier.py`'s shape. No second hand-rolled httpx block;
`import httpx` is gone from the module.

**Deleted the `reasoning`-field fallback**, for the same reason phase 1 deleted
the classifier's: under JSON mode an empty `content` means the grammar blocked
the output, so `reasoning` is prose **on every backend measured** (phase-1 probe
E returned 1978 chars of it) and `json.loads` rejects prose.

An earlier draft of this record said "both paths ended in `JSONDecodeError`" and
claimed no terminal state changes. That was **empirical, not tautological**, and
the review caught the overreach: a backend that mirrored valid JSON into
`reasoning` *would* have been rescued by the old fallback and returned a healthy
board. So this does trade an unobserved rescue for a visible failure. That is
the right way round — the rescue path also returned `degraded=False`, so it
could turn a broken call into a board claiming to be healthy, which is the exact
defect this change exists to close.

**Fixed the silent-success bug.** `_llm_suggest` raises now — on transport, HTTP
status, unparseable JSON, and on valid JSON of the wrong top-level shape. The
caller's existing handler turns that into `degraded=True, note="retrieval-only"`,
which is what the field always meant. The caller's log line gained the exception
*type*, because a bare `wait_for` `TimeoutError` stringifies to `""` and `"%s"`
alone logged a reason of nothing at all.

**Kept `asyncio.wait_for`.** httpx applies its timeout per operation
(connect/write/read), so a pathological backend can exceed it in total, and this
endpoint has a fixed 45s ceiling to answer inside. The two deadlines still race,
but the race is no longer semantically load-bearing: both arms now set
`degraded=True`.

**`DECISION_SYNTH_TIMEOUT_SECONDS` 20.0 → 30.0**, mirrored into both
`docker-compose.yml` blocks and `.env.example` (phase 1's trap: a stale
`${VAR:-20}` silently wins over the code default).

---

## The timeout decision, and why it is not just obedience

The design said 20 → 30. I checked the client before relying on it and the number
holds, for two independent reasons:

1. **The client already assumed 30.** `client/firekeep_client/decision/server.py`
   sets `_DEFAULT_SYNTH_TIMEOUT = 30.0` under the comment *"Kept env-tunable to
   mirror the server default."* It did not mirror it. The two processes have
   disagreed since SP4 shipped.
2. **30 is also the client's ceiling.** The same file derives its HTTP timeout
   for `POST /decision/synthesize` as `synth + _INGEST_TIMEOUT_HEADROOM (15.0)`
   = **45s**. This endpoint must answer inside that or be hung up on. **Past 30
   needs a coordinated client release, not an env change.**

   **What the raise costs, stated plainly.** The 45s is fixed, so the margin for
   everything outside the LLM budget — the up-to-9 recalls — went **25s → 15s**.
   That is a *reduction*, not the restoration "restoring the 15s the constant is
   named for" makes it sound like; the 15s is precisely the recall headroom, and
   it is what shrank. It is still the right trade, because at 20 the LLM pass
   never completed at all, so the extra 10s of recall margin was protecting a
   call that produced nothing.

   What actually lives in that margin: the recalls issue
   `ContextQuery(format="raw")`, which `RAGEngine.recall` uses to skip the
   synthesis LLM pass entirely (`engine/rag.py:302`) — so **no generation**. But
   there *is* one embed per recall (`VectorClient._embed` →
   `POST {LLM_BASE_URL}/embeddings`), LRU-cached by content hash and therefore
   **always cold** for distinct question texts. Sub-second each on this
   hardware, not free, and nobody has timed nine of them. An earlier draft of
   this record and of `config.py` said "~10ms each"; that number was **not
   measured and is wrong by roughly two orders of magnitude** — corrected here
   rather than quietly dropped, because the ceiling argument is justified with
   it. The conclusion survives: no generation on that path, and 15s absorbs the
   embeds with room.

   **Ops note:** `RERANK_ENABLED=true` fires `top_k ×
   RERANK_CANDIDATES_MULTIPLIER` LLM calls *per recall*, times 9 questions. That
   already exceeded the old 25s, so this is not a new regression — but the
   margin is thinner now. Do not enable it on a CPU backend and expect this
   endpoint to answer inside the client's ceiling.

A third reason the design did not state, and the one that makes 20 wrong on its
own terms: **20 was below the floor even for the endpoint this change makes
fast.** At the measured 5.9–7.2 tok/s, 20s is ~120–145 output tokens — less than
a suggestion JSON for three questions. Fixing the endpoint alone would not have
made the feature work.

### No per-endpoint split, unlike classify

Phase 1 landed `KNOWLEDGE_CLASSIFY_NATIVE_TIMEOUT_SECONDS` as a separate budget.
That structure is right there and wrong here, because the asymmetry runs the
other way. A native sibling could only ever be **lower** than the `/v1` one — and
"lower native budget" is precisely what phase 1 measured to be dangerous: the
probe confirms *ollama*, not *a thinking model*, so a non-thinking backend is
routed down the native path, gains nothing from `think:false`, and simply loses
headroom. Meanwhile the `/v1` budget cannot be raised past 30 because the client
hangs up at 45. One number for both endpoints.

### No `max_tokens`, unlike skill synthesis

The design asked for an output bound. I did not add one, and this is the one
place I depart from it deliberately.

Phase 1 added `SKILL_SYNTH_MAX_TOKENS=800` because skill drafting is free-form
text with nothing to terminate it — measured `done_reason=length` at *every* cap
tried (300/400/500/800), i.e. the model never stops on its own, so the cap
multiplied by the token rate simply *is* the cost of a draft.

This call is **JSON mode**. The grammar terminates generation by itself — a
native classify returns complete, valid JSON in 4.00s. A cap therefore cannot
make a successful call shorter; it can only truncate a long one into invalid
JSON, converting a slow success into a guaranteed `JSONDecodeError`. Wall clock
is already bounded by the timeout, which is the resource actually being
protected. And on `/v1` a cap is spent on reasoning tokens *before the answer
starts* — the exact failure `dreams/synthesize.py:36-71` records from raising its
own bound 700 → 4000.

There is also a concrete regression it would have caused. Phase 1's follow-up #2
notes that `max_tokens` is the one body change that lands on `/v1` too. A bound
sized for the answer alone (~550 tokens for a full 8-question board) would
truncate on the one deployment shape where this feature *currently works* — a
non-thinking model on `/v1`, i.e. the office deploy. Sized large enough to be
safe there, it would never bind on the CPU boxes anyway.

### Honest ceiling — this is a partial win, and it is now measured

Run live on the VPS (qwen3:4b, native `/api/chat`, `think:false`) after the
change landed:

| Board | Wall clock | Output tokens | vs the 30s budget |
|-------|-----------|---------------|-------------------|
| 1 question  | **20.98s** | 57  | fits |
| 3 questions | **16.31s** | 111 | fits |
| 8 questions | **37.28s** | 239 | **exceeds** |

**The ceiling claim is confirmed: small boards get suggestions, a full board
still degrades to retrieval-only** — correctly labelled now rather than reported
as healthy.

Two corrections to what this record previously estimated. My token figure was
**high** — I guessed ~400–550 output tokens for an 8-question board; the real
number is **239**. The timing conclusion survives only because generation is
*slower* than the estimate assumed, so the binding constraint is **wall clock at
~6.5 tok/s, not output size**: 239 tokens is a small answer and it still costs
37s. Read the table as three data points rather than a function of question
count — the 1-question run is *slower* than the 3-question one, so per-call
overhead and warmness dominate at this size.

The remaining lever is therefore the **prompt** (cap suggestions per question
and their length, which cuts tokens actually generated), not a larger budget the
client will cut off and not a token cap that only truncates. That is a semantic
change to output quality, it is unmeasured, and it is not in this change.
Follow-up 1.

---

## What was left out, and why

**Phase 2(b), `sleep_cycle.py`, is NOT converted.** The design gates it on a
cheap confirmation — `GET /ops/queues`, is `event_dlq` non-zero and climbing —
and that check has not been run, because the VPS was being redeployed with phase
1 by a concurrent process and this change was told not to touch it. It also needs
a sync `chat_sync()` wrapper (`httpx.post`, not `AsyncClient`), and its failure
path writes the consolidation DLQ. Converting it blind, unmeasured, in the same
change as a fix that matters is the exact blast-radius trade phase 1 declined.
Follow-up 2.

**No client change.** The invariant the client enforces is
`ingest_timeout > server_synth_timeout`. Before: 45 > 20, satisfied only by
accident of the two defaults disagreeing. After: 45 > 30, satisfied with exactly
the 15s of headroom `_INGEST_TIMEOUT_HEADROOM` exists to provide. This change
makes the client's stated design intent true for the first time; it does not
break any ordering constraint, and nothing on the client needs editing.

One caveat worth recording rather than fixing: the two processes share the env
var **name** `DECISION_SYNTH_TIMEOUT_SECONDS` while reading it independently.
Setting it in the server's `.env` (or compose) does not reach the client, and
setting it in a developer's shell reaches the client only. That is documented in
both `CLAUDE.md`s and is pre-existing; it is a trap for anyone who assumes one
knob.

**Live measurement was run by the team lead, not by this change.** The
implementation was written against phase 1's numbers (same box, same model, same
endpoints) because this agent was instructed not to touch the VPS. The two
claims flagged as estimates — the token count for a full board, and whether a
1–3 question board lands inside 30s — were then measured and are in "Honest
ceiling" above. One was wrong (tokens, high by ~2x) and the conclusion held
anyway. **The lesson is the flagging, not the luck:** an estimate labelled as an
estimate got checked; the "~10ms" recall figure, which was stated as fact, did
not, and was wrong by two orders of magnitude.

---

## Gates

| Gate | Run from | Result |
|------|----------|--------|
| `python -m pytest tests/ -q` | `cortex/` | **1576 passed, 30 skipped, 0 failed** (baseline 1565 + 11 new) |
| `python -m ruff check .` | repo root | **All checks passed!** |
| `python -m pytest tests/test_forbidden_tokens.py -q` | **repo root** | **21 passed** |

The third gate's working directory is load-bearing (phase 1's note):
`tests/test_forbidden_tokens.py` exists only at the repo root, so running it from
`cortex/` exits 4 with no tests collected and reads like a failure.

---

## Tests changed, and why

| File | Change |
|------|--------|
| `cortex/tests/test_decision_synthesize.py` | `_settings()` became a plain class instead of a `MagicMock` — `app.llm` type-guards `LLM_BASE_URL` to `str`, so a MagicMock resolves to "no native root" and every endpoint assertion would have been testing the fallback by accident — with `LLM_NATIVE_CHAT="never"` pinned (phase 1's trap: on the default `auto` these would fire a real `GET http://ollama:11434/api/version`, which happens to fail in CI and happens to land back on `/v1`, passing for a reason unrelated to what they assert and flipping on any dev box running ollama). **9 tests added**, the first in this file to exercise `_llm_suggest` at all: suggestions landing on their questions; connect error → `degraded` **and evidence intact**; HTTP error → degraded; unparseable completion → degraded; JSON array instead of object → degraded; the deleted `reasoning` fallback; the `/v1` body carrying standard OpenAI fields only and **no** `max_tokens`; the native body (`stream:False`, `think:False`, `format:"json"`, `options` without `num_predict`); and the one budget reaching both endpoints — that last one **parametrised over both**, since it originally asserted only `/v1` (`_Settings` pins `LLM_NATIVE_CHAT="never"` and nothing overrode it) and so had not earned the "both" in its name. |
| `cortex/tests/test_decision_config.py` | Default 20.0 → 30.0, with a comment saying *why* the number is pinned (it is a contract with a second process). **2 tests added** asserting `docker-compose.yml`'s `${DECISION_SYNTH_TIMEOUT_SECONDS:-N}` and `.env.example` both agree with the code default — phase 1's first trap, mechanised so the next person cannot hit it. |
| `cortex/tests/test_decision_api.py` | **Unchanged** — it patches `synthesize_board` itself, above the seam that moved. |

Every pre-existing assertion about a returned board is unchanged; those are the
contract proving the conversion is behaviour-preserving apart from the two
intended changes (`degraded` now tells the truth, and the endpoint moved).

---

## Follow-ups

1. **Bound the suggestion output in the PROMPT**, not with a token cap: "at most
   2 answers and 2 actions per question, ≤12 words each". That is what makes a
   full 8-question board fit a 30s budget on CPU. It changes output semantics and
   is unmeasured, so it is not in this change.
2. **OPEN QUESTION — suggestion COVERAGE may be broken independently of
   latency.** In the team lead's live probe, qwen3:4b returned only **2
   top-level question keys regardless of board size** — the same for a
   1-question and an 8-question board. That probe used a *simplified* prompt,
   not the production `_SUGGEST_SYSTEM_PROMPT`, so do not over-conclude from it.
   But if it reproduces with the real prompt, a board would come back with two
   questions carrying suggestions and the rest silently empty — and the grounding
   loop in `synthesize_board` would render that as `degraded=False`, because
   partial coverage is indistinguishable from "the model had nothing to add for
   those". Same class of defect as phase 1's follow-up on skill drafts echoing
   `_DOC_LLM_PROMPT`'s placeholders: qwen3:4b not adhering to an output schema.
   **Unverified with the production prompt — check before trusting coverage.**
3. **`sleep_cycle.py`** (phase 2(b)) — check `GET /ops/queues` `event_dlq` first;
   needs `chat_sync()`.
4. **Phase 3** — `dreams/`, plus rewriting `config.py`'s dreams caveat and
   `dreams/synthesize.py`'s docstring, which still instruct the reader to
   repoint `LLM_BASE_URL` (advice the embeddings grep makes actively harmful).
5. The split-brain state stands and is documented in both `CLAUDE.md`s: five
   converted sites, six not. **Do not sweep the rest in one unreviewed change.**
