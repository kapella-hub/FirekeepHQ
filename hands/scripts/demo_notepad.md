# Hands live demo — Windows (Notepad)

Paste this into a fresh Claude Code (or any runtime) session on a Windows
machine where `firekeep hands enable` has run and `firekeep doctor` shows the
`hands` row `ok`. It exercises the unprotected path end to end; the protected
paths follow below.

## 1. Unprotected task (no permit expected)

> Use the `hands_*` tools only. Call `hands_task_start` with goal "write a
> note" and apps ["Notepad"]. Open Notepad with
> `{"kind": "open_app", "app": "notepad"}`, call `hands_observe` and find the
> text area, then `{"kind": "type", "text": "Hands was here <today's date>"}`.
> Save it as `%USERPROFILE%\.firekeep\hands\demo.txt`: press `ctrl+shift+s`,
> `hands_find` the file-name box and the Save button (the dialog is a new
> window — observe again), `set_value` the path, `invoke` Save. Then
> `hands_task_end` with outcome "done" and a one-line summary.

Expected: every step returns `ok: true`, no `needs_permit`; `demo.txt`
exists; `firekeep hands evidence` lists the task with at least six steps.

## 2. Protected step — chord approval

> In the same session, start a task "empty the recycle bin" with apps
> ["Explorer"], open Explorer, `hands_find` "Recycle Bin", invoke it, then
> find and invoke "Empty Recycle Bin".

Expected: the last `hands_act` returns `needs_permit` with classes
`["destroy"]` and a challenge. Press the approve chord (`firekeep hands chord`
shows it; default `Ctrl+Alt+Y`) while the runtime waits in
`hands_request_permit`; the step then runs and `steps.jsonl` records
`"via": "chord"`.

Deny path: repeat, press the deny chord (default `Ctrl+Alt+N`) —
`hands_request_permit` returns `{"state": "denied"}` and nothing happens.

## 3. Injected input is ignored

With a permit pending, run from another terminal:

```
python -c "from firekeep_hands.backends import _win_input as w; w.send(w.build_key_chord('ctrl+alt+y'))"
```

Expected: the permit stays `pending` (`firekeep-hands-broker status` shows it;
with `FIREKEEP_HANDS_LOG=DEBUG` the broker logs `real=False` for the six
injected events). Press the real chord to clear it.

## 4. Phone approval (only if you opted in)

`firekeep hands config set phone_approvals true`, restart the broker, then
with a permit pending open the dashboard on your phone → Relay tab → the
`hands_permit:` row → Approve. Read the guide's trust-boundary note first:
relay records no approver yet, so with this on anyone holding the workspace
key can complete the task.

## 5. Browser boundary

> Ask the runtime to open `https://example.com` in the Hands browser and read
> the heading.

Expected: before `firekeep hands allow domain example.com` the navigate
returns `needs_permit` with classes `["boundary"]`; after it, the page loads
and `hands_browser read` returns the text.
