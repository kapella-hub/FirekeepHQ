# Hands — the desktop operator

> Reference for `firekeep-hands`: what it can do to your computer, what it will
> not do without you, and where the guarantees stop. Read it before you turn it
> on. The design record is
> [`docs/superpowers/specs/2026-09-05-firekeep-hands-design.md`](../superpowers/specs/2026-09-05-firekeep-hands-design.md);
> the security analysis is [`docs/THREAT-MODEL.md`](../THREAT-MODEL.md) §5.8.

## What it is

Hands is a local MCP server plus a local approval broker, shipped as the
`firekeep-hands` wheel and mounted behind the same `firekeep` gateway every
other tool goes through. With it registered, whatever runtime you already
use — Claude Code, Codex, Kiro, OpenCode, Studio — gains eight `hands_*` tools
that let it see the active window as a list of named controls, click and type
into them, drive a Hands-managed browser, and leave a hash-chained local record
of every step. The runtime is the brain; Hands is only the eyes and the fingers.
The comparison people reach for is Violoop, the box that sits between a PC and
its monitor: same operator model and the same insistence that a protected step
waits for a physical button, without the device — your runtime is the brain, and
a separate broker process is the button.

Hands is a **capability**, not a dex. It is the first entry in
`~/.firekeep/dexes.json` whose manifest carries `role: "capability"` — it
operates its domain rather than indexing it, and the registry treats it
accordingly: it is never seeded, never bundled with a release, and only ever
registered by a human typing `firekeep hands enable`. See
[`dexes.md`](dexes.md#the-registry-model) for why the registry is really a
capability registry and always was.

What Hands is not: a model, a chat product, a screenshot streamer, or a way
around your operating system's own permission prompts. It cannot act on a locked
screen, and it cannot drive an elevated window. Both are in
[Honest limits](#honest-limits), which is the section to read if you only read
one.

## Turn it on

```bash
firekeep hands enable --from <checkout>/hands
```

**`--from` is the only path that works today.** The wheel is not published to
PyPI yet, so a bare `firekeep hands enable` — and `--pypi` too — refuses rather
than `pip install`-ing a name a third party could still claim. The refusal names
the checkout form, and exits non-zero rather than half-enabling anything. When
the name is published this flag flips and `firekeep hands enable` installs from
PyPI on its own.

`enable` does four things, in order. The first three abort the command if they
fail; the fourth does not:

1. `pip install`s the wheel into the kit's own venv.
2. Proves `firekeep_hands` is importable. If it is not, it prints why and does
   **not** register — a registered capability with no wheel is a gateway backend
   that fails to start, which shows up as tools that quietly stopped existing.
3. Writes `hands` into `~/.firekeep/dexes.json`.
4. Installs the broker's autostart: a Scheduled Task named
   `FirekeepHandsBroker` at logon on Windows, a LaunchAgent labelled
   `ai.firekeep.hands-broker` on macOS. If this fails, `enable` says so and names
   `firekeep-hands-broker run` — but still reports success and exits zero, since
   the wheel is installed and registered by then. `--no-autostart` skips the step
   silently; start the broker yourself with `firekeep-hands-broker run`, or
   nothing will be able to approve anything.

The `hands_*` tools appear **on the next agent session** — the gateway reads the
registry once, at startup.

### What each OS asks for

**Windows** asks for nothing beyond the logon task. There is no per-app consent
gate for UI Automation, screen capture or synthetic input, so the only way any
of the three can be unavailable is a dependency that failed to import, and that
is exactly what `firekeep hands status` reports (`accessibility`, `screen`,
`input`). The Scheduled Task is created at LIMITED rights on purpose: a
low-level keyboard hook and a loopback socket both need no privilege, and
running the broker elevated would only widen what a bug in it could reach.

**macOS** asks for three separate TCC permissions, and they are not
interchangeable:

| Permission | Granted to | Without it |
|---|---|---|
| Accessibility | the kit's python (the MCP server) | no control tree, no clicks, no typing |
| Screen Recording | the kit's python (the MCP server) | `hands_observe(detail="screenshot")` and the evidence images fail |
| Input Monitoring | the kit's python (the **broker**) | the chord listener cannot install, so nothing can approve a protected step |

`firekeep hands status` reports the first two as `ok`/`missing`. It reports
`input` as `unknown` on macOS, because the backend genuinely cannot tell from
inside the process — the broker's own `/health` is what answers that, and
`firekeep doctor`'s `hands` row is where you will see it.

### Checking it

```bash
firekeep hands status     # platform, backend, permissions, broker, policy, last task
firekeep doctor           # one `hands` row alongside everything else
```

The doctor row is `fail` when the wheel is missing, `warn` when the broker is
not answering, `warn` when the broker is up but nothing can approve (no chord
listener and phone approvals off or offline), and `ok` otherwise. That third
case is worth stating plainly because it is silent otherwise: a broker with no
approval path refuses every protected step, correctly and uselessly.

## How a task runs

Eight tools. Three of them are the loop; the rest are bookkeeping.

| Tool | What it does |
|---|---|
| `hands_status` | Platform, backend health, permissions, broker, chord, current task. |
| `hands_task_start(goal, apps=[])` | Opens a task: prunes aged evidence, mints a task id, opens the ledger, takes the machine lease, tells the Keep. `apps` declares what you expect to touch; anything outside it is a `boundary` step. |
| `hands_observe(detail, app, region, max_nodes)` | `detail` is `summary`, `controls` (the default) or `screenshot`. Returns the active window and its interactive controls, each with a `ref` you act on. |
| `hands_find(query, role, app, limit)` | Controls matching text in the active window, folded into the current observation so their refs are live. |
| `hands_act(action, permit=None)` | One step. Routed, classified, gated, executed, ledgered. |
| `hands_request_permit(challenge, wait_s=45)` | Blocks until the human answers or the wait runs out (capped at 55 s so the call returns inside an MCP client's timeout). |
| `hands_browser(op, …)` | `open`, `tabs`, `navigate`, `read`, `find`, `click`, `fill`, `screenshot` against the Hands-managed browser. `navigate`, `click` and `fill` are classified and gated exactly like native steps, and can return `needs_permit`. |
| `hands_task_end(outcome, summary)` | `done`, `failed` or `abandoned`. Closes the ledger, tells the Keep, releases the lease. |

**Everything but `hands_status` needs an open task.** `hands_observe` and
`hands_find` refuse with `no_task` alongside `hands_act` and `hands_browser`, and
the reason is accountability rather than bookkeeping: an observation is a tree,
and possibly a screenshot, of the human's own screen, which may leave the machine
for a cloud model. Reading the screen belongs inside a declared task with a
ledger and a lease. Looking still **costs no step** and leaves no ledger line —
only the budget of acting is spent.

**Every ref goes stale after every act.** Anything Hands just did may have moved
or destroyed the controls it was looking at, so the observation is dropped at the
end of each step and a ref minted before it is refused with `stale_ref`. Observe
again; that is the intended rhythm, not a fault.

### The action union

`hands_act` takes exactly one of these, and the required keys are checked before
anything else happens:

| `kind` | Required keys | Route it takes |
|---|---|---|
| `invoke` | `ref` | the control's own Invoke/AXPress pattern, else a pixel click at its rect centre |
| `set_value` | `ref`, `value` | the Value/AXValue pattern, else click + select-all + type |
| `click` | `ref` (+ optional `button`, `double`) | pixel click at the control's rect centre |
| `type` | `text` | synthetic keystrokes, capped at **500 characters** |
| `key` | `chord` | a keyboard shortcut |
| `scroll` | `ref` (or the literal `"window"`), `dy` | wheel at the control's centre, or the window's |
| `focus_app` | `app` | the OS |
| `open_app` | `app` | the OS |
| `open_url` | `url` | the Hands-managed browser |
| `clipboard_set` | `text` | the OS clipboard |
| `wait` | `seconds` (≤ 10) | nothing |

**A model never supplies a coordinate.** An action carrying `x`, `y`, `point` or
`coordinates` is rejected outright as "raw coordinates are not an accepted action
shape"; every pointer action names a control by `ref` and Hands computes the
point from the rect that control reported in the observation the runtime was
actually shown. On Windows that point is normalised over the whole virtual
desktop, so a monitor placed left of or above the primary one — which gives the
desktop a negative origin — is addressed correctly rather than off by a screen.

The 500-character cap on `type` is not arbitrary. Typed text is delivered one
character at a time and paced, so a long string is a long window during which the
keystrokes keep landing wherever the foreground happens to be, and the foreground
is not something Hands controls. A few hundred characters is a form field; four
thousand is a document, and a document belongs in `set_value` on the field
itself, where it arrives at one named control in one step. Above the cap the
action is refused with `invalid_action` and told to use `set_value`. On Windows
the elevation guard is additionally re-checked every 100 characters while typing,
so a guard that was true when the step started cannot decay silently while focus
moves under it.

### A worked example

Open Notepad, write a note, save it, and then step outside what the task
declared.

```
hands_task_start(goal="write today's note and save it", apps=["notepad"])
  -> {"ok": true, "task_id": "h-3f9c21a04b7e", "evidence": "…/hands/evidence/h-3f9c21a04b7e",
      "max_steps": 400, "keep": "online"}

hands_act({"kind": "open_app", "app": "notepad"})
  -> {"ok": true, "step_index": 0, "route": "os", "classes": []}
     # "notepad" is in the task's apps, so this is not a boundary step

hands_observe()
  -> {"window": {"app": "notepad", "title": "Untitled - Notepad", "elevated": false},
      "controls": [{"ref": "w3a10f:42.1.5", "role": "Document", "name": "Text editor", …}], …}

hands_act({"kind": "type", "text": "Shipped the broker today."})
  -> {"ok": true, "step_index": 1, "route": "input", "classes": []}

hands_act({"kind": "key", "chord": "ctrl+s"})
  -> {"ok": true, "step_index": 2, "route": "shortcut", "classes": []}

hands_observe()                                   # refs went stale at step 2
hands_act({"kind": "set_value", "ref": "w4b112:9.2", "value": "note.txt"})
hands_act({"kind": "invoke", "ref": "w4b112:9.7"})   # the Save button

hands_act({"kind": "focus_app", "app": "excel"})  # not declared, not allowlisted
  -> {"ok": false,
      "needs_permit": {"challenge": "a1b2c3…", "title": "focus_app excel",
                       "classes": ["boundary"], "reason": "boundary: excel",
                       "expires_in_s": 60}}

hands_request_permit("a1b2c3…")                   # the human presses ctrl+alt+y
  -> {"state": "approved", "via": "chord", "challenge": "a1b2c3…"}

hands_act({"kind": "focus_app", "app": "excel"}, permit="a1b2c3…")
  -> {"ok": true, "step_index": 3, "route": "os", "classes": ["boundary"]}

hands_task_end("done", "note saved")
  -> {"ok": true, "steps": 4, "outcome": "done", …}
```

The permit round trip is the shape to internalise: **the same action, repeated
with the permit.** The challenge id is derived from the machine, the agent
session, the task, the step index and a hash of the action dict, so a permit
approved for one step cannot be replayed on a different one, on the same button
one step later, or by a second agent. Hands recomputes the id from the action it
is about to run and refuses a permit that does not match it.

## What needs approval

A step is protected when its **effect** falls in one of six classes. The classes
are decided by Hands from the routed action and its target — never from a label
the model supplies — and a protected step is refused unless a permit minted for
that exact step is consumed, once, before anything reaches the desktop.

| Class | What triggers it |
|---|---|
| `send` | an `invoke`/`click` whose control name or value matches `\b(send\|post\|publish\|submit\|reply\|tweet\|share)\b`, or a `key` action whose chord is `ctrl+enter`, `cmd+enter` or `cmd+shift+d` |
| `money` | the control text **or the window title** matches `\b(pay\|buy\|purchase\|checkout\|transfer\|donate\|place order\|order now\|confirm payment\|subscribe)\b` |
| `destroy` | control text or window title matches `\b(delete\|remove\|erase\|format\|uninstall\|empty (the )?(trash\|recycle bin)\|discard\|shred\|factory reset\|permanently)\b`, or a `key` action whose chord is `delete`, `shift+delete`, `cmd+backspace` or `cmd+delete` **while Explorer or Finder is in front** |
| `credential` | a `type`/`set_value` into a control whose role is `PasswordBox` (Windows), `AXSecureTextField` (macOS) or a web `<input type="password">`; or into a control whose text matches `\b(password\|passcode\|passphrase\|otp\|2fa\|verification code\|secret\|api key\|token)\b`; or a `clipboard_set` whose text matches `^[A-Za-z0-9_\-]{32,}$` |
| `install` | control text or window title matches `\b(install\|run as administrator\|allow access\|grant\|enable extension\|add extension\|trust this)\b`, or an `open_app` whose target ends in `.msi`, `.exe`, `.pkg`, `.dmg` or `.app` and is not allowlisted |
| `boundary` | an `open_url` to a host outside the domain allowlist, or an `open_app`/`focus_app` naming an app neither declared in `hands_task_start(apps=…)` nor allowlisted |

Two deliberate asymmetries are worth knowing. `send` reads only the clicked
control's own name, because a bare "OK" or "Yes" button does not say what it
sends and the control that was actually invoked does. `money`, `destroy` and
`install` also read the **window title**, because a confirmation dialog puts the
meaning there ("Confirm payment", "Delete file?") and leaves a generic "Yes" on
the button.

And the regexes are narrow on purpose. They match a control's own name and value,
not arbitrary action payloads: a `key` action's chord field or a `type` action's
text can contain any string a model or a user produces, and matching those
against words like "delete" would classify by what was typed rather than by what
the action targets.

The `credential` shape rule deserves its own line: a pasted secret looks like an
opaque token, not a sentence, so `clipboard_set` is matched on shape — 32 or more
characters of `A-Za-z0-9_-` and nothing else — since a generated API key never
repeats and no word list could catch it.

**`type` has no target of its own**, so it is judged against a *focus hint*: the
control the last successful `click`, `invoke` or `set_value` touched. That is what
makes "click the password box, then type" a `credential` step rather than an
unclassified one. The hint is cleared by `focus_app` and `open_app`, because focus
has then gone somewhere this session knows nothing about and a stale hint would
judge the keystrokes against a control in a window that is no longer in front. It
is best-effort by construction: a human or the application can move focus between
the two steps and Hands cannot know.

**Web controls are classified the same way native ones are.** A page's "Place
order" button is the same decision as a native one, and an `<input
type="password">` is the same credential target as a `PasswordBox` — the surface
a button is drawn on is not a reason to ask the human less often. The classifier
reads the descriptor from the last `hands_browser op=find` scan, with the page's
own title standing in for a window title (the URL is deliberately *not* used as
the title: matching destructive words against a path would flag every click on a
documentation page). The probe decides a password field from its `type` attribute
**before** any author-supplied `role`, so a page cannot label its own password box
`role="textbox"` to opt out; it also refuses to fall back to a field's current
value when building an accessible name, which would otherwise put the secret
already sitting in the box into a name that travels to the model.

### The chord

```bash
firekeep hands chord                        # print both
firekeep hands chord set ctrl+alt+enter     # approve
firekeep hands chord set-deny ctrl+alt+q    # deny
```

The defaults are **`ctrl+alt+y` to approve and `ctrl+alt+n` to deny**. Changing
either writes `config.json` and takes effect when the broker restarts; the
command says so.

The broker ignores every synthetic keystroke, which is the whole point of it
being a separate process. On Windows a `WH_KEYBOARD_LL` hook checks two bits and
requires both clear: `LLKHF_INJECTED` (`0x10`), set on anything delivered through
`SendInput` or `keybd_event` — including every key Hands itself types — and
`LLKHF_LOWER_IL_INJECTED` (`0x02`), which additionally marks injection from a
lower-integrity process. On macOS a listen-only `CGEventTap` applies two
independent filters: Hands stamps every event it posts with its own marker in
`kCGEventSourceUserData`, and any event whose source state is not
`kCGEventSourceStateHIDSystemState` is rejected as synthetic regardless of who
posted it.

A permit is one-use, has a 60-second TTL, and lives only in the broker's memory.
Expiry applies to an approved permit as well as a pending one, so a human who
approves and then walks away does not leave a usable permit behind them.
`denied`, `expired` and `consumed` are terminal: a fresh attempt at the same step
needs a whole new permit, which needs a whole new human. A restarted broker holds
no permits at all, which is the correct failure direction.

**The chord approves the oldest permit still waiting**, because a keystroke
carries no permit id. In practice a task issues one protected step at a time and
waits for it, so there is one thing pending; if there is more than one, the
oldest is what you answered.

### The allowlist

```bash
firekeep hands allow app "Notepad"
firekeep hands allow domain example.com
firekeep hands allow list
firekeep hands allow forget <class> <app> <match>
```

Both lists start **empty** — nothing is allowlisted by default, including the
Keep's own dashboard. An allowlisted app stops being a `boundary` step (and stops
tripping the installer-extension check); an allowlisted domain stops navigation
to it being a `boundary` step, matching the host exactly or as a subdomain.

`policy.json` also carries a `remembered` list — approvals that stand for a
while, so a human is not asked the same question every time — and `decide()` does
consult it. **Nothing writes an entry to it in this release.** There is no
auto-remember path: an approval is good for exactly the one step it was minted
for. `allow forget` removes remembered entries, which is only useful against a
file you edited by hand. Treat the standing-approval story as unbuilt rather than
as a feature you have not found yet.

### Phone approvals, and what turning them on trusts

**Off by default.**

```bash
firekeep hands config set phone_approvals true
firekeep-hands-broker run            # or log out and back in
```

With them on and a Keep configured, the broker posts each pending permit as a
relay task titled `hands_permit:<challenge>`, and the dashboard's Relay tab draws
Approve and Deny buttons on that row. Approve completes the task with the result
`approve`; Deny cancels it. The broker polls, reads the answer, and mints or
refuses the permit. `firekeep hands status` and the doctor row report the phone
listener as one of three words: `off` (not opted in), `offline` (opted in, but
this machine has no Keep to post to), or `active`.

Here is what you are agreeing to, in plain words. **Relay does not record who
completed a task.** It writes the status, the result and the assignee, and never
the completing principal. So "the task says approve" means "somebody holding the
workspace API key completed it" — not "a person decided". The agent driving Hands
holds that key and reaches `relay_task_list` and `relay_task_update` through the
same MCP surface it already has. With phone approvals on, that agent can post its
own step for approval, complete its own `hands_permit:` task with the result
`approve`, and be granted the permit within one poll. A local one-time code would
not close it either, because Hands can screenshot the screen the code is on.

Turning `phone_approvals` on is therefore a deliberate decision to trust every
holder of the workspace key, the driving agent included. It stays off until relay
stamps the verified principal on a task update, which is the PR2 fix. The chord
path has no equivalent hole: it needs a physical keystroke that no process can
forge.

## Modes

Violoop offers three modes. Here is where each one lands.

| Violoop | Hands today |
|---|---|
| Long-running cross-app task | **Task mode.** `hands_task_start` → observe/act loop → `hands_task_end`, with a permit at every protected step. This is the whole product in this release. |
| Away — finish while you are gone, approve from the phone | **Phone approvals**, opt-in, on an unlocked screen. Input injection does not reach a locked session, so "away" means away from the keyboard, not away from the machine. |
| Learns by watching | **The local ledger only.** Every step is recorded with its route, classes and outcome, but there is no client-to-Keep replay route yet, so nothing promotes a repeated routine into a procedure. |
| Instant — hints beside the cursor | Not built. It belongs to a Studio Operate mode. |

## Evidence

Every task writes a directory under `~/.firekeep/hands/evidence/<task_id>/`:

```
task.json           goal, apps, machine_id, session_id, started, ended, outcome, summary, steps
steps.jsonl         one hash-chained JSON line per step
NNN-before.png      before/after images, for protected native steps only
NNN-after.png
```

Each line in `steps.jsonl` carries the step index, a timestamp, the action, the
route it took, the classes it triggered, the permit if any, the sha256 of each
image, the outcome and any error — plus a `chain` field:
`sha256(previous_chain + the canonical JSON of this line without its chain)`,
starting from the empty string. A dropped, reordered or edited line breaks every
chain after it, so the log is tamper-evident offline, with no remote party
co-signing anything. Images are separate files with only their hashes in the
line, so the hash still binds the image into the chain while a reader can page
through the log without loading megabytes of PNGs.

Three details that surprise people:

- **`task.json["steps"]` is a count, not a list.** `steps.jsonl` is the one store
  of record; closing a task never duplicates its whole log into a second file.
- **Screenshots are captured for protected *native* steps only.** An ordinary
  click leaves a ledger line with `before` and `after` both null — and so does a
  browser step, permitted or not, since `hands_browser` records no images at all.
  Capture is also best-effort:
  the permit has already been consumed by then, so a machine that cannot
  screenshot must not turn the human's approval into a refusal — the failure is
  logged and the line simply carries no image.
- **A typed secret is redacted in the ledger.** A `type`, `set_value` or
  `browser.fill` that classified as `credential`, or that targeted a credential
  role, has its `text`/`value` replaced with `<redacted:credential>` in the
  recorded line. Only in the recorded line: the permit's challenge is hashed from
  the real action and the real action is what runs. Evidence should say a password
  was typed into that field at that moment; it should not be where the password
  lives for the retention period. The redaction also fires on a credential *role*
  even when the class was dropped by a standing allowance — a human choosing not
  to be asked again is not a human choosing to write their password into a file.

Retention is 14 days, applied at `hands_task_start` — so pruning happens when you
next use Hands, not on a timer. It is deliberately conservative: a task directory
whose `task.json` is missing or unreadable is left alone rather than guessed at.

Read it back with:

```bash
firekeep hands evidence                    # every task, newest first
firekeep hands evidence h-3f9c21a04b7e     # one task's steps
```

### What reaches the Keep, and what does not

| Reaches the Keep | How |
|---|---|
| The task itself | `action_before` at start (goal, machine, declared apps) and `action_after` at end (success, outcome, summary) |
| One operator per machine | a relay lease on `hands:<machine_id>`, taken at `task_start`, renewed via `relay_heartbeat` every 10 steps, released at `task_end` |
| Pending approvals | relay tasks titled `hands_permit:<challenge>` — **only when phone approvals are on** |

| Does not reach the Keep | Why |
|---|---|
| The per-step trail | there is no client-to-Keep replay route yet; replay events are emitted server-side and there is no `POST /replay/events`. The steps are local and inspectable, and PR2 adds the route |
| Screenshots | they never leave the machine on the evidence path at all |

A refused lease is fatal to a task on purpose: two Hands sessions interleaving
actions on one screen is worse than not starting, so `hands_task_start` raises
`busy`, naming who holds the machine and the wall-clock time the lease lapses,
rather than proceeding with a lease that enforces nothing.
A lease call that merely *failed* — no Keep, unreachable server, personal mode —
is not a refusal, and Hands keeps working.

**A crashed session's lease is indistinguishable from a live one, and there is no
override.** If a previous Hands session died holding `hands:<machine_id>` — the
runtime was killed, the machine lost power mid-task — the lease stays held until
its TTL runs out, and every `hands_task_start` until then refuses with that dead
session named as the holder. The TTL is **30 minutes** from when the lease was
taken or last renewed, and the refusal prints the wall-clock time it lapses, so
the answer is to wait for that time. `hands_task_start` takes no force flag in
this release, deliberately: from here, a stale lease that is safe to break and a
live agent one keystroke into a bank transfer look exactly alike.

Everything on the Keep path is best-effort: a five-second timeout, every failure
logged and swallowed, and no network call attempted at all when the machine has
no Keep configured. One consequence to be explicit about: **`action_before`'s
verdict is not enforced.** Hands reads the action id out of the reply and nothing
else, so the Keep's policy engine cannot block a Hands task in this release. The
gate that works is local: the six classes and the broker.

## The browser

`hands_browser` drives Chrome or Edge over the DevTools protocol, against a
**dedicated profile** at `~/.firekeep/hands/chrome-profile`. Attaching to a
browser you already have open is not possible unless it was started with remote
debugging, so Hands launches its own instance instead. The profile starts empty:
**Hands has none of your logins until you sign into a site through the Hands
browser yourself**, which is the point, and the moment you do, that session is
inside the agent's reach for as long as it stays signed in. `browser` in
`config.json` selects `auto`, `chrome` or `edge`.

Navigation is the one browser operation that can leave the ground the task
declared, so it routes back through `hands_act` as an `open_url` and gets the
`boundary` class like anything else. `click` and `fill` are classified in place
against the page's own descriptors, and go through the same permit gate a native
step goes through. Every op is a ledgered step against the same budget: a task
that clicks its way through a page has done that many things.

Controls come from a DOM probe that stamps each ref with the scan that minted it
— `g<generation>-d<N>`, where the generation counter lives on the page and bumps
once per scan. A ref from any scan but the most recent is rejected as stale
before the DOM is even consulted, and `click`/`fill` re-resolve the ref to a
*current* rect at the moment of the call, so a page that scrolled or reflowed in
between gets clicked in the right place or reports the ref gone. A ref this
session has no descriptor for is refused outright rather than run unclassified —
acting on a ref with no descriptor would be acting with no classifier.

**Page scans, unlike native observations, survive a step.** A native
`hands_observe` costs nothing, so "look again, then retry with the permit" lands
on the same step index and the approval still fits. A browser `find` *is* a step,
so dropping the scan every step would move the index between the refusal and the
retry and make a browser permit impossible to spend. Navigation is what clears the
scan, because the whole document changed underneath it.

Four limits to plan around:

- **`fill` inserts at the caret.** It types into whatever the field already
  contains, exactly like a person. Nothing clears the field first; replacing text
  is the caller's job.
- **Only the top-level document is probed.** No iframe crossing, no shadow DOM
  piercing. A control inside either is invisible to `find` and cannot be clicked.
- **`navigate` waits up to 10 seconds for the load event**, and the result carries
  `url`, `title` and `loaded` — `loaded: false` means the load event had not
  arrived in time, which is information to act on rather than an error: a slow or
  streaming page still reports where it is.
- **Screenshots are downscaled to 1280 px wide** before they leave the browser.

## Honest limits

Read these as the specification, not as caveats.

**A locked screen is out of reach.** Input injection does not reach the Windows
secure desktop or the macOS lock screen. Violoop's HID hardware does; software
does not. "Away mode" here means approving from your phone while the machine
stays unlocked. A locked-session design — a separate user session, a VM, an RDP
loopback — is a different spec that has not been written.

**Elevated windows are out of reach.** A normal-integrity process cannot drive a
higher-integrity (UAC-elevated) window through UI Automation, so Hands reports
`elevated_target` and refuses rather than pretending it clicked. The guard is
re-checked mid-typing, and a process that refuses to open at all is treated as
elevated — the safe direction.

**Screen data leaves the machine whenever the runtime asks for a screenshot.**
Unlike Violoop, the brain here is usually a cloud model, so `hands_observe(detail
= "screenshot")` sends your screen to it. Accessibility trees are the default for
exactly this reason: they are text, cheap, precise, and they stay closer to the
things you meant to expose. There is no per-task screenshot switch in this
release; the control you have is not asking for one.

**Prompt injection through observed UI text is real and is not solved.** A web
page or an application can put text in front of the model that reads like an
instruction. Permits and the allowlist bound the damage; they do not remove the
risk. Anthropic's own computer-use guidance — isolate the environment, withhold
credentials, allowlist domains, confirm consequential actions with a human — is
the baseline this design assumes, not a stronger claim than it makes.

**The broker draws nothing on your screen.** It is a loopback service and an
input listener, with no window, no notification and no prompt of its own. Hands
builds a description of the step from the *routed* control's own name and the
window's app, capped and stripped of anything unprintable, and hands it back to
the runtime as `needs_permit.title` — but **the runtime is what shows it to you**,
and on the chord path the runtime is also the thing you are gating. If you are
approving with the chord, you are trusting your agent to have told you honestly
what it is about to do; look at the screen before you press it. The dashboard
renders the broker's own text on the phone path, which is the only path where
that guarantee is structural.

**Two-hop trust at the input layer.** The broker trusts the operating system's
injected flag and event source. A kernel-mode input driver can originate events
with no injection bit set, and that defeats it. This filter stops user-mode
malware and honest mistakes, not a rootkit — the same boundary Violoop's hardware
draws in a different place.

**The macOS source-state filter is unverified on hardware.** It is implemented as
specified, and the tap logs `(keycode, flags, userData, sourceStateID)` at DEBUG
under `FIREKEEP_HANDS_LOG=DEBUG` precisely so the measurement can be made. Until
it is, the marker half of the filter is the half known to hold. See
[Verified](#verified).

**Phone approvals trust every holder of the workspace key** when you turn them
on. Spelled out above; it is the largest open item in this release.

**Standing approvals do not exist.** Nothing writes a `remembered` entry, so
every protected step asks every time.

**A stale machine lease locks you out for up to 30 minutes.** There is no force
flag; see [Evidence](#what-reaches-the-keep-and-what-does-not).

**The Keep cannot veto a task.** `action_before` records; it does not block.

**No Linux.** `hands_status` reports the backend as unsupported and every other
tool refuses. AT-SPI is a later job.

**A task is capped at 400 steps**, 200 controls per observation and 4000
characters of text. When the budget runs out the tools say so and ask for
`hands_task_end`.

## Turning it off

```bash
firekeep hands disable            # unregister, remove the broker autostart
firekeep hands disable --purge    # …and delete ~/.firekeep/hands entirely
```

`disable` removes the registry entry and the logon task or LaunchAgent. The
`hands_*` tools disappear on the next agent session — the gateway reads the
registry once, at startup, so a session already running keeps them until it ends.
The wheel stays installed and idle; `--purge` additionally deletes
`~/.firekeep/hands`, which takes your config, your allowlist, the Chrome profile
and **every evidence ledger** with it. There is no undo.

Between the two there is a third position worth knowing: leave Hands registered
and stop the broker. Every protected step then fails closed, and everything
harmless still works.

## Verified

Live checks as of the date shown, on the machine shown. This table is the honest
record, not a plan — a row that says "not yet" means nobody has done it.

| What | Status | When / where |
|---|---|---|
| Windows typing, including non-ASCII (`café`) and a 219-character string spanning the guard's 100-character chunks | **Verified** | 2026-09-05, Windows 11, Notepad |
| Windows pointer accuracy at four points across the virtual desktop | **Verified** | 2026-09-05, Windows 11 |
| Windows broker hook installed, and a `SendInput` chord rejected with `flags=0x10` | **Verified** | 2026-09-05, Windows 11 |
| Real Chrome, full flow, including a 12-second idle gap | **Verified** | 2026-09-05, Windows 11 |
| End to end through the real MCP server over stdio: `firekeep hands enable --from <checkout>/hands`, `firekeep doctor` `hands` row, then a Notepad task — open, focus, observe, `set_value` through UI Automation, `ctrl+a`/`ctrl+c`, typed text, a screenshot, close, dismiss the save prompt, end — eight ledgered steps with an intact hash chain | **Verified** | 2026-09-05, Windows 11, from a scratch kit venv |
| Injected approve chord against a *running* broker: the permit stayed `pending`, `consume` was refused, the DEBUG log carried event kinds only (no key codes) | **Verified** | 2026-09-05, Windows 11 |
| Browser boundary: navigating to a host that is not allowlisted returned `needs_permit` with class `boundary`; after `firekeep hands allow domain example.com` the same task loaded the page (`loaded: true`), read it and ended cleanly | **Verified** | 2026-09-05, Windows 11, real Chrome |
| `firekeep hands disable`-style teardown of the broker (`uninstall-autostart` stopped the running broker and removed `broker.json`) | **Verified** | 2026-09-05, Windows 11 |
| Windows autostart at logon | **Fixed after the check** | 2026-09-05: `schtasks /Create /SC ONLOGON` was "Access is denied" for an unelevated user, so the Windows autostart is now a per-user `Run` registry value launching `pythonw.exe -m firekeep_hands.broker run`; the Run-value path itself was verified writable and removable unelevated, and a logon has not yet been observed |
| A session that dies with a task open | **Fixed after the check** | 2026-09-05: the next `hands_task_start` on the same machine was refused for the full 30-minute lease; the server now releases the lease on shutdown and reclaims a lease held by its own agent id |
| A **real human** chord press accepted | **Not yet** | needs a person at the keyboard |
| Anything at all on macOS — AX tree, CGEvent input, `screencapture`, TCC prompts, the LaunchAgent | **Not yet** | no Mac was reachable from the build session; `hands/scripts/demo_textedit.md` is the runbook |
| The macOS source-state filter (`kCGEventSourceStateHIDSystemState`) against real hardware events | **Not yet** | measured by the live test in `hands/tests/live/test_mac_textedit.py`; the marker filter is the half known to hold until then |
| Phone approvals end to end through the dashboard | **Not yet** | off by default; needs a phone and the opt-in |
| Multi-monitor pointer maths on real hardware | **Not yet** | single display on the build machine; the arithmetic is unit-tested |
