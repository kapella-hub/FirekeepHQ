# LLM endpoint phase 3 — structured outputs

**Date:** 2026-08-04
**Files:** `cortex/app/llm.py`, `cortex/app/decision/synthesize.py`, `cortex/tests/test_llm.py`, `cortex/tests/test_decision_synthesize.py`, `CLAUDE.md`, `cortex/CLAUDE.md`
**Predecessors:** `2026-08-04-llm-endpoint-phase1.md`, `2026-08-04-llm-endpoint-phase2.md`

## The defect

Phase 1 moved the Decision Board's suggestion pass onto ollama's native
`/api/chat`. Phase 2 raised its budget 20s → 30s and stopped `_llm_suggest`
swallowing exceptions. Both were necessary. Neither was sufficient.

Verified on the deployed VPS after phase 2: `synthesize_board` completed in
**15.07s** (no timeout), returned **`degraded: False`**, and every question came
back **`answers=0 actions=0`**. Dumping `_llm_suggest`'s raw return showed the
model echoing the **user message** back —

```json
{"context": "Rolling out memory consolidation to production.",
 "questions": [{"id":"q0","text":"...","evidence_snippets":[]},
               {"id":"q1","text":"...","evidence_sn:":[]},
               {"id":"q2","text":"...","evidence_snippets":[]}]}
```

— not even cleanly (`"evidence_sn:"` is a corrupted key). The system prompt's
`{question_id: {suggested_answers, suggested_actions}}` contract was ignored.

**Root cause.** `format: "json"` (native) and `response_format:
{"type":"json_object"}` (`/v1`) constrain output to *syntactically valid JSON*
and nothing more. Neither enforces a schema. A small model handed a JSON input
under a "be valid JSON" constraint reproduces the input's shape. Phase 1
recorded the same class of failure in skill drafting, where qwen3:4b echoed
`_DOC_LLM_PROMPT`'s template placeholders instead of filling them in.

Then `synthesize_board`'s grounding loop found no `q0`/`q1`/`q2` keys, left every
question empty, and — because nothing raised — reported `degraded: False`. That
is the *same* "healthy while producing nothing" shape phase 2 closed one level
up, and it is why the endpoint and budget fixes both looked like they had
landed.

## Step 1 — wire verification, before any code

All measurements: live VPS, inside `firekeep-cortex-api-1`, ollama **0.32.4**,
**qwen3:4b**, 4 vCPU, against `http://ollama:11434`. The suggestion prompt is
`_SUGGEST_SYSTEM_PROMPT` verbatim with the three-question payload above.

### Adherence — the decisive result

| Probe | `format` | Latency | Top-level keys | Grounded |
|---|---|---|---|---|
| A | `"json"` | 20.62s | `['context','questions']` | **0/3** |
| A2 | `"json"` (rerun) | 16.31s | `['context','questions']` | **0/3** |
| B | `<json schema>` | 16.55s | `['q0','q1','q2']` | **3/3** |
| B2 | `<json schema>` (rerun) | 14.81s | `['q0','q1','q2']` | **3/3** |
| C | `<schema + minItems:1>` | 24.51s | `['q0','q1','q2']` | 3/3 |

Adherence goes 0% → 100%, twice each, and **latency does not get worse** — a
constrained decode stops emitting the wasted tokens, which roughly cancels the
grammar's cost. `minItems:1` was measured and **rejected**: no adherence gain
over B, so it buys only latency plus pressure to invent a suggestion where the
model has none.

### Latency vs question count (schema-constrained)

| Questions | Latency | Output tokens | Grounded |
|---|---|---|---|
| 1 | 8.16s | 43 | 1/1 |
| 3 | 22.95s | 126 | 3/3 |
| 8 | 52.75s | 328 | 8/8 |

A schema does **not** make a big board fit a small budget. Phase 2's finding
stands: the binding constraint is wall clock at ~6.5 tok/s, and the 8-question
board still exceeds `DECISION_SYNTH_TIMEOUT_SECONDS=30`. The remaining lever is
the prompt, not the grammar and not the budget.

### The fallback's cost, measured

A `format` the handler cannot convert is refused **pre-generation in 0.27s**:

```
HTTP 400 :: Field 'json_schema': JSON schema conversion failed:
            Unrecognized schema: {"type":"not_a_real_type","properties":7}
```

That is what makes retry-without-the-schema the right shape for the non-Ollama
safety net — it costs a quarter of a second, not a second generation.

### Not confirmed on the wire

Ollama's `/v1` handling of `response_format: {"type":"json_schema", ...}` was
attempted and **abandoned**: on this thinking model the `/v1` call had not
returned after ~20 minutes (consistent with phase 1's 83.19s–288.9s findings,
scaled to a three-question board), and it was monopolising the backend. The
shipping path on every ollama deploy is native, and the `/v1` body uses the
**standard OpenAI shape**, so this is an untested-but-standard branch guarded by
the fallback below rather than a guess. Stated here rather than implied.

## The change

### 1. `cortex/app/llm.py` — optional schema, threaded to both bodies

`chat()`, `build_native_body()` and `build_openai_body()` gain
`json_schema: dict | None` (plus `json_schema_name: str = "response"` for the
OpenAI envelope's required `name`).

- **native:** the schema object goes in `format` — the same field that otherwise
  carries the string `"json"`. A schema supersedes `json_mode` rather than
  combining with it; one field, one value.
- **`/v1`:** `response_format = {"type":"json_schema", "json_schema": {name,
  strict: true, schema}}`. `strict: true` is what makes the schema binding
  rather than advisory on OpenAI. This is a standard `response_format` type, so
  the module's "standard OpenAI fields only" rule is intact.

**Additive by construction.** A caller that passes no schema gets bodies
byte-identical to the pre-change ones — asserted directly, because this is a
helper shared with the classifier, the skill synthesizer and dreams.

**A schema implies `json_mode`,** coerced once at the top of `chat()`. A caller
may reasonably pass only `json_schema`; without the coercion the schema-dropped
fallback would carry no output constraint at all, converting a backend's polite
400 into free-form prose and a `JSONDecodeError` — strictly worse than the
rejection it was recovering from.

### 2. Non-Ollama safety — the ladder, not a flag

There is no capability endpoint to feature-detect structured outputs against, so
`chat()` reuses the mechanism already in the file. Attempts run
**most-capable-first, each rung dropping exactly ONE capability**, so a
pre-generation 4xx is answered by giving up the least it can:

| Preferred endpoint | Ladder |
|---|---|
| native, schema | (native+schema) → (native) → (`/v1`) |
| native, no schema | (native) → (`/v1`) — *unchanged from phase 1* |
| `/v1`, schema | (`/v1`+schema) → (`/v1`) |
| `/v1`, no schema | (`/v1`) — *unchanged; still no retry* |

A vLLM/LiteLLM/OpenAI deploy that rejects `json_schema` therefore keeps working
at pre-schema quality instead of failing. Two deliberate properties:

- **Dropping the schema natively does not demote the native verdict.** The
  endpoint was never the problem; demoting would push every later call in the
  process onto the 83.19s `/v1` path for an unrelated fault. `_demote` fires
  only on the native→`/v1` transition.
- **There is no `/v1`+schema rung under a failed native+schema.** On ollama both
  endpoints are the same engine, so a schema the native handler rejects will not
  be honoured by its own `/v1`; that rung buys a wasted round trip.

**`422` joins `_DEMOTE_STATUS_CODES`** (now `{400, 404, 405, 422, 501}`). vLLM's
OpenAI-compatible server is FastAPI, whose request-validation rejection is a 422
rather than a 400. It satisfies the set's one criterion — pre-generation —
exactly like the other four, so it is safe on the endpoint-demotion path too.

### 3. `cortex/app/decision/synthesize.py` — the schema, and the missing check

`_suggestion_schema(question_ids)` builds a schema naming every `q0..qN` in
`properties` **and** `required`, with `additionalProperties: false` at both
levels. `required` is the load-bearing half: `properties` alone describes a shape
the model may decline to produce, while naming every id in `required` is what
makes the mirrored-input answer *ungrammatical* rather than merely discouraged.
`additionalProperties: false` closes the same door from the other side and is
also what OpenAI's `strict: true` requires. No `minItems`, per the measurement
above. A board with **no** questions sends no schema (it would constrain output
to the literal `{}`).

`synthesize_board` now counts what actually grounded and refuses to call a board
healthy when the answer is nothing:

| Condition | `degraded` | `note` |
|---|---|---|
| ≥1 question got a suggestion | `False` | `""` |
| payload named **none** of the board's ids | `True` | `suggestions-unusable` |
| ids matched, every list empty | `True` | `suggestions-empty` |
| exception (timeout/transport/HTTP/parse) | `True` | `retrieval-only` |

The two new notes are distinguished because they need different responses:
`unusable` means the model answered a different question than the one asked (a
prompt/schema/backend problem), `empty` means it answered *this* one with
nothing (a retrieval or model-capability problem). `matched` counts ids
**present**, not ids answered, which is what makes them separable. The warning
logs the offending top-level keys — the one line that would have named this
defect in an hour instead of three phases.

A board that grounded *something* is deliberately **not** degraded. Over-reporting
would make the flag mean "a board was served" and cost it the meaning this change
gives it.

The grounding loop's `suggestions.get(id) or {}` also became an `isinstance`
guard: a non-dict value raised `AttributeError` **halfway through the loop**,
leaving the questions before it assigned and every one after it untouched. One
malformed entry is not a reason to drop the good ones.

## Considered and deliberately not changed

**`knowledge/classifier.py` — measured, left alone.** It already adheres, and
the bar was "a clear improvement, with no regression". Measured on the same
backend with a real two-procedure runbook:

| | Run 1 | Run 2 | Output |
|---|---|---|---|
| no schema (ships today) | 16.12s | 5.31s | `{"primary_type":"procedural","procedure_titles":["Restart the ingest worker","Rotate the Confluence PAT"]}` |
| with schema | 10.34s | 5.59s | *byte-identical* |

Identical output, latency inside the noise, 100% valid both ways. That is not a
clear improvement, and a schema is not free: it must be kept in step with
`_VALID_PRIMARY_TYPES` and the parse code, and an `enum` in the grammar would
silently coerce where the existing `primary_type not in _VALID_PRIMARY_TYPES →
"mixed"` guard does its documented job. A new coupling on a path that works is a
new failure mode, not insurance.

**`skills/synthesizer.py` — a schema does not apply.** Its `_chat` sends
`json_mode=False`; a skill card is header-plus-Markdown parsed by
`parse_skill_content`, not JSON. There is no JSON for a schema to constrain.
Phase 1's placeholder-echo there is real and unfixed, but it needs a prompt fix,
not a grammar.

**Everything else on phase 1's not-converted list stays not-converted:**
`workers/sleep_cycle.py`, `dreams/`, `engine/rag.py`, `mcp_server.py`.

## Verification

**Live gate, the new code against the real backend** (loaded in-process over the
deployed modules via `sys.modules` injection — nothing on the VPS's disk or
`.env` was modified, and the probe files were removed afterwards). Same
three-question board as the failing repro:

```
elapsed=19.66s degraded=False note=''
q0: Should the feature default to enabled or disabled?
   answers = ['Disabled']
   actions = ['Set feature flag to disabled by default']
q1: Which deployment should get it first?
   answers = ['Staging environment']
   actions = ['Deploy to staging first']
q2: What metric decides whether it worked?
   answers = ['Memory usage reduction percentage']
   actions = ['Track memory usage before and after rollout']
```

Terse, but concrete, on-topic and actionable — and 3/3 where the deployed build
returns 0/3.

**Gates:** `cortex` suite **1610 passed / 0 failed** (baseline 1577 + 22 added
here + the concurrent confirmed-memory work); `ruff check .` clean;
`tests/test_forbidden_tokens.py` 21 passed.

**Guards added.** `test_llm.py` +15: both body shapes, the no-schema
byte-identity check, schema-supersedes-json_mode, the json_mode coercion, the
`/v1` reject-and-retry across all five demote codes, 5xx-not-retried, and each
rung of the ladder including the two deliberate omissions. `test_decision_
synthesize.py` +7: schema shape (`properties` + `required` + `additionalProperties`),
absence of `minItems`, no-schema-for-an-empty-board, the **actual mirrored-input
payload** → `suggestions-unusable`, ids-matched-but-empty → `suggestions-empty`,
partial grounding → not degraded, and one malformed entry not abandoning the rest.

Two pre-existing tests were updated rather than added to, both deliberately:
`test_openai_body_carries_no_vendor_flags_and_no_output_cap` and
`test_native_body_when_the_backend_is_ollama` pinned the *pre-schema* body for
this path (`{"type":"json_object"}` / `format == "json"`) — precisely the setting
under which the live board answered 0/3 twice. They now assert the schema
envelope; their vendor-flag and no-`max_tokens` halves are unchanged.
