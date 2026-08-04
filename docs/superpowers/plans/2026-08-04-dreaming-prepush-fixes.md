# Dreaming — pre-push fixes from live end-to-end validation

**Date:** 2026-08-04
**Status:** Applied
**Scope:** `cortex/app/dreams/`, `cortex/app/config.py`, root + cortex `CLAUDE.md`,
`docs/superpowers/specs/2026-08-04-dreaming-design.md`
**Trigger:** a live end-to-end run of the just-merged Dreaming feature (HEAD `837741a`)
against a real Ollama 0.17.5 + Qdrant deployment.

This document is the record of what live validation found, because most of it could not
have been found any other way. Every defect below is one the merged code's own tests
passed over, and two of them (P3, P4) are cases where the feature reported success while
producing nothing.

---

## P1 — a real person's legal name and workstation hostname were in shipped source

`cortex/app/dreams/profile.py`'s module docstring listed the seven measured `agent_id`
values verbatim, including a named individual's full legal name (surname-first form), a
bare first name, an OS username, and a hostname-derived agent id. That file ships inside
the customer-facing cortex Docker image. The same list appeared in the root `CLAUDE.md`
and in the design spec — both new to this push, so it would have been the first time the
full legal name reached the remote.

Anonymised in all three places. The argument is measured and is preserved intact: **one
human appeared under SEVEN distinct `agent_id` values while `member_id` was uniform
across all 538 active memories**, which is why profiles key on `member_id`. Placeholders
describe the *shape* of each value (`agent-<host>-<hash>`, `<Surname, Forename>`,
`<first-name>`, `<username>`) alongside the three literal sentinels that are not personal
data (`unknown`, `default`, `legacy-pre-team-continuity`). The count, the uniformity
claim and the conclusion are unchanged; each site now also says explicitly that the values
are placeholders and why.

Tree-wide grep for the surname / `marat` / `marat_pc` / `MARAT-PC` afterwards returns only
`docs/superpowers/specs/2026-07-28-workspace-awareness-design.md` (lines 89, 90, 130),
which pre-dates this push and was explicitly out of scope. **Flagged, not fixed:** that
file still carries a first name and a workstation hostname. It is a design spec, not
shipped source, so the exposure is smaller — but it is the same class of leak and worth a
separate pass.

## P2 — `CLAUDE.md` stated a rule the code deliberately breaks

The doc said, under a heading naming `profile.py`, that written `memory_type` is
"`procedural`, never `reference`". `profile.py:104` writes `"memory_type": "reference"`,
and its own docstring calls that "the one deliberate exception".

This was not pedantry. `reference` → `DECAY_REFERENCE_DAYS=0` → `rag.py` skips decay
entirely, and profiles *are* returned by ordinary `memory_recall` — so profiles really do
get the permanent rank immunity that sentence says an auto-approved LLM memory must never
have.

The sentence is now scoped to **cluster insights** (where `parse_insights` genuinely
forces `procedural` regardless of model output), and the profile exception is stated
explicitly with its real rationale: a profile is **replaced in place** at a deterministic
point id on every run rather than accumulating, so there is no pile of stale profiles for
decay to thin, and decaying the single current profile would only demote the freshest
statement of who a person is. The blast radius is named too — a profile cannot feed its
own future clustering (`select.is_candidate` accepts only `episodic`-or-missing types, and
`_scope_filter` excludes `source="dream_profile"`).

## P3 — `max_tokens: 700` starved synthesis on the documented reference configuration

**Measured, not inferred.** `build_request_body` sends both `"think": False` and
`"chat_template_kwargs": {"enable_thinking": False}`. Ollama honours them on its **native
`/api/chat`** and **silently ignores both on `/v1/chat/completions`** — which is exactly
where `synthesize()` posts, because `LLM_BASE_URL` ends in `/v1`.

Live probes against Ollama 0.17.5 with the production request body, 3 of 3:

| | value |
|---|---|
| HTTP status | 200 |
| `finish_reason` | `length` |
| `completion_tokens` | 700 — i.e. exactly the hardcoded cap |
| content length | **0** |
| reasoning length | ~3200 chars |

The identical call at `max_tokens=4000` returned correct JSON. So this is **budget
starvation**, not a broken model and not a broken flag: the reasoning runs regardless, the
JSON grammar blocks it from being emitted as content, and the cap is reached before a
single content token exists.

Field impact: **2 of 3 clusters produced zero insights, reproducibly, across two full
runs.** Worse, a zero-insight cluster is never added to `dreams:consolidated`, so those
clusters were re-selected and re-attempted on *every* future run — a permanent treadmill
that could never succeed.

**Fix.** `synthesize._MAX_COMPLETION_TOKENS = 4000`, a named module constant carrying the
measurements above in its comment. The flags stay: they are correct and they do work
against `/api`, and a comment now says plainly that the `/v1` path ignores them, which is
*why* the budget must accommodate reasoning tokens. The module docstring — which
previously claimed the flags made this a non-issue — now says so too.

**Why a module constant rather than config** (the question was asked explicitly):
`DREAM_MAX_INSIGHT_CHARS` caps the *content* of each insight (800 chars, enforced by
`parse_insights`), while the tokens this budget must cover are overwhelmingly reasoning
the content cap knows nothing about. Deriving one from the other would make raising the
content cap silently shrink the reasoning headroom, and vice versa. `build_request_body`
is also a pure function shared with `profile.py` with no `Settings` in scope; making it
configurable means threading settings through two call sites to expose a knob nobody has a
reason to turn.

> ### ⚠ Flagged, deliberately not fixed: this raises worst-case wall time
>
> On `/v1` the reasoning tokens now actually get **generated** rather than truncated at
> 700. `DREAM_SYNTH_TIMEOUT_SECONDS = 45.0` is the only control that binds under
> `--pool=solo`, and it was sized as 2× the **22.5s** measurement taken on the path where
> `think:false` *is* honoured. On slow CPU inference the call can now exceed 45s and be
> cut off, where it previously returned fast-and-empty.
>
> Both outcomes are zero insights, so this is not a regression — and the new behaviour is
> strictly more visible (`synthesize()` logs a WARNING, and P5 makes `GET /dreams` report
> `degraded` rather than `ok`). I did **not** raise the timeout: 45.0 was derived from a
> measurement and argued in `config.py`, and raising it to a number I have not measured
> would be exactly the sin that comment exists to prevent.
>
> **Recommended remedy for such a deployment, in order:** point `LLM_BASE_URL` at Ollama's
> `/api` (where `think:false` works and 22.5s is the measured latency), or raise
> `DREAM_SYNTH_TIMEOUT_SECONDS` *with a measurement behind it*. Both `config.py` and the
> constant's comment now state this interaction.

## P4 — the generation gate probed the endpoint, not the model

`_generation_backend_available` did `GET {LLM_BASE_URL}/models` + `raise_for_status()` and
nothing else. **Proven live:** with `LLM_MODEL=this-model-does-not-exist:7b` the probe
returned `True`, the pass then walked the **entire backlog** (3 clusters + 2 profile
groups) marking every unit done with zero output, stamped `last_completed_at`, and
reported `status=complete health=ok` — the exact false-complete this gate was added to
prevent, just moved one level down.

The repo already solved this correctly on the client side. `_generation_backend_available`
now checks the configured model against the returned list, mirroring
`client/firekeep_client/nightshift.py::_model_available` — including its Ollama tag
tolerance (bare vs `:latest`, compared both directions, since `/v1/models` reports fully
tagged ids while the chat API resolves bare names).

**Leniency follows the same precedent exactly:** an empty or unreadable list means "cannot
tell", never "absent". Only a list that is both readable *and* demonstrably missing the
model returns `False`. A wrong veto stops the feature entirely and reports `unavailable`;
a wrong pass costs one real call that fails loudly on its own terms — the asymmetry runs
one way.

Tests: model present → True; model absent from a readable list → False; five
unreadable/empty shapes → True (lenient); tag-form matching both directions; and an
end-to-end `run_one_unit` case with the **real** probe in the loop asserting a missing
model closes the gate instead of walking the backlog to `complete`.

## P5 — `GET /dreams` could not tell a productive run from a barren one

A run that wrote 6 dreams and a run that wrote 0 both returned
`{"clusters_done":3,"profiles_done":2,"errors":0,"health":"ok"}`. That is how P3's
starvation passed for healthy across two full runs.

Two changes.

**`insights_written` is now a real per-run cumulative total.** `record_run` was already
being handed an `insights_written`, but it was *that tick's* count, so the run hash held
whatever the last tick happened to do — a run writing one insight on each of two ticks
reported `1`. It is now a `dreams:counter:insights_written` bumped per written insight,
cleared by `reset_progress` like every other per-run tally, and mirrored into the
`dreams:run` hash on every tick that does work.

The endpoint reads it from the **hash**, deliberately unlike `errors`: the counter is
cleared at completion, so reading the counter would report `0` for the run that just
finished — precisely the moment an operator wants the number. (`errors`' opposite argument
does not transfer: *nothing* ever writes `errors` into the hash, so a mirror there would
be a value that only goes stale.)

**`health` now reflects reality.** A completed run that attempted ≥1 cluster and wrote
zero insights records `health="degraded"`. `errors` structurally cannot cover this —
`synthesize()` never raises by contract, so `run_one_unit`'s outer `except`, the only
thing that bumps `errors`, is unreachable on an LLM failure.

Deliberate limits, stated rather than hidden: a run that attempted **no** clusters stays
`ok` (nothing to write is not a failure, and reporting it as degraded would make the
signal worthless), and a profile-only run that wrote no profiles is also `ok` — a profile
write is not an insight, and this counter measures one thing.

## P6 — `parse_profile` accepted an LLM refusal as a profile

A live run stored and then **served through the briefing**: *"No human is mentioned in the
memories. The text describes system behavior…"*. `parse_profile` rejected only
empty/whitespace and over-budget text, so a refusal was a perfectly valid profile — and
because a profile is replaced in place at a deterministic point id, that non-answer
overwrote whatever real profile was there.

**Decision: I added the guard rather than documenting the behaviour**, because the failure
is not merely "no profile this run" — it *replaces* a good profile with a non-answer that
then gets injected into every subsequent briefing. Leaving that in place while calling it
a known limitation understates it.

`_looks_like_refusal` matches a set of specific refusal phrases against the **first 200
characters only**. Two choices keep the heuristic from doing more harm than the bug:

- **Windowed to the opening.** A real profile may legitimately contain "there is no
  evidence that…" mid-body; rejecting that would be worse than the defect. A refusal lives
  in the opening clause, so that is the only place worth looking. A test pins exactly this
  case, asserting the negation falls outside the window.
- **Specific verb phrases, not bare negations**, for the same reason.

It is **explicitly documented as a heuristic and not a guarantee** — a differently-worded
refusal still gets through — in both `profile.py` and `CLAUDE.md`. Rejected as too
fragile: requiring the profile to mention `member_id`, which is an opaque `member-<hex>`
handle a valid profile has no reason to quote, so that check would reject nearly every
good profile.

The asymmetry the guard trades on: a false reject costs one skipped refresh (the group is
marked done for this run, picked up on a later one, previous profile intact); a false
accept poisons every briefing until the next successful run.

## P7 — stale comment

`task.py:30` cited `DREAM_SYNTH_TIMEOUT_SECONDS=120s worst case`; `config.py` now sets
`45.0` and argues against 120. Corrected, with a pointer to `config.py` for the reasoning
and a note that 120 was the value the setting held before it was re-derived from the
22.5s measurement.

---

## Verification

| Gate | Result |
|---|---|
| `cd cortex && python -m pytest tests/ -q` | **1504 passed**, 30 skipped (was 1478 passed; +26 new) |
| `cd benchmarks/memory && python -m pytest tests/ -q` | **87 passed** |
| `python -m ruff check .` | **All checks passed** |
| `python -m pytest tests/test_forbidden_tokens.py -q` | **21 passed** |

New/changed tests: `cortex/tests/test_dreams_task.py` (P4 probe semantics + tag matching +
end-to-end missing-model gate; P5 cumulative total, degraded verdict, no-clusters-is-still-ok,
per-run reset), `test_dreams_api.py` (P5 `insights_written` surfaced, productive-vs-barren
distinguishability, hash-not-counter read), `test_dreams_state.py` (P5 every per-run counter
is cleared), `test_dreams_synthesize.py` (P3 budget floor), `test_dreams_profile.py` (P6 the
live refusal string, four other refusal shapes, and the mid-body-negation false-positive case).

## What was deliberately not done

- **Archival** — round 1 stays additive.
- **`DREAM_ENABLED`'s default** — unchanged (`False`).
- **`docs/superpowers/specs/2026-07-28-workspace-awareness-design.md`** — pre-existing, out
  of scope. It still contains a first name and a workstation hostname (see P1).
- **Raising `DREAM_SYNTH_TIMEOUT_SECONDS`** — see the flagged box under P3. Documented in
  `config.py` and in the `_MAX_COMPLETION_TOKENS` comment instead of changed without a
  measurement.
