# Pi runtime validation

Live-validation record for the `pi` runtime, following the kiro (2026-07-13) and
opencode (2026-07-18) precedents. **Nothing in the capability matrix may claim a
Pi behaviour that is not asserted by `client/pi/validation/run.mjs`.**

- **Pi version:** `@earendil-works/pi-coding-agent` 0.84.4
- **Bridge:** `firekeep-pi` (`client/pi/`), loaded from source via jiti
- **Date:** 2026-08-29
- **Result:** 16/16 assertions pass
- **Reproduce:** `cd client/pi && npm install && node validation/run.mjs`

## What the harness actually proves

The suite runs a **real Pi agent session** — no mocks of Pi itself. Two things
are substituted, both deliberately:

1. **The dispatcher.** A stub `firekeep_client` on `PYTHONPATH` records every
   `python -m firekeep_client.hooks <core>` invocation and returns a controllable
   exit code. This is what makes the block/allow pair a *controlled experiment*:
   the only difference between run 1 and run 2 is `pre_tool`'s exit code.
2. **The model.** Pi's own `registerFauxProvider` scripts the assistant turn, so
   a `write` tool call happens deterministically with no credential, no network
   and no spend. The faux response factory receives the real `Context`, which is
   how the system-prompt assertion is made against what the model was *actually
   handed* rather than against what the bridge intended to send.

## Results

| # | Assertion | Result |
|---|---|---|
| 1 | model was actually called | PASS |
| 2 | `prompt` core fired before the model call | PASS |
| 3 | **briefing reached the MODEL system prompt** (2735 chars captured) | PASS |
| 4 | prompt TEXT forwarded to the core (enables proactive recall) | PASS |
| 5 | `pre_tool` gate was consulted | PASS |
| 6 | `pre_tool` invoked with `--block-exit 2` | PASS |
| 7 | tool name translated to the Claude shape (`write` → `Write`) | PASS |
| 8 | `file_path` forwarded from Pi's `path` | PASS |
| 9 | **write BLOCKED** — target absent after a real tool call | PASS |
| 10 | every call carried `--runtime pi` | PASS |
| 11 | **write ALLOWED** when the gate exits 0 — target present | PASS |
| 12 | `post_tool` fired after the call | PASS |
| 13 | `stop` core fired when the agent settled | PASS |
| 14 | `session_start` fired (CLI) | PASS |
| 15 | `session_end` fired on shutdown (CLI) | PASS |
| 16 | lifecycle calls carried `--runtime pi` | PASS |

Assertion 4 is what earns Pi a real `proactive_recall` cell in the matrix rather
than opencode's `none (no prompt text)`: `before_agent_start` carries
`event.prompt`, and the bridge forwards it, so `promptrecall` has something to
embed. Measured, not assumed — the recorded payload was
`{"prompt":"Write the target file.","cwd":"…"}`.

Observed core order:

```
blocked run:   prompt -> pre_tool -> stop
allowed run:   prompt -> pre_tool -> post_tool -> stop
lifecycle:     session_start -> session_end
```

Assertions 9 and 11 are a matched pair on purpose. "The file is absent" proves
nothing on its own — it is also what a crashed harness looks like. Assertion 8
is therefore gated on `pre_tool` having actually run, and assertion 11 shows the
same setup *does* produce the file when policy allows. Together they establish
that the block is causal.

## What Pi can do that no other non-Claude runtime can

Both of these were measured, not inferred from documentation:

- **The briefing enters the context window.** Pi's `before_agent_start` may
  return `{ systemPrompt }`. Assertion 3 captures `context.systemPrompt` inside
  the provider and finds the marker in it. The opencode bridge cannot do this —
  `adapters/opencode.py` states plainly that "opencode has no systemMessage
  channel, so the briefing/inbox text is LOGGED, not injected into model
  context".
- **The pre-edit gate hard-blocks.** Pi's `tool_call` takes
  `{ block: true, reason, terminate }` as a first-class return. Kiro's pre-edit
  hook "fires but is advisory rather than a hard block" (`docs.html:1462`);
  opencode has to throw. Assertion 9 shows the file is never written.

## Against a REAL Keep

The 16 assertions above use a stub dispatcher, which proves the bridge↔dispatcher
contract but not the Keep round-trip. Both were then run against the live Keep
(`cortex/bridge/relay/sentinel` all OK, client 1.5.5, cortex v1.3.3) on
2026-08-29:

1. **`session_start` → real pre-flight briefing.** The CLI path fired
   `session_start`, the real dispatcher fetched from the Keep, and 2294
   characters of genuine briefing came back — agent identity, profile,
   environment, bulletins, resumable sessions.
2. **`before_agent_start` → real proactive recall, verbatim in the model's
   system prompt.** The SDK path with the real interpreter, prompted with *"How
   did we build the Pi runtime adapter for the Firekeep client kit?"*, put this
   in `context.systemPrompt` (11 421 chars total):

   ```
   [firekeep recall] team memory that may be relevant (verify before relying on it):
   - Built the Pi (pi.dev) runtime for the Firekeep client kit end to end: … (score 0.78)
   - Task: Build the claude-desktop runtime adapter for the Firekeep client kit: … (score 0.77)
   ```

   Real Keep, real retrieval, real injection — asserted as an exact substring of
   what the model received, not as an intention of the bridge.

## Published-package verification

`firekeep-pi@0.1.0` went to npm on 2026-08-29 (registry 200; `npm view
firekeep-pi version` → `0.1.0`). The PUBLISHED artifact — not the working tree —
was then installed into a clean directory and driven:

- tarball contains exactly `LICENSE`, `README.md`, `package.json`, `src/index.ts`
  (7.7 kB), and the BUSL-1.1 text really is in it
- `pi.extensions` resolves to `./src/index.ts`
- Pi loaded it and fired `session_start` and `session_end`, both carrying
  `--runtime pi`
- `pi list` resolves the `packages: ["firekeep-pi"]` entry that
  `firekeep install --runtime pi` writes, closing the loop from adapter to
  registry to running extension

## Empirical surprises worth recording

Five things cost real time and are not in Pi's documentation:

1. **The faux provider needs a provider ENTRY, not just an API registration.**
   `registerFauxProvider` registers the stream implementation but no provider,
   so Pi's credential check finds nothing and the turn dies with *"No API key
   found for firekeep-faux"*. `pi.registerProvider(name, { api, apiKey: "$VAR" })`
   from an extension factory supplies the missing half.
2. **pi-ai is module state, and npm may install two copies.** With a nested
   `pi-coding-agent/node_modules/@earendil-works/pi-ai`, registering the faux
   stream from the top-level copy puts it in a registry the agent never reads:
   *"No API provider registered for api: firekeep-faux-api"*. The harness
   resolves pi-ai **through** pi-coding-agent so both share one instance. Any
   future extension touching pi-ai's registries must do the same.
3. **The CLI cannot host this test.** `--provider` is validated during argument
   parsing, before extensions load, so an extension-registered provider can never
   be named there; and `--api-key` refuses to apply without `--model`. Hence the
   SDK path for runs 1–2.
4. **The SDK does not emit `session_start` / `session_shutdown`.** They are app
   lifecycle events. Run 3 drives the real CLI to cover them — which is also why
   the suite is not SDK-only.
5. **The globally-installed CLI is a BUNDLE, and an extension cannot reach into
   it.** `dist/bundle/cli.js` has pi-ai compiled in, so a faux provider
   registered from a jiti-loaded extension lands in a different instance and the
   turn dies with "No API provider registered for api: …" no matter which
   on-disk copy you resolve. This is why the one-process end-to-end run
   (real briefing AND a faux model turn) is not achievable, and why the
   real-Keep evidence above is split across the CLI and SDK paths.

## Not yet validated

These are **not** claimed anywhere and must not appear in the capability matrix
until a run asserts them:

- `session_before_compact` → `precompact`. The bridge wires it, but no scenario
  forces a compaction, so the cell is unproven.
- Mid-run steering via `pi.sendMessage({ deliverAs: "steer" })`. Not wired.
- Behaviour against a **real** Keep. Every run above used the stub dispatcher, so
  what is proven is the bridge↔dispatcher contract, not Keep round-trips.
- Non-Windows platforms. Run on Windows 11 / Node 24.11.1 only.
