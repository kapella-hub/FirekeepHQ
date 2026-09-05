# Firekeep Hands — a screen-aware operator for the whole computer, without the device

**Date:** 2026-09-05 · **Status:** approved direction (founder decision board 2026-09-05) · **Scope:** PR1 as phased in §9

## 0. The ask, in one paragraph

Violoop (violoop.ai, IFA 2026) is a palm-sized box that plugs between a PC and its monitor: it reads the screen over HDMI, plans multi-step tasks across whatever apps are open, acts through a USB keyboard/mouse, and executes protected steps only when a physical button wired to a separate security chip is pressed. It routes deterministic paths (API, shortcut, accessibility handle) before pixel clicks, learns workflows by watching, keeps raw screen data local, and offers three modes: *Instant* (hints beside the cursor), *Long-running* (cross-app tasks) and *Away* (finish while the screen is locked, approve from the phone). The founder asked for "something like Violoop, but without a device", named it **Firekeep Hands**, and settled four decisions on a board:

| Decision | Choice |
|---|---|
| The brain that drives tasks | **The installed runtimes, through MCP** (Claude Code, Codex, Kiro, Studio call `hands_*` tools on the user's existing subscriptions). No new Firekeep agent. |
| The approval "button" | **A separate approval-broker process** that accepts only real, non-injected input (or a phone tap through the relay) and mints one-use permits. |
| Platforms | **Windows and macOS from day one.** |
| First surface | **Kit-mounted MCP server** (`firekeep-hands` wheel behind the gateway); a Studio "Operate" mode comes second. |

This fits the standing product decision (2026-08-14/16): Firekeep does not become another horizontal assistant. Hands is a **brokered capability** — the third leg of the dex / host-adapter / capability split — and its autonomy is **earned**: observed traces become Living Procedures, and the skill ladder decides what an agent may trust next time.

## 1. What Hands is, and is not

Hands is a local MCP server plus a local approval broker. Any connected runtime can:

1. **Perceive** — the active window and app, a compact accessibility tree (UI Automation on Windows, the Accessibility API on macOS), text from controls, the clipboard, and, on request, a downscaled screenshot or a zoomed region.
2. **Act** — focus or launch an app, invoke a control by reference, type, press chords, scroll, select menu paths, drive the browser (tabs, navigate, read, fill, click) through Chrome DevTools, and read or set the clipboard.
3. **Route deterministically first** — app scripting → keyboard shortcut → accessibility action → pixel click at a control's own rectangle. Model-guessed pixel coordinates are not accepted in PR1 (that is the screenshot-driven fallback, §9 PR2).
4. **Ask before it matters** — a protected step (§5) needs a one-use permit from the broker; the broker mints permits only on a real keystroke chord or a phone tap.
5. **Leave evidence** — every step is a replay event with the control path, the action, and before/after screenshot hashes; a task is a lease on the relay so two agents never share one mouse.

Hands is not: a chat product, a model, a screenshot streamer, or a way around the operating system's own permission prompts. It cannot act on a locked screen (§7).

## 2. Modes, mapped from Violoop

| Violoop | Hands PR1 | Hands later |
|---|---|---|
| Long-running cross-app task | **Task mode**: `hands_task_start` → observe/act loop → `hands_task_end`; permits at protected steps | — |
| Learns by observation | **Observe mode (lite)**: every step is a replay trace; Living Procedures reads traces of kind `ui-step` | Ladder promotes repeated successful routines; suggestions become one-tap |
| Away mode | **Phone approvals** through the relay (dashboard on the phone) while the screen stays unlocked | A dedicated locked session or VM; see §7 |
| Instant mode (Tab to accept a hint beside the cursor) | — | Studio Operate mode: hints beside the cursor, task timeline, approvals UI |
| Local 8B model for routine work | — (the runtime is the brain) | Optional local model for Instant hints |

## 3. Architecture

```
 runtimes (Claude Code · Codex · Kiro · Studio)
        │  MCP (stdio)
        ▼
 firekeep gateway ──mounts──▶ firekeep-hands  (MCP stdio server, console script)
                                  │  perceive / act / browser / evidence
                                  ├── win backend   (UI Automation, SendInput, mss, clipboard, launch)
                                  ├── mac backend   (AXUIElement, CGEvent, screencapture, osascript)
                                  ├── browser       (Chrome DevTools over a local websocket)
                                  └── permits ◀──── firekeep-hands-broker (separate process)
                                                      │ low-level input hook: real keystrokes only
                                                      │ phone tap via relay task
                                                      └ one-use permits, in-memory, fail closed
 Keep: replay events · policy engine (action_before) · relay leases + phone approvals · procedures
```

### 3.1 Packaging and mounting

- New wheel **`firekeep-hands`** (source `hands/` at the repo root, like `symdex/` and `docdex/`), hatchling, `requires-python >= 3.10`, console scripts `firekeep-hands` (the MCP server) and `firekeep-hands-broker`. Dependencies are declared with environment markers: Windows — `uiautomation` (UI Automation over comtypes), `mss` (screenshots), `pillow` (downscale, region zoom); macOS — `pyobjc-framework-Quartz`, `pyobjc-framework-ApplicationServices`, `pyobjc-framework-Cocoa`, `pillow`; both — `mcp`, `websocket-client`. Input injection and the low-level hooks use `ctypes` / Quartz directly. The client-kit spine stays stdlib-only; Hands is a wheel like the dexes.
- **Not bundled by the bootstrap in PR1.** The pyobjc set alone is tens of megabytes; Hands is an opt-in capability with real permissions, so `firekeep hands enable` installs the wheel into the kit venv from PyPI (the release workflow already publishes the dex wheels there), then registers it. `firekeep hands disable` unregisters; `--purge` uninstalls.
- **Registry:** Hands is an entry in `~/.firekeep/dexes.json` under the name `hands`. `DexManifest` gains one optional field, `role: "index" | "capability"` (default `index`), and the CLI/doctor label capabilities as such — the file is, honestly, the client's *capability registry*; the dexes were its first residents. Manifest: `id firekeep.hands · name hands · title Hands · indexes desktop · kind mcp-stdio · role capability · console_script firekeep-hands · import_probe firekeep_hands`. The gateway's existing `kind == "mcp-stdio"` mount path needs no change. The seeding rule never seeds `hands` — a capability that moves the mouse is registered only by an explicit human command.
- **Doctor** gains a `hands` row: registered/available/not installed, broker running or not, OS permissions granted or missing (macOS: Accessibility, Screen Recording, Input Monitoring), and the approval chord.
- **Autostart of the broker:** Windows Task Scheduler (at logon) / macOS LaunchAgent, written by `firekeep hands enable`, removed by `disable`. The MCP server refuses protected actions when the broker is not reachable (fail closed) and says so in `hands_status`.

### 3.2 The MCP tool surface (PR1)

| Tool | Purpose |
|---|---|
| `hands_status` | Platform, backend health, broker reachable, permissions, current task/lease, approval chord. |
| `hands_task_start(goal, apps=None)` | Acquire the machine's `hands` lease on the relay (one operator per machine), open a task id, record the goal. Fails with the holder's identity if another agent holds it. |
| `hands_observe(detail="summary"\|"tree"\|"screenshot", app=None, region=None, max_nodes=200)` | Active app/window, focused control, compact accessibility tree ranked around focus (depth and node caps), or a downscaled screenshot (≤ 1568 px long edge) / zoomed region. Every control carries a stable `ref` for the current observation. |
| `hands_find(query, app=None, role=None)` | Locate controls by name/role/automation id in the active window; returns refs and rectangles. |
| `hands_act(action)` | One action from the union in §3.3, routed deterministically. Returns the routed path taken, a fresh compact observation, and the replay event id. Protected classes require `permit`. |
| `hands_request_permit(step)` | Ask the broker for a permit for a described step; returns `challenge_id` and the human instruction ("press Ctrl+Alt+Enter" or "approve on your phone"). The runtime relays that instruction to the human; nothing in Hands can satisfy it. |
| `hands_browser(op, …)` | `tabs`, `open`, `navigate`, `read` (text/DOM outline), `find`, `click`, `fill`, `eval` (opt-in), `screenshot` — through DevTools on a Hands-managed Chrome/Edge instance (§3.4). |
| `hands_task_end(outcome, note=None)` | Release the lease, close the task, declare the outcome (feeds evals and the ladder). |

Every tool call is exception-tight: Hands never crashes the gateway, and a failing backend degrades to a structured error the runtime can read.

### 3.3 Actions and deterministic routing

`hands_act` accepts one of: `focus_app`, `open_app`, `click {ref|point}`, `double_click`, `right_click`, `invoke {ref}` (Invoke/Toggle/Expand/Select patterns), `set_value {ref, text}`, `type {text, ref=None}`, `key {chord}`, `scroll {ref|point, direction, amount}`, `menu {path}`, `read {ref}`, `clipboard {get|set}`, `wait {ms|until}`, `close_window`.

Routing order, chosen per action and recorded in the event: (1) **app scripting** where a verified adapter exists (AppleScript on macOS for Finder/Mail/Safari/TextEdit; none on Windows in PR1); (2) **keyboard shortcut** when the action maps to one the app advertises (accelerator text on the control); (3) **accessibility action** — the Invoke/Value/Selection patterns; (4) **pixel click** at the centre of the control's own rectangle. A `point` click is accepted only when it lies inside a rectangle returned by the current observation; a bare model coordinate is rejected in PR1.

### 3.4 The browser

Chrome or Edge is driven over the DevTools protocol. Attaching to a browser the user already has open is not possible unless it was started with remote debugging, so `hands_browser open` starts a **Hands-managed instance** with `--remote-debugging-port` on a **dedicated persistent profile** (`~/.firekeep/hands/chrome-profile`), which the user signs into once. Pointing Hands at the real default profile is allowed only when that browser is closed and the user opts in (`firekeep hands config browser.profile=default`), because it exposes every saved session to the agent. Navigation to non-allowlisted domains is a protected class by default (§5).

### 3.5 The approval broker — the button without hardware

Violoop's argument: the model must not be able to press its own approve button. In software the equivalent is a **separate process the model cannot drive**:

- **Real input only.** Windows: a `WH_KEYBOARD_LL` hook; every event synthesized with `SendInput` carries `LLKHF_INJECTED`, and the broker ignores flagged events, so no process — Hands, a shell command, a script — can press the chord. macOS: a `CGEventTap` on the session; events Hands posts carry a Hands-specific `kCGEventSourceUserData` tag and a non-HID source state, and the broker ignores both. (Verified on this PC on 2026-09-05 from the kit venv: a `WH_KEYBOARD_LL` hook saw a `SendInput` F13 press with `flags & LLKHF_INJECTED` set — `sizeof(INPUT)` must be 40 on x64, i.e. the union must include `MOUSEINPUT`, or `SendInput` returns 0; and `CallNextHookEx.argtypes` must be declared or the callback overflows on `lParam`. The broker's tests repeat this — gate 2 in §10.)
- **Phone tap.** The broker posts a relay task (`kind hands_permit`) the dashboard renders with Approve/Deny; the human's tap needs the dashboard credential, which the agent does not hold. The broker polls the task; an approve mints the permit, a deny records `denied_by_human`.
- **Permits** are one-use, bound to a deterministic challenge id over `(machine, agent session, task, step index, action hash)` — the Enforced Runbooks shape (`enforce.challenge_id_for`) — with a 60 s TTL, held in the broker's memory only. `hands_act` presents the permit; the broker validates and burns it. A restarted broker has no permits (fail closed), and Hands with no broker performs no protected action.
- **What the model can still do:** kill the broker (→ nothing protected runs), ask the human to press the chord under false pretences (→ the permit prompt always shows the broker's own description of the step, built from the action, never from model text), or drive an app that itself performs a protected act through an unprotected path (→ protected classes are defined on effects, and the allowlist, not the action verb; see §5). The host OS and the user account are trusted, exactly as Violoop trusts them.

### 3.6 Evidence, leases, procedures

- Each `hands_act` writes a step record — `task_id`, step index, the action, the routed path, the control path (automation-id chain), before/after screenshot SHA-256, the permit id if any, and the outcome — to the local evidence ledger `~/.firekeep/hands/evidence/<task>/steps.jsonl` beside the images (pruned after 14 days). **PR1 has no client-to-Keep replay route**: replay events are emitted server-side today (`cortex/app/main._replay_emit`), and there is no `POST /replay/events`. So in PR1 the Keep sees the task through `action_before` (goal, protected classes expected) and `action_after` (outcome, step count, the ledger's rolling hash, permit ids), and the per-step trail is local and inspectable with `firekeep hands evidence <task>`. PR2 adds the replay route so `replay_timeline` shows every click.
- `hands_task_start` declares the task through `action_before` so the policy engine can `block` a goal outright; `hands_task_end` reconciles it with `action_after`.
- The relay lease `hands:<machine-id>` serialises operators; `hands_status` shows who holds it.
- Living Procedures gains a step kind `ui-step` (app + control path); a task's trace becomes an observed procedure like a command runbook does today. Promotion of a routine to "one-tap" is the ladder's job, not Hands' (PR2).

## 4. Perception budget

Accessibility trees, not screenshots, are the default: they are text, cheap, and precise. `hands_observe(detail="tree")` returns at most `max_nodes` nodes, ranked by distance from the focused control, each `{ref, role, name, value?, rect, patterns, path}`; a `summary` is the window title, focused control and up to 30 salient controls. Screenshots are downscaled to the runtime's image limit, PNG, and never sent unless the runtime asks; region zoom returns the crop at full resolution. The runtime is a cloud model: **unlike Violoop, raw screen data does leave the machine whenever the runtime requests a screenshot** — the docs say so plainly, trees are preferred by design, and screenshots can be disabled per task (`hands_task_start(..., screenshots=False)`).

## 5. Protected classes and the allowlist

A step is protected — needs a permit — when its **effect** falls in any class below, or when it targets an app or domain outside the allowlist. Classes are decided by Hands from the routed action and target, not by model-supplied labels:

| Class | Examples |
|---|---|
| `send` | Send in a mail/chat/composer window, post/submit forms with message semantics |
| `money` | Payment, checkout, transfer, order buttons; card/IBAN fields |
| `destroy` | Delete, empty trash, discard, uninstall, close-without-saving, `rm`-like shell |
| `credential` | Typing into a password/OTP field, sign-in flows, credential dialogs |
| `install` | Installers, browser extensions, system settings changes |
| `boundary` | Any app not on the allowlist; any domain not on the browser allowlist; leaving the workspace folder |

Defaults ship in `~/.firekeep/hands/policy.json`: allowlisted apps = none (every new app is a `boundary` permit the first time, then remembered for 30 days if approved), allowlisted domains = the Keep's own dashboard plus what the user adds. The Keep's policy engine can tighten this per workspace; it cannot loosen `credential` or `money`. A permit prompt always names the class, the app, and the broker's own description of the action.

## 6. Platforms

**Windows 11 (PR1, this PC):** UI Automation via `uiautomation`; input via `SendInput` (ctypes); screenshots via `mss`; clipboard via Win32 (ctypes); launch via `os.startfile` / `subprocess`; broker hook `WH_KEYBOARD_LL`; autostart via Task Scheduler. Windows' own MCP registry (Insider preview: on-device registry, File Explorer and Settings connectors, sandboxed with identity and audit) is a future *structured* path: when it ships, the gateway can both consume app connectors and register Firekeep as a server (PR2+).

**macOS (PR1, MacBook):** AXUIElement via pyobjc for trees and actions (`AXPress`, `AXSetValue`), CGEvent for input, `screencapture` for screenshots, `osascript` for the scripted apps; TCC permissions requested and reported by doctor (Accessibility, Screen Recording, Input Monitoring — the broker needs Input Monitoring for the event tap); autostart via LaunchAgent. Live verification runs on the MacBook before merge (§10).

**Linux:** not in PR1 (AT-SPI later).

## 7. Honest limits

- **Locked screen.** Input injection does not reach the Windows secure desktop or the macOS lock screen; Violoop's HID hardware does. Hands' Away mode is "approve from your phone while the machine stays unlocked". A locked-session design (separate user session, VM, or RDP loopback) is a later, separate spec.
- **Elevated windows.** UIA cannot drive a higher-integrity (UAC-elevated) window from a normal-integrity process; Hands reports `elevated_target` instead of pretending.
- **Screen data leaves the machine** when the runtime asks for screenshots (§4).
- **Prompt injection** through web pages and app content remains real; permits and allowlists bound the damage, they do not remove the risk. Anthropic's own computer-use guidance (VM/container, no credentials, domain allowlist, human confirmation for consequential actions) is the baseline the docs repeat.
- **Two-hop trust.** The broker trusts the OS's injected flag / event source; a kernel-level or hardware injector defeats it. Out of scope, as it is for Violoop.

## 8. Config and CLI

```
firekeep hands enable            # pip-install the wheel into the kit venv, register, install broker autostart, request OS permissions
firekeep hands disable [--purge] # unregister; --purge uninstalls
firekeep hands status            # same rows doctor shows
firekeep hands allow app "Notepad" | domain example.com | --list | --forget
firekeep hands chord ctrl+alt+enter
firekeep hands config browser.profile=dedicated|default  screenshots=on|off
```

Settings live in `~/.firekeep/hands/config.json` (0600); nothing secret is stored — the broker token is per-boot and in memory.

## 9. Phasing

**PR1 — this plan.** The wheel and MCP server; Windows and macOS backends behind one interface; the browser over DevTools; the approval broker with chord + phone permits; registry `role`, `firekeep hands` CLI, doctor row, autostart; replay events, relay lease, `action_before/after` wiring; protected classes + allowlist; one end-to-end demo task on each platform (open the text editor, write a note, save it under the workspace, then attempt a protected step and watch the permit flow); docs (`docs/guides/hands.md`, dexes.md registry note, client-kit.md, CLAUDE.md, README, firekeep.ai docs page); tests.

**PR2.** Studio Operate mode (cursor-side hints, task timeline, approvals UI, phone Away panel); Living Procedures `ui-step` observation and ladder promotion of routines; screenshot-driven fallback through Anthropic's `computer_toolset_20260801` for apps without accessibility trees; Windows MCP registry integration; a locked-session design.

## 10. Verification gates for PR1

1. Unit: routing order per action (fakes for both backends); permit one-use and TTL; challenge id determinism; fail-closed when the broker is absent; protected-class classification from effects; allowlist memory; tree ranking and caps; screenshot downscale math.
2. Broker: injected `SendInput` chord rejected, real chord accepted (Windows, automated by injecting and asserting rejection; the accept path is a manual step recorded in the report); phone approve/deny through a fake relay.
3. Live smoke, Windows (this PC): the demo task end to end through Claude Code with the gateway mounting Hands; replay shows the steps; the permit prompt appears for the protected step; a denied permit stops the task.
4. Live smoke, macOS (MacBook): the same task in TextEdit; TCC prompts documented.
5. Repo gates: `client/tests/test_import_boundary.py` (spine stays stdlib-only), `tests/test_forbidden_tokens.py`, `tests/test_requirements_lock.py` (wheels stay unlocked), ruff, full client suite.

## 11. Open questions (decide during the plan, not blockers)

- Whether `role: capability` entries should appear in `firekeep dex list` at all, or only under `firekeep hands`. Leaning: both, labelled.
- Whether to ship a minimal Windows app-scripting adapter (PowerShell COM for Office) in PR1 or leave scripting macOS-only. Leaning: macOS-only in PR1.
- The default approval chord. Leaning: `Ctrl+Alt+Enter` on Windows, `⌃⌥↩` on macOS; configurable.

## 12. As built — amendments from the PR1 execution (2026-09-05)

The plan (`docs/superpowers/plans/2026-09-05-firekeep-hands-pr1.md`) and its reviews changed the design in the places below. Where this section and an earlier section disagree, this section is what shipped; `docs/guides/hands.md` is the user-facing statement of the same facts.

- **Open questions (§11) resolved.** Capabilities appear in `firekeep dex list` labelled `operates <domain>`; no app-scripting adapter shipped in PR1 (routing order is accessibility → shortcut → pixel; `open_url` goes to the browser, `open_app`/`focus_app` to the OS); the default chords are `ctrl+alt+y` (approve) and `ctrl+alt+n` (deny), both configurable via `firekeep hands chord`.
- **Tool surface (§3.2).** Eight tools: `hands_status`, `hands_task_start`, `hands_observe`, `hands_find`, `hands_act`, `hands_request_permit`, `hands_browser`, `hands_task_end`. Everything except `hands_status` requires an open task — an observation is a copy of the human's screen and belongs inside a ledgered, leased task. `hands_find` merges its results into the current observation. A protected `hands_act` returns `needs_permit` (challenge, human-readable title, classes); the runtime calls `hands_request_permit` and repeats the same action with `permit=<challenge>`; the server recomputes the challenge from the action and refuses a permit minted for anything else. Refs are stale after every act.
- **Action union (§3.3).** `invoke`, `set_value`, `click`, `type`, `key`, `scroll`, `focus_app`, `open_app`, `open_url`, `clipboard_set`, `wait`; any `x`/`y`/`point`/`coordinates` key is refused; field types are validated; `type` is capped at 500 characters and the Windows backend re-checks the elevation guard every 100 typed characters.
- **Classification (§5).** Six classes as designed, plus: browser `click`/`fill` are classified like native controls from the DOM probe's description (a password input is a credential field; the probe never reports its contents); `type` is classified against the control last targeted by click/invoke/set_value; the ledger redacts typed or set text on credential steps. Nothing writes a remembered approval in PR1 — `firekeep hands allow` writes apps and domains only; auto-remember is off.
- **`boundary` is the catch-all (found by the whole-branch review).** Acting — a click, a keystroke, a value — in any app the task did not declare in `apps` and the allowlist does not cover is a `boundary` step, not only switching to it; after a consumed `boundary` permit, exactly the app(s) the permit title named are open for the rest of that task (task-scoped, never persisted). Hosts are kept apart from apps: a navigation is cleared by the domain allowlist or a task-scoped host widening, never by an `apps` entry; the reserved declaration `"browser"` satisfies browser steps only. A blank app or URL in an action is refused outright. The permit Hands spends must describe the step Hands is running — a permit pre-registered under the same challenge with a different title or classes is refused as tampered, not consumed.
- **Phone approvals (§3.5) are opt-in and off by default.** Relay records no principal on task updates, so any holder of the workspace API key — the driving agent included — could complete a `hands_permit:` task. `firekeep hands config set phone_approvals true` turns it on; `/health` reports phone `off` | `offline` | `active`; PR2 stamps the verified principal on relay task updates so the broker can require a human.
- **The broker shows the human what a chord approves.** A pending permit raises an OS notification (Windows balloon, macOS `display notification`) with the title, classes and chord, and is listed by `firekeep hands status` from `~/.firekeep/hands/pending.json`. The chord still approves the oldest pending permit; that residual is disclosed.
- **Broker token (§8).** The token is NOT only in memory: `~/.firekeep/hands/broker.json` (0600) holds `{port, token, pid, started_at, chord}` so the MCP server and `firekeep doctor` can reach the broker. A same-user process can read it — an OPEN residual in the threat model; the chord requirement still stands.
- **Autostart (§3.1).** Windows: a per-user `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` value launching `pythonw.exe -m firekeep_hands.broker run` — `schtasks /Create /SC ONLOGON` is "Access is denied" for an unelevated user. macOS: the LaunchAgent as designed.
- **Keep wiring (§3.6).** `action_before`/`action_after` use cortex's real tool shapes (`session_id`, `agent_id`, a list `success_criteria`; `success: bool` + `deviation_notes`); a Keep `block` decision refuses `hands_task_start`. The machine lease `hands:<machine_id>` is renewed with `relay_heartbeat`; a lease held by our own agent id from a dead session is reclaimed, another agent's never; the server releases the lease and marks the ledger `abandoned` on shutdown. Evidence stays local (`task.json` with a step count, hash-chained `steps.jsonl`, before/after images for protected steps — native and browser); no replay POST route in PR1.
- **Installation.** `firekeep hands enable --from <checkout>/hands` is the PR1 path; the bare PyPI install is refused until `HANDS_PYPI_PUBLISHED` flips after the first trusted-publisher release. The wheel is never bundled by the bootstrap. `hands` is a `role: capability` registry entry, never seeded.
- **Verification (§10).** Windows: unit + live (Notepad end to end through the real MCP server, injected chord rejected against a running broker, browser boundary and allowlist, broker teardown) — done 2026-09-05. Not done: a real human chord press, everything macOS (no Mac reachable from the build session; `hands/scripts/demo_textedit.md` is the runbook and includes the `kCGEventSourceStateID` measurement), phone approvals end to end, multi-monitor pointer maths on hardware. The guide's "Verified" table is the record.
