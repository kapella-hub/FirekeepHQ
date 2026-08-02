# Uncommitted-Work Preservation — Local Snapshots and a Destructive-Command Guard

**Status:** design, approved 2026-08-02

**Origin:** on 2026-08-02 an agent (me) ran `git checkout -- cortex/app/` to undo its own
botched edit script. That reverts every tracked file in the directory, and nine of them
carried the user's uncommitted work — the memory-GC preview/restore feature
(`preview_memories`, `POST /memory/restore`, `/dashboard/api/memory-gc`). The
implementation was destroyed. Its tests (+976 lines) and docs survived, because they live
outside `cortex/app/`.

Nothing in Firekeep prevented, detected, or could recover it. This spec fixes that.

## Why the existing machinery did not help

**Bridge captured the loss and not the work.** `_git.workspace_snapshot()` — the payload
`stop` and `prompt` persist — runs `git diff --stat`:

```python
diff   = _git(["diff", "--stat"], cwd=cwd) or "no changes"
staged = _git(["diff", "--cached", "--stat"], cwd=cwd) or "nothing staged"
```

It faithfully recorded `63 files changed, 2776 insertions(+), 888 deletions(-)`. It knew
the work existed and its exact size, and retained none of its content. One flag separates
proving the work existed from restoring it.

**The policy engine is the layer that should have caught it, and is structurally blind.**
`pre_tool.py` already implements shell handling — `_action_type("Bash") -> "run_command"`,
`_target` returns `tool_input["command"]` — but the Claude adapter registers:

```python
("PreToolUse",  "pre_tool",  "^(Edit|Write)$",                5),   # the BLOCKING gate
("PostToolUse", "post_tool", "^(Edit|Write|MultiEdit|Bash)$", 10),  # after it ran
```

So `pre_tool` is never invoked for `Bash`; that branch is dead in production, while
`PostToolUse` observes the command *after* execution. The engine will refuse an `Edit` to
`.env` — one file — and cannot see a single shell command that destroys nine.

This is the same defect class this codebase has repeatedly shipped and deleted:
capability built, wiring absent, nobody notices, because the feature still looks alive
(`trace_links` schema with zero writers; `SessionFeatures.file_paths` whose producer never
emits; skills unreachable from every recall path; a distill queue filled for months and
never drained; the corpus graph written and never read; `_GRAPH_LABELS` referenced by
nothing). This instance cost real work.

## Goal

Make uncommitted work recoverable on the developer's machine, and make destructive
commands announce themselves. Two components, both **client-side, local, deterministic,
and offline**.

**Non-goals, explicitly:**

- **Diffs must never reach the server.** See §1. This is a hard constraint, not a
  preference.
- **No LLM, no agent-gateway round trip, no network on the hot path.**
- **Not a replacement for committing.** A snapshot is a safety net under a mistake, not a
  substitute for version control.
- **Not a blocker.** Approved posture is snapshot-then-allow (§2).

---

## 1. Why snapshots stay on the machine

The obvious design — put the diff in the Bridge session shadow, where the `--stat` already
goes — is wrong, and it is worth recording why so it is not re-proposed.

A raw `git diff` contains whatever was being edited: `.env` files, private keys, customer
data, credentials mid-rotation. Shipping that to a **team** memory server inverts the
guarantee personal mode exists to provide.

Cortex has a scanner for exactly this — `cortex/app/secret_scan.py` (`scan_text`, regex
patterns plus Shannon entropy), already used on `/memory/learn`. It cannot be reused here:
it is server-side, and the hook cores are deliberately stdlib-only so that `pre_tool` does
not drag heavy imports into every edit. Reimplementing it client-side would be a second
copy of security-critical logic — the precise failure the `search_skill_points`
consolidation was written to end.

So: snapshots are written under `~/.firekeep/worktree-snapshots/` and never transmitted.
No scanner is needed, because nothing leaves the machine that was not already on it.

**Personal mode does NOT disable snapshots** (deliberate). `resolver.is_bypassed()` means
"nothing reaches the server"; these are local files, and disabling them would withdraw
recovery exactly when someone is doing sensitive work by hand. The content is already on
disk in the working tree; a local copy is not new exposure. This is the one place a
bypass check is deliberately absent, so it is stated here rather than discovered later.

## 2. Component A — `worktree_snapshot.py`

`capture(repo_root, *, reason) -> Path | None`, in `client/firekeep_client/`.

**Contents**, per snapshot directory `~/.firekeep/worktree-snapshots/<repo-slug>/<ts>/`:

| File | Produced by | Restores |
|---|---|---|
| `tracked.patch` | `git diff HEAD` | every uncommitted change to tracked files, staged or not |
| `untracked/…` | file copies | files `git clean -fd` would delete and no diff contains |
| `meta.json` | — | branch, HEAD sha, reason, file counts, and every truncation |

`git diff HEAD` (not bare `git diff`) is required: it captures staged and unstaged changes
in one patch, so a partially-staged tree restores whole.

**Bounded, and honest about it.** `FIREKEEP_SNAPSHOT_MAX_BYTES` (default 8 MiB total) and a
per-file cap for untracked copies. Exceeding either is recorded in `meta.json` as an
explicit `truncated` list. A snapshot that silently dropped content would be worse than
none — it would look like a safety net and fail on use. This is the same
publish-your-own-yield rule the archmap spec applies to its collectors.

**Rotation:** keep `FIREKEEP_SNAPSHOT_KEEP` (default 20) most recent per repo, prune
oldest. Bounded disk, no unbounded growth path.

**Never raises.** A snapshot failure must not fail a hook or block a command. It logs via
`hooklog` and returns `None`.

**Cadence:** on the existing `prompt` periodic tick (which already snapshots workspace
state) and before any command the §3 guard matches. Cheap: `git diff HEAD` on an
unchanged tree is fast, and an empty diff writes nothing.

## 3. Component B — the destructive-command guard

Widen the Claude adapter matcher to `^(Edit|Write|Bash)$`. The hook core already routes
`Bash` to `run_command`; only the regex excluded it.

**Deliberately NOT the agent gateway.** Routing every Bash call through
`POST /agent/action/before` would put a 5 s network timeout on the hottest tool, and that
gate fails open — so the one command that matters would sail through whenever Cortex is
slow or down. The check is instead pure-local, deterministic, and offline, matching the
repo's own "if the server ever needs to think, you've lost" position.

**Two conditions, both required, before anything happens:**

1. The command matches a destructive pattern:
   `git checkout -- <path>` / `git checkout .`, **`git restore <path>`** (the modern
   spelling — omitting it would leave the same hole), `git reset --hard`,
   `git clean -f…`, `git stash drop|clear`, `rm -rf`.
2. The affected path actually has uncommitted changes — `git status --porcelain -- <path>`
   is non-empty.

Requiring (2) is what keeps this quiet. On a clean tree these commands destroy nothing and
the guard says nothing, so it does not become noise that gets disabled.

**Posture: snapshot, then allow.** Take a snapshot, exit 0, and return a `systemMessage`
naming the snapshot id and the restore command. Destructive git commands are frequently
exactly what the user wants; blocking them would fire constantly on intentional cleanups,
and an agent unable to revert its own bad edit will thrash — which is how this incident
started. The value is recoverability, not prohibition.

## 4. Component C — `firekeep restore`

```
firekeep restore --list [--repo PATH]     # ids, timestamps, branch, file counts, reason
firekeep restore --show <id>              # the patch, and what it would touch
firekeep restore --apply <id>             # git apply + copy untracked back
```

**This is not optional polish.** Snapshots without a recovery path are write-only
machinery, and this repo has deleted features for exactly that (`docs/HISTORY-NOTES.md`:
the corpus entity graph, removed after "an audit found 0 entities had ever been
extracted"; ~161K BACKLINK edges written and never traversed). A snapshot store nobody can
read from would be the same feature in a new costume.

`--apply` refuses to overwrite a dirty tree without `--force`, and prints what it will do
first. Restoring on top of unrelated changes is how a recovery tool becomes a second
incident.

## 5. Failure modes

- **Snapshot fails** → log, return `None`, allow the command. The guard must never be the
  reason work stops.
- **Not a git repo** → no-op, silently. Matches `symdexindex.is_indexable()`'s precedent.
- **Repo huge / diff enormous** → truncate to the cap and record it in `meta.json`.
- **Two sessions, one repo** → snapshot dirs are timestamped and per-repo; concurrent
  captures cannot collide destructively. No claim/lock needed because nothing is shared.

## 6. Testing

1. **Round trip, the load-bearing test:** dirty a scratch repo, capture, `git checkout --
   .`, apply, assert the tree is byte-identical to before. Anything less does not prove
   recovery.
2. Untracked files survive a `git clean -fd` and restore.
3. Truncation is recorded in `meta.json`, never silent.
4. Rotation prunes to `FIREKEEP_SNAPSHOT_KEEP` and keeps the newest.
5. Guard fires on each destructive pattern **only** when the path is dirty; silent on a
   clean tree.
6. `git restore` is matched, not just `git checkout --` (the modern-spelling hole).
7. Guard never blocks: exit 0 in every case.
8. `capture()` never raises — non-repo, unreadable dir, git absent.
9. **Regression guard for this incident:** `git checkout -- <dir>` against a dirty
   directory produces a snapshot from which that directory is fully recoverable.

## 7. What this would have done on 2026-08-02

The `prompt` tick would have captured `cortex/app/` hours before, and the guard would have
snapshotted immediately before `git checkout -- cortex/app/` ran. Recovery would have been
`firekeep restore --apply <id>` — seconds, not an IDE-history hunt with the implementation
gone and only its tests left to rebuild from.

## 8. Scope note

Client-only: `client/firekeep_client/` plus its tests. No server change, no new MCP tool
(honouring the `token-reduction` plan's "no new tool surface"), no schema change, nothing
deployed. It cannot regress any existing behaviour except by adding one matcher entry and
one local check.
