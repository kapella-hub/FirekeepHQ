# FirekeepScope Contract v1

Versioned JSON contract for FirekeepScope scope-clarification sessions. `"v": 1`
appears on both Session and Screen objects (they cross the companion/Relay/
dashboard boundary independently — see SP2 design spec §4).

## Session

    {
      "v": 1,
      "scope_id": "sc_a1b2c3",
      "agent_id": "agent-alex_pc-5a60",
      "bridge_session_id": "4848c027-e34",
      "project": "firekeep",
      "goal": "Design auth for the client kit",
      "origin": "cli",
      "status": "active",
      "created_at": 1783606090.68
    }

- `origin`: `"cli"` (has a local companion; companion owns Bridge persistence)
  or `"mcp"` (no local companion; Relay owns Bridge persistence — see Bridge
  route below).
- `status`: `"active" | "completed" | "abandoned"`.

## Screen

    {
      "v": 1,
      "screen_id": "sc_a1b2c3-3",
      "kind": "questions",
      "mode": "gating",
      "title": "Auth flow",
      "context_md": "Optional markdown rendered above the questions",
      "questions": [{
        "id": "auth", "text": "Which auth flow?", "type": "single", "required": true,
        "options": [
          { "id": "a", "label": "Per-user keys", "detail": "…", "recommended": true },
          { "id": "b", "label": "Shared key + header", "detail": "…" }
        ],
        "allow_other": true,
        "embed": { "type": "mermaid", "src": "flowchart LR; A-->B" }
      }]
    }

- `screen_id` is globally unique: `<scope_id>-<n>`, monotonic per session,
  minted by whichever side creates the screen (Relay mints when the caller
  omits it).
- `kind`: `"questions"` (implemented here) `| "spec_review"` (Phase 2, not
  yet implemented — screens of this kind render as "upgrade needed" until
  then).
- `mode`: `"gating" | "async"` — set by the verb that created the screen
  (`ask`/`scope_ask` → gating, `post`/`scope_post` → async), not
  independently settable by the caller.
- `type`: `"single" | "multi" | "text"`. A question may set
  `"required": false`; a skipped optional question is an explicit `null`
  entry in `answers`, never a missing key.
- `embed.type`: `"mermaid" | "html" | "image"`. Any renderer of embeds MUST
  never grant `allow-same-origin` to the rendering iframe, on any surface,
  under any configuration, and MUST inject a `default-src 'none'`
  Content-Security-Policy — the CSP is what blocks subresource network
  loads, not the `sandbox` attribute alone. (Not implemented by the
  dashboard tab in this plan — see "Scope note" above.)

## Answer

    {
      "screen_id": "sc_a1b2c3-3",
      "answers": {
        "auth": {
          "choice": "a", "choices": null, "text": null,
          "other_text": null, "note": "rotate quarterly"
        }
      },
      "source": "local",
      "answered_at": 1783606201.23
    }

- `source`: `"local" | "dashboard"`.
- `choice` (single) / `choices` (array, multi) / `text` (free-text type).
- "Other": when offered (`allow_other: true`) and chosen, `choice` (or an
  entry in `choices`) is the reserved sentinel `"__other__"`; the free text
  goes in `other_text`. `note` is a separate, always-permitted annotation on
  top of any answer regardless of `allow_other` — `{choice:"a",
  note:"rotate quarterly"}` is an annotated endorsement of option A;
  `{choice:"__other__", other_text:"my own idea"}` is a rejection of all
  listed options.

## First-answer-wins arbiter

`SET NX` on `nr:scope:answer:{scope_id}:{screen_id}`. The winning write
resolves the screen; the losing surface's write fails (`resolved: false`
in the REST response) because the screen is no longer pending.

## Reserved namespace

The `_` id prefix is reserved: no `questions` screen may define a question
`id` starting with `_` (Phase 2's `spec_review` answer uses the magic key
`_spec`).
