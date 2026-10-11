# Firekeep Studio 0.5 — "Window into the Keep"

Date: 2026-10-11 · Status: draft for review · Supersedes the UI sections of
`2026-08-24-firekeep-studio-design.md` (the runtime-neutral core, Mission truth contract and
security boundaries in that spec still stand unless changed below).

## 1. Why

Studio 0.4.1 is a capable control panel. It is not a workspace a new teammate understands in
two minutes, and it barely shows the thing only Firekeep has: the Keep.

Evidence (2026-10-11):

- About 126 Studio installers were downloaded between 2026-09-20 and 2026-10-11 (site counter,
  browser user agent). By contrast, client install scripts were fetched only 25 times,
  mostly by internal release testing.
- The founder's own machine has two Studio sessions, the last on 2026-08-26. The builder
  does not use it day to day.
- The first screen asks for a runtime and a workspace before you can type, gives a zeros-only
  token card the best spot in the rail, and puts Missions behind eight slash commands.
- Studio reaches the Keep only by shelling out to `firekeep status|doctor|update`. It never
  shows a memory, a skill, or a teammate.

## 2. Decisions (agreed in brainstorming)

| Decision | Choice |
|---|---|
| Studio's job | **Window into the Keep**: the place you *see* Firekeep working, with any agent driving. |
| Primary user | **A new teammate**: value within two minutes, no slash-command knowledge required. |
| Layout | **A**: sessions rail · conversation · collapsible Keep panel. A new, empty session shows Keep highlights instead of a slogan. |
| Cut | Voice in/out, Grok runtime, `/compare`, `/consensus`, the Agents tiled grid. |
| Add | Keep panel, per-turn Keep receipts, Changes + undo, "Needs you" inbox, @files + images. |
| How Studio reads the Keep | **MCP client to the kit's own gateway** (`firekeep gateway`). No direct REST, no new server endpoints. |
| Receipt source | The Keep's **replay trace** for the Studio session. Tool calls in the runtime stream are only a fallback. |

## 3. Delivery: five pieces, two in this spec

| # | Piece | Spec |
|---|---|---|
| 1 | Layout, first run, cuts | **This spec, §4–§6** |
| 2 | Keep link: gateway client, receipts, Keep panel | **This spec, §7–§8** |
| 3 | Changes + undo (git checkpoint per turn) | Own spec, after 2 |
| 4 | "Needs you" inbox + OS notifications | Own spec, after 2 |
| 5 | Composer @files + images | Own spec, after 2 |

Pieces 1 and 2 ship together as **Studio 0.5.0**. Piece 1 alone would remove features
without adding the reason to use Studio, so it must not be released on its own. Pieces 3–5
each ship as a 0.5.x release. This spec reserves their UI slots (§4) so they slot in without
another layout change.

## 4. Information architecture (Layout A)

```text
┌ top bar ───────────────────────────────────────────────────────────────┐
│ ◉ Firekeep Studio · [repo ▾ Firekeep]            [Needs you · n]* [⚙] │
├ rail ────────┬ conversation ─────────────────────────┬ Keep panel ─────┤
│ + New        │ user turn                             │ [Memory|Skills| │
│ ● Session A  │ answer                                │  Team]          │
│   Session B  │ ┄ Keep · recalled 3 · learned 1 👍👎 ┄ │ search…         │
│   Session C  │ ┄ 2 files changed · diff · undo ┄*    │ results         │
│              │                                       │ Online now      │
│              │ ┌ composer ──────────────────────────┐│                 │
│              │ │ Ask Claude…  (@file, paste image)* ││  (⌘\ collapses) │
│              │ │ [Claude·Fable·high] [Done when…]   ││                 │
│              │ └────────────────────────────────────┘│                 │
│              │ 12k fresh · 40k cached                │                 │
└──────────────┴───────────────────────────────────────┴─────────────────┘
  * = slot reserved now, filled by pieces 3–5
```

- **Rail:** sessions only (name, colour, relative time) and **+ New**. The usage card,
  workspace card and kit-status block are removed from it. A kit problem shows as a single
  warning row at the bottom of the rail, which opens Settings → Keep connection.
- **Top bar:** the brand, a **repo picker** (the current workspace, a list of recent
  workspaces, and "Open folder…"), the **Needs you** pill (hidden until piece 4 ships),
  and Settings.
- **Conversation:** answers, the work log (unchanged from 0.3.2), Mermaid diagrams
  (unchanged), and a **Keep receipt** under each completed answer (§8). The
  **Second opinion** action sits in each answer's hover actions.
- **Composer chips:**
  - **Agent chip** (`Claude · Fable · high`) opens a popover for runtime, model, effort and
    permission mode. It replaces the primary picker and the Turn-controls inspector.
  - **Done when…** chip (§5.3).
  - **Usage line** under the composer: a single line that expands into the existing usage
    breakdown and budget control.
- **Keep panel:** §7. Collapsible with ⌘\\ (the shortcut previously used by the Agents
  grid). Its open or closed state is persisted. Below 1100 px window width it becomes a
  drawer, reusing the 0.3.7 responsive inspector behaviour.
- **⌘K** opens the command palette: a fuzzy-searchable list of every slash command plus
  "Search the Keep…". Slash commands remain as the power-user path. **Rule:** no
  first-run or core task may *require* a slash command.
- **Settings (⚙)** is a sheet with these sections:
  - Agents & accounts: the former Runtime Center, including connect and disconnect.
  - Keep connection: kit status, doctor, personal mode, update, and the dashboard link.
  - Reviews: the after-turn auto-review mode and the default reviewer.
  - Appearance: theme.
  - Studio updates.

## 5. Piece 1 — first run, simplified flows, cuts

### 5.1 First run

1. On launch, Studio runs the existing runtime probes, which do not start an agent turn,
   and probes the Keep (§7.1) in parallel.
2. The empty session shows the **Start card** in place of the 0.3.4 hero:
   - **Agent row.**
     - If exactly one runtime is ready, it is preselected and shown as "Using Claude ·
       change".
     - If several are ready, they appear as one-click cards. The runtime-neutral contract
       still holds: the user explicitly picks, but in one click rather than through a
       dropdown.
     - If none is ready, each card shows its connect action (the existing `/connect` flows).
   - **Workspace row:** recent workspaces as chips, plus "Open folder…".
     - Typing without choosing one keeps the 0.4 behaviour (start in the home folder).
     - The chip makes the choice visible rather than required.
   - **Keep highlights** (§7.4) render below once the Keep probe answers. If the Keep is
     unavailable, the panel shows its "Connect to your Keep" card instead (§7.5).
3. The composer is focused and usable immediately. The only hard requirement before Send
   is a ready runtime.

### 5.2 Second opinion (replaces reviewer UI)

- Each completed answer gets a **Second opinion** hover action.
- It opens a one-line chooser: an agent and an optional focus (e.g. "security").
- It runs the existing fresh, read-only review path (`/review`) against that answer.
- The result renders as a subordinate card attached to the answer, never as a new primary
  turn.
- Automatic after-turn review moves to Settings → Reviews. The setting (`reviewerMode`,
  `reviewerRuntimeIds`) and the existing guarantees are unchanged:
  - reviews are fresh and read-only;
  - a review is never itself reviewed;
  - review prose is never parsed as a grade.

### 5.3 "Done when…" (replaces the Missions UI)

- Clicking the chip runs **test-command detection** in the workspace and proposes one
  command:
  - `package.json` → `npm test`;
  - `pyproject.toml` / `pytest.ini` / `tests/` → `pytest`;
  - `Cargo.toml` → `cargo test`;
  - `go.mod` → `go test ./...`;
  - otherwise empty.
- The user confirms or edits the command. More checks can be added from the same popover.
  The existing name and timeout fields live under "More".
- With the chip set, **Send creates and runs a mission** from the composer text.
  - The writer is the selected primary.
  - Configured reviewers are optional.
  - The repair limit and token guard use their existing defaults.
- The 0.2 Mission state machine, the native confirmation dialog showing the exact commands,
  the outcome receipt, and the **human acceptance rule are unchanged**.
- Mission progress renders inline as a stepper on the turn: Write → Check → Repair n/N →
  Review → **Accept / Partial / Failure**. The Accept buttons appear on the turn itself.
- `/mission …` commands keep working for power users.
- The mission panel in the inspector is removed.

### 5.4 Cuts and what goes with them

| Cut | Removed with it |
|---|---|
| Voice | `voice-input.ts`, the microphone button, the `/voice` command, speech synthesis, the `voiceEnabled` setting (ignored on load, dropped on next save), and the related README security bullets |
| Grok | `grok-runtime.ts`, its registry entry, the `safeStorage` API-key dialog and storage, and the Grok rows in docs. **Studio then stores no secrets.** |
| `/compare`, `/consensus` | Their commands, service paths and tests |
| Agents grid | Pane state, Use/maximize controls, the 3-pane layout code and its packaged-smoke geometry assertions |
| Hero, inspector marketing lines, rail usage card | Their renderer components |

Users of a cut feature see a single line, once, in the 0.5.0 release notes and in the
session where it would have applied (e.g. `/voice` returns "Voice was removed in Studio
0.5"). There is no silent failure.

Saved settings that name a cut runtime are handled on load:
- a `primaryRuntimeId` of `grok` falls back to "no primary" and shows the Start card;
- `grok` entries are dropped from `reviewerRuntimeIds`;
- existing session JSONL files that contain Grok turns still render read-only.

## 6. Piece 1 — component boundaries

`App.tsx` (978 lines) and `studio-service.ts` (1408 lines) are already the largest files. The
rework splits the renderer along the new layout instead of growing `App.tsx`:

| Unit | Responsibility | Depends on |
|---|---|---|
| `renderer/src/layout/StudioShell.tsx` | The top bar / rail / conversation / Keep-panel grid, and collapse state | settings IPC |
| `renderer/src/rail/SessionRail.tsx` | Session list, rename and colour (moved from App), + New | session IPC |
| `renderer/src/conversation/Conversation.tsx` | Timeline rendering, work log, receipts, second-opinion cards, mission stepper | `timeline.ts`, runtime events |
| `renderer/src/composer/Composer.tsx` + `AgentChip.tsx` + `DoneWhenChip.tsx` | Input, chips, usage line | runtime/mission IPC |
| `renderer/src/start/StartCard.tsx` | First-run / empty-session card | probes, Keep highlights |
| `renderer/src/settings/SettingsSheet.tsx` | The Settings sections from §4 | existing IPC |
| `renderer/src/palette/CommandPalette.tsx` | ⌘K fuzzy list over `slash-commands.ts` metadata | `slash-commands.ts` |
| `core/test-command-detect.ts` | Pure detection rules for §5.3 | filesystem listing passed in (testable) |

The cuts remove code from `studio-service.ts`; no new logic is added there for piece 1.

## 7. Piece 2 — the Keep link

### 7.1 Gateway client (main process)

- `main/keep/keep-gateway.ts` spawns the kit's `firekeep gateway` as a long-lived **MCP
  stdio client**. It resolves the executable the same way `firekeep-client.ts` does today
  (`~/.firekeep/current/...`, then PATH).
- It starts with `FIREKEEP_TOOLSET=studio`. If an older kit exits with "unknown
  FIREKEEP_TOOLSET", it retries once with the same names in `FIREKEEP_TOOLS_ALLOW`.
- **New kit preset `studio`** (in `client/firekeep_client/gateway.py` `TOOLSET_PRESETS`):
  - reads: `memory_recall`, `skill_list`, `skill_recall`, `relay_who_is_online`,
    `relay_read`, `ctx_list_sessions`, `replay_timeline`, `corpus_sources`;
  - writes: `memory_feedback`, `ctx_start_session`, `ctx_complete_session`.
  - The kit owns and tests this list. Studio never calls a tool outside it.
- The renderer gets **typed IPC only**:
  - `keep:status`
  - `keep:search {query, kind: memory|skill}`
  - `keep:team`
  - `keep:highlights`
  - `keep:receipt {turnId}`
  - `keep:feedback {memoryIds, useful, comment?}`

  There is no generic "call tool" IPC. This preserves the 0.1 rule that the renderer has no
  generic process or network API.
- **Lifecycle:**
  - One gateway per Studio process, started lazily at first launch probe.
  - Restarted with backoff (1 s, 5 s, 30 s, then manual) if it exits.
  - Killed as a process tree on quit, reusing `process.ts`.
  - Calls time out after 10 s. A timed-out call never blocks a runtime turn.

### 7.2 Session correlation (Studio session ⇄ Keep session)

Receipts need the Keep session id that the agent's memory calls were attributed to.

- When a Studio session sends its first turn, Studio calls `ctx_start_session(goal=<session
  name or first prompt, truncated>)` over its gateway and stores the returned id in the
  session index as `keepSessionId`.
- Every runtime child for that Studio session is launched with
  `FIREKEEP_SESSION_ID=<keepSessionId>`. The variable is already on the kit's `childenv.py`
  pass-through allowlist, but nothing consumes it yet.
- **Kit change (required):** when `FIREKEEP_SESSION_ID` is set,
  - the session-start hook resumes or adopts that session instead of starting a new one;
  - the gateway shim's `X-Session-Id` tap uses it in preference to the per-agent stash.

  This applies to all hook-capable runtimes (Claude, Kiro) and to hookless Codex, whose MCP
  calls go through the shim.
- Deleting or archiving a Studio session calls `ctx_complete_session`. Plain closing does
  not, because the Studio session may be resumed.

### 7.3 Keep receipts (per turn)

- When a turn completes, Studio calls `keep:receipt`.
  - The main process queries `replay_timeline(keepSessionId, event_type=memory_read|
    memory_write)` for events between the turn's start and end timestamps.
  - It also collects skill ids surfaced in those events.
- The receipt renders under the answer: **"Keep · recalled 3 · learned 1 · skill
  recall-bench"**.
- Expanding it lists each memory with its summary, score and a "claim" trust marker for
  non-self-authored items, using the kit's existing trust semantics.
- 👍/👎 on the receipt, or per item, calls `memory_feedback` for the ids acted on.
  - The UI copy says "Was this knowledge right?", not "rate this answer". That matches the
    kit rule that feedback is about knowledge, not the turn.
  - Feedback is one-shot per item.
- **Fallback.** If the kit predates §7.2, or replay is unreachable, Studio builds the
  receipt from `mcp__firekeep__*` tool calls visible in the runtime stream and marks it
  **"partial — hook-injected recall not shown"**. Studio never shows a confident zero it did
  not measure.
- If nothing was read or written, the receipt shows "Keep · nothing recalled" at reduced
  weight. Hiding it would make the Keep's silence invisible.

### 7.4 Keep panel

| Tab | Default content | Search |
|---|---|---|
| **Memory** | The memories surfaced in this session's receipts (most recent first) | `memory_recall(task=query, format=raw)`, debounced 300 ms |
| **Skills** | `skill_list` (active), with trial-ladder skills marked `TRIAL` | `skill_recall(task=query)` |
| **Team** | `relay_who_is_online` (agent, goal, idle/active) and the latest `relay_read` bulletin posts | none |

- Clicking a memory or skill opens a read-only detail popover with an **Insert into
  composer** action, which quotes it into the prompt.
- Studio does not edit memories or skills in this release.
- **Keep highlights** (empty session, §5.1): who is online now, three recent bulletin posts,
  and "Pick up where you left off", which lists paused Keep sessions from
  `ctx_list_sessions(status=paused)` and opens or creates the matching Studio session.

### 7.5 States

| State | Panel shows |
|---|---|
| Kit not installed | "Connect Studio to your team's Keep": a sentence on what the Keep is, the firekeep.ai install command (copy button), and "I've installed it — retry" |
| Kit installed, server unreachable or unauthorised | The first line of `firekeep doctor`, plus **Run doctor** (opens Settings → Keep connection) |
| Personal mode on | A banner: "Personal mode — nothing from this session is shared". Receipts still show reads. |
| Connected | Tabs |

Chat always works without the Keep. Only the panel and receipts degrade.

## 8. Error handling summary

- **Gateway spawn or exit:** the panel shows the §7.5 state, with backoff as described in
  §7.1. No modal dialogs and no blocked turns.
- **Receipt query failure:** the receipt shows "Keep receipt unavailable · retry". It never
  shows zero.
- **Feedback failure:** an inline retry on that item. Failed feedback is never queued for
  later sending.
- **Test-command detection finds nothing:** the chip opens with an empty field and the
  placeholder "e.g. npm test". Send stays disabled until a check is confirmed (the existing
  Mission prerequisite rule from 0.4.1).
- **Cut-feature settings:** migrated on load as described in §5.4. A cut runtime id never
  crashes the picker.

## 9. Testing

- **Unit (vitest):**
  - `test-command-detect` rules;
  - receipt builder: replay events → receipt, the fallback path, and the "partial" label;
  - Keep IPC validation: unknown tool names rejected, argument shapes enforced;
  - settings migration for the cut features;
  - command palette ranking.
- **Gateway client:** tested against a fake MCP stdio server fixture covering spawn,
  timeout, restart with backoff, preset-to-allowlist fallback, and process-tree kill on
  quit.
- **Kit (pytest, `client/`):**
  - the `studio` preset contents, and that an unknown preset still fails closed;
  - `FIREKEEP_SESSION_ID` adoption in the session-start hook and in the shim's
    `X-Session-Id` tap.
- **Packaged smoke (`smoke:package`):** replace the grid geometry and inspector assertions
  with:
  - the three-column shell;
  - Keep panel collapse with ⌘\\;
  - the Start card;
  - ⌘K palette open;
  - both themes.

  The smoke runs with no kit present, so it asserts the "Kit not installed" panel state.
- **Live smoke (`smoke:runtimes`):** adds a Keep probe (gateway handshake and one
  `relay_who_is_online` call) when a kit is configured.
- **Removed tests:** voice, Grok, compare/consensus and grid tests are deleted along with
  their code, not skipped.

## 10. Documentation and consistency

Update together, per the repo's change-consistency rule:
- `studio/README.md`: a 0.5 section, runtime table without Grok, the removed security
  bullets, and new Keep-link bullets;
- `client/firekeep_client/gateway.py` and `docs/guides/client-kit.md` "Gateway toolsets":
  the `studio` preset;
- the kit's session-start / shim docs: `FIREKEEP_SESSION_ID`;
- root `CLAUDE.md`: the Studio row (drop Grok, mention the Keep link).

The kit change ships in a client release **before** Studio 0.5.0. Studio must still work,
with "partial" receipts, against an older kit.

## 11. Out of scope

- Concurrent agents and git worktrees. Studio keeps the one-writer-per-workspace rule.
- Editing or deleting memories and skills from Studio.
- Pieces 3–5 beyond their reserved slots.
- macOS notarization and in-app updating (unchanged from 0.4).
- Any new server endpoint.
