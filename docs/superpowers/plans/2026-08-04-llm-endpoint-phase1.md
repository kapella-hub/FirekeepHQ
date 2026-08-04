# LLM endpoint selection — phase 1 (implementation record)

Design: `docs/superpowers/specs/2026-08-04-llm-endpoint-selection.md`.
Scope: steps 0–10 of that design's implementation sketch. Phases 2 (decision
board, sleep_cycle) and 3 (dreams) are deliberately NOT in this change.

Commits, in order:

| SHA | What |
|-----|------|
| `62a2938` | `feat(cortex): app/llm.py — select ollama's native /api/chat over /v1` |
| `6aca7e4` | `fix(cortex): classify via app/llm; a read timeout is not "backend unavailable"` |
| `e429604` | `fix(cortex): skill synthesis via app/llm, with an output bound and an empty guard` |
| `3863b84` | `fix(cortex): drop the native skill-synth timeout — drafting is generation-bound` |
| (this file) | `docs(cortex): record the phase-1 LLM endpoint change` |

---

## STEP 0 — wire verification (done BEFORE any code was written)

All probes run inside `firekeep-cortex-api-1` / `firekeep-cortex-worker-1` on
the production VPS against `http://ollama:11434` — ollama **0.32.4**, model
**qwen3:4b**, 4 vCPU. Every probe used the SAME two-procedure runbook document.

```
GET /api/version                                    -> 200 {"version":"0.32.4"}

A  /api/chat  {stream:F, think:F, format:"json",
               options:{temperature:0.1, num_predict:800}}
                                                    -> 200, 16.18s COLD, valid JSON
B  same, WARM                                       -> 200, 4.00s (load_duration 0.25s)
C  same, "stream" OMITTED                           -> 200 but NDJSON: 35 lines;
                                                       json.loads -> JSONDecodeError
                                                       "Extra data: line 2 column 1"
D  same, "think" OMITTED                            -> 200, 111.20s,
                                                       message.thinking = 3552 chars
E  /v1/chat/completions, EXACTLY the pre-change
   classifier body                                  -> 200, 83.19s,
                                                       reasoning 1978 chars,
                                                       26 content tokens
```

**Findings the design asked to be confirmed from the wire:**

1. `format` and `think` ARE top-level siblings of `stream`. `num_predict` and
   `temperature` ARE inside `options`. Confirmed by probe A returning 200 with
   correct JSON.
2. `"stream": False` IS mandatory — probe C is the proof, not an argument.
3. Response shape is
   `{model, created_at, message:{role, content[, thinking]}, done, done_reason,
   eval_count, eval_duration, load_duration, prompt_eval_count,
   prompt_eval_duration, total_duration}`.
4. **Not in the design, and it would have caused a crash:** with `think:false`
   the `message` dict is exactly `['content', 'role']` — the `thinking` key is
   **absent entirely**, not present-and-empty (contrast probe D, where it
   appears). `parse_native_response` therefore uses `.get()`; the subscript a
   reader of probe D's output might have written would raise on every
   successful call.
5. Probe D shows the lever is the FLAG, and probe E shows `/v1` will not apply
   it. 111.20s (native, thinking on) vs 4.00s (native, thinking off) vs 83.19s
   (`/v1`, flag sent and ignored).

---

## What shipped

**New:** `cortex/app/llm.py` — pure helpers (`native_root`, `build_native_body`,
`build_openai_body`, `parse_native_response`, `parse_openai_response`), a lazy
TTL-cached probe (`is_native`, `reset_probe_cache`), and one entry point
`chat(...) -> ChatResult`. All five measurements live in the module docstring.

**Converted:** `knowledge/classifier.py` (one call), `skills/synthesizer.py`
(`_call_llm` and `_call_llm_doc` collapse into one `_chat`). `_embed` untouched.

**Also fixed, same release:** `_is_backend_unavailable` no longer treats a read
timeout as an absent backend; the classifier's `reasoning`-field fallback is
deleted; both synthesis calls gained an output bound and an empty-completion
guard.

**Config:** `LLM_NATIVE_CHAT` (`auto`), `LLM_NATIVE_PROBE_TTL_SECONDS` (600.0),
`LLM_NATIVE_BASE_URL` (""), `KNOWLEDGE_CLASSIFY_NATIVE_TIMEOUT_SECONDS` (300.0 —
see "no timeout was actually reduced" below), `SKILL_SYNTH_MAX_TOKENS` (800).
Mirrored into `docker-compose.yml` (cortex-api + cortex-worker), `.env.example`,
root `CLAUDE.md`, `cortex/CLAUDE.md`.

---

## Where this deviates from the design, and why

### 1. Timeouts are per-ENDPOINT, and in the end NOTHING WAS REDUCED

The design said `KNOWLEDGE_CLASSIFY_TIMEOUT_SECONDS` 300 → 120 and
`SKILL_SYNTH_TIMEOUT_SECONDS` 300 → 120. Its own risk list then says a
reduction "must not land where the native path does not engage", because a
deployment whose probe says not-Ollama still takes ~289s and a 120s budget
would convert today's slow successes into guaranteed timeouts.

Both cannot be true of one number, so the configured value keeps its meaning as
the `/v1` budget (300) and a **separate** native budget is selected by `chat()`
after the endpoint resolves. That structure is right and is what shipped.

**The 120 that structure was built to carry then turned out to be wrong too.**
The design's risk was stated as "deployments where the native path does NOT
engage". The real hazard is the opposite one, and neither the design nor I saw
it: a deployment where the native path DOES engage and buys nothing.

**"Native" does not imply "fast".** The native path is faster only because it
disables THINKING, and the probe confirms *ollama*, not *a thinking model*. The
office deploy runs **llama3.2:3b, a non-thinking model** — the probe confirms
native, it takes the native path, and `think:false` disables something that was
never happening. Its recorded ~56s classify stays ~56s while its headroom falls
5.4x → 2.1x. `classify_document` sends the whole document untruncated and the
crawler admits 2MB pages, so a document ~2.2x the measured one newly times out.
The office helm chart lives in a separate config repo and sets none of these
vars, so it would have inherited the reduction with nobody deciding.

The one escape hatch that might have saved it does not fire. Measured
2026-08-04, a non-thinking model accepts `think:false` cleanly rather than
4xx-ing, so `chat`'s demote-and-retry never engages:

```
llama3:latest  + think:false  -> OK 3.10s  keys=['content','role']
llama3:latest  WITHOUT flag   -> OK 0.36s  keys=['content','role']
gemma3:4b      + think:false  -> OK 1.94s  keys=['content','role']
```

Against all that, the upside was small: a native classify measures ~6s, so 120
vs 300 only changes how fast a **broken** call gives up. So
`KNOWLEDGE_CLASSIFY_NATIVE_TIMEOUT_SECONDS` **defaults to 300**. The knob stays
— it is genuinely useful as a separately tunable budget, and an operator who
wants fail-fast on a measured backend can lower it — it just no longer defaults
to a value that strands non-thinking-model deploys.

Net across the whole change: **no timeout was reduced.** The win is the ~13x
faster classify, not a tighter budget.

### 2. `SKILL_SYNTH` gets NO native budget at all

The design flagged its own 120 here as "the number I am least sure of". It was
wrong, and the live verification is what caught it — a real 3-procedure ingest
had **all three drafts fail at exactly 120.13s each**.

Measured natively, with `think:false` already in effect (so this is not a
reasoning artefact):

```
num_predict=300   40.78s  eval_count=300  done_reason=length
num_predict=400   51.10s  eval_count=400  done_reason=length
num_predict=500   69.13s  eval_count=500  done_reason=length
num_predict=800  111.87s  eval_count=800  done_reason=length   (warm)
num_predict=800  135.32s  eval_count=800  done_reason=length
```

Generation runs at **5.9–7.2 tok/s**, and `done_reason` is `length` at *every*
cap — the model never stops on its own. So skill drafting is
**generation-bound, not reasoning-bound**: the endpoint fix removes reasoning
overhead, which is most of a classify's cost and little of a draft's. The
design's "~25-45s native" extrapolated from Dreaming's 22.5s, which at this
token rate is a ~145-token output — a fifth of a skill card.

`SKILL_SYNTH_TIMEOUT_SECONDS` therefore stays 300 for both endpoints and the
native sibling was deleted rather than raised: a knob whose only safe value
equals the default invites tuning without measuring. `SKILL_SYNTH_MAX_TOKENS`
is the real cost control (`tokens ÷ ~6 tok/s` = wall clock).

### 3. Compose placement

The design said mirror the new vars into "cortex-api, cortex-mcp, worker, beat".
In fact `LLM_BASE_URL` is set only on **cortex-api** and **cortex-worker**;
cortex-mcp proxies to REST and cortex-beat only schedules. The endpoint vars are
an LLM concern, so they went where `LLM_BASE_URL` already is.

### 4. Test blast radius was smaller than predicted

The design expected every existing patch to stop intercepting. It didn't:
`patch("app.knowledge.classifier.httpx.AsyncClient")` mutates the **shared
`httpx` module object**, not a module-local name, so it still intercepts
`app.llm`'s calls. Only 2 of 29 tests failed on the first run — the two
asserting the deleted reasoning fallback.

That is not a reason to leave them alone. See "Tests changed" below.

### 5. One small hardening not in the design

`_chat` raises when the model returns blank content. `parse_skill_content("")`
returns the fallback dict whose trigger is the literal `"Synthesized skill"` —
**truthy** — so the callers' `if not trigger and not body` guard does not fire
and a contentless placeholder gets stored, which both callers' docstrings say
must never happen. An empty completion is an observed failure mode on a
thinking model, so the conversion makes this reachable enough to close now.

---

## STEP 10 — live verification on the VPS

Method: the changed files were `docker cp`'d into the running `cortex-api` and
`cortex-worker` containers and the containers restarted (no image rebuild, no
git change on the VPS). **Everything was reverted afterwards** — see "VPS state"
below.

### Before / after, same document, real `classify_document`

Alternating warm runs through the actual pipeline function:

| Endpoint | Runs | Result |
|----------|------|--------|
| `/v1` (pre-change path) | 96.67s, 66.50s, 85.36s | ok, 3/3 titles |
| native (post-change) | 6.08s, 6.29s, 5.50s | ok, 3/3 titles |

**~13x, identical classification output in all six runs.** (An initial native
run measured 31.49s cold; the 4.00s/6s figures are warm. The audit's 288.9s →
3.3s / 87x was a larger document.)

### End to end through `POST /knowledge/ingest`

First attempt (with the mistaken 120s native skill budget): classify succeeded
in **8.59s** (`classified`, `skills_queued: 3`) and then **all three drafts
failed at 120.13s each** — caught the regression, fixed in `3863b84`.

After the fix, a fresh 2-procedure document:

```
POST /knowledge/ingest        -> 202 {"status":"queued"}
  [   1.7s] status=classifying
  [  27.1s] status=classified disposition=procedural skills_queued=2
  [ 268.5s] 2 drafts present in GET /skills?status=draft
```

**Both drafts actually landed in the review queue**, which the design calls "the
whole point — today the endpoint reports 'classified, 3 skills queued' next to a
permanently empty queue."

That empty queue is real and still visible in production: `GET
/knowledge/sources` shows `Runbook: Restart stuck Celery worker`, ingested
2026-07-12, with `skills_queued: 1` and `draft_skill_count: 0`.

### Gates

| Gate | Run from | Result |
|------|----------|--------|
| `python -m pytest tests/ -q` | `cortex/` | **1565 passed, 30 skipped, 0 failed** (baseline 1504 + 61 new) |
| `python -m ruff check .` | repo root | **All checks passed!** |
| `python -m pytest tests/test_forbidden_tokens.py -q` | **repo root** | **21 passed** |

The third gate's working directory is load-bearing: `tests/test_forbidden_tokens.py`
exists only at the REPO ROOT, so running it from `cortex/` exits 4 (no tests
collected) and reads like a failure in CI output.

### VPS state after verification

Fully restored and confirmed: `llm.py` removed from both containers, the three
touched files byte-identical to `/opt/Firekeep`'s deployed checkout (`diff -q`
clean), the backup dir removed, all 12 containers healthy, and both
verification corpus sources plus their draft skills deleted.

**The fix is NOT live on the VPS.** It needs a normal deploy (merge → pull →
rebuild). During restore the worker briefly crash-looped: `docker exec` runs as
a non-root user that cannot write `/app`, so the intended in-container restore
silently failed while `llm.py` had already been removed, leaving a patched
`classifier.py` importing a missing module. Fixed within ~2 minutes via
`docker cp` (which runs as root on the host). Worth recording: **restore
container files with `docker cp`, never `docker exec cp`.**

---

## Follow-ups (NOT done here)

1. **Draft card quality is poor and this change made it visible.** The landed
   drafts have `trigger='<one sentence — what situation activates this skill>'`
   — qwen3:4b is echoing `_DOC_LLM_PROMPT`'s template placeholders instead of
   filling them in, and `done_reason=length` at every cap means it is also
   truncated. Pre-existing and unrelated to the endpoint (it reproduces with
   `think:false` on native); previously invisible only because drafts never
   landed at all. Needs a prompt fix and/or a bigger model, plus possibly a
   few-shot example.
2. **`SKILL_SYNTH_MAX_TOKENS=800` is the one body change that also lands on
   `/v1`** — recorded here rather than fixed. Every other difference is
   confined to the native branch, but the output bound applies to both, so a
   NON-Ollama backend (vLLM, LiteLLM, OpenAI) that previously emitted a longer
   skill card is now truncated at 800 tokens. Justified — an unbounded
   generation was the defect — and tunable via the setting, but it is a
   behavioural change beyond Ollama and should not surprise anyone. Note the
   truncation is not hypothetical even on Ollama: `done_reason=length` at every
   cap tried means the model always runs to the bound.
3. **Phase 2**: `decision/synthesize.py` (+ its silent-success bug, where
   `except Exception: return {}` makes `synthesize_board` report
   `degraded=False` on a board that produced nothing), and — after checking
   `GET /ops/queues` for `event_dlq` — `sleep_cycle.py`, which needs a sync
   `chat_sync()` wrapper.
4. **Phase 3**: `dreams/`, and rewriting `config.py:205-216` + the
   `dreams/synthesize.py` docstring, which currently instruct the reader to
   repoint `LLM_BASE_URL` — advice this change makes both unnecessary and
   actively harmful.
5. The split-brain state is intentional and documented in both `CLAUDE.md`s:
   four converted sites, seven not. **Do not sweep the rest in one unreviewed
   change.**

---

## Tests changed, and why

| File | Change |
|------|--------|
| `tests/test_llm.py` | **New, 54 tests.** Root derivation (incl. path-prefixed proxy, non-str type guard), body builders (`stream:False` asserted across every argument combination; no vendor flags on the OpenAI body), normalisation (absent `thinking` key), probe acceptance (2xx **and** a `version` key; a 200 without one is rejected), caching (N calls → 1 probe, per-root keying, asymmetric TTL), `auto`/`always`/`never`, the 4xx demote-and-retry, 5xx/timeout propagation, and per-endpoint timeout selection. |
| `tests/conftest.py` | Autouse fixture resetting `llm`'s module-global verdict cache. Without it a verdict decided in one test leaks into later ones, order-dependently. |
| `tests/test_knowledge_classifier.py` | 17 patch sites repointed to the `app.llm` seam. They kept working either way (see deviation 4), but naming the classifier as the caller is now false. `FakeSettings` gained `LLM_NATIVE_CHAT="never"`: on the default `auto` these tests fired a **real** `GET http://ollama:11434/api/version` which happens to fail in CI and happens to land back on `/v1` — so they passed for a reason unrelated to what they assert, and would break on a dev box running ollama. Two reasoning-fallback tests **rewritten** to assert the honest failure that replaces the deleted rescue (same terminal state in the field, so no coverage is lost). Three tests added: read-timeout vs connect-timeout, and the native body end to end. |
| `tests/test_skill_synthesizer.py` | `LLM_NATIVE_CHAT="never"` pinned on both settings builders (same accidental-pass problem). Four tests added: `max_tokens` on the `/v1` body, `options.num_predict` on the native body, blank content raising, and blank content surfacing as `synthesis_failed` with nothing written to Qdrant. |
| `tests/test_knowledge_e2e.py` | **Unchanged** — it patches `SkillSynthesizer._call_llm_doc` directly, above the seam that moved. |

No assertion about a returned dict was weakened; those are the contract that
proves the conversion is behaviour-preserving.
