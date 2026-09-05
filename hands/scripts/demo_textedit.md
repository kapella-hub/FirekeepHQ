# Hands live demo — macOS (TextEdit)

Run on a Mac where `firekeep hands enable --from <checkout>/hands` has run,
macOS has been granted Accessibility and Screen Recording for the kit's
python (the first `hands_observe` prompts for them) and Input Monitoring for
the broker (`firekeep doctor` shows the `hands` row `ok` with the chord
listener `active`).

## 1. Unprotected task

> Use the `hands_*` tools only. Call `hands_task_start` with goal "write a
> note" and apps ["TextEdit"]. `{"kind": "open_app", "app": "TextEdit"}`,
> `hands_observe`, find the text area, `{"kind": "type", "text": "Hands was
> here <today's date>"}`, then `{"kind": "key", "chord": "cmd+s"}`, observe the
> save sheet, `set_value` the name field to `demo.txt`, choose the folder
> `~/.firekeep/hands` (type the path with `cmd+shift+g` if needed), invoke
> Save, then `hands_task_end` with outcome "done".

Expected: no `needs_permit`; `~/.firekeep/hands/demo.txt` exists;
`firekeep hands evidence` lists the task.

## 2. Protected step, chord, deny, injected input

Same as the Windows demo: ask for "empty the Trash" in Finder →
`needs_permit` with `["destroy"]`; approve with the chord (default
`Ctrl+Alt+Y`); deny with `Ctrl+Alt+N`; with a permit pending post a TAGGED
synthetic chord from another terminal and confirm it is ignored:

```
python - <<'EOF'
from firekeep_hands.backends.mac import MacBackend
MacBackend().key("ctrl+alt+y")   # every event carries HANDS_TAG
EOF
```

## 3. Measure the source-state claim (Task 15 requirement)

The listener also filters on `kCGEventSourceStateID != 1`. That claim was
never measured. Run the live measurement test and record what it prints:

```
FIREKEEP_HANDS_LIVE=1 python -m pytest hands/tests/live/test_mac_textedit.py -k source_state -s
```

It posts an UNTAGGED `CGEventCreateKeyboardEvent(None, …)` while a
listen-only tap records `(keycode, flags, userData, sourceStateID)`. If the
synthetic event reports `sourceStateID == 1`, the source-state filter is
best-effort only and the guide's "Honest limits" and the threat model must say
that the tag is the only discriminator for Hands' own events.

## 4. Browser boundary

As on Windows: `https://example.com` needs a boundary permit until
`firekeep hands allow domain example.com`.
