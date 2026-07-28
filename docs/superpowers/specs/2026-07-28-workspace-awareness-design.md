# Workspace Collision Detection

_Date: 2026-07-28. Revision 3 — local-only. No server, no network, no Relay change._

Two agent sessions in one working tree overwrite each other's files silently. This detects that
and says so. Nothing is blocked; no worktree or container is created.

> **Revision 3 replaced the mechanism.** Revisions 1–2 put workspace identity on Relay presence
> and detected collisions server-side. Four review rounds established that the presence route was
> unworkable (self-matching predicate, a crackable digest, a migration that breaks presence
> registration fleet-wide) and that it contradicted `docs/STRATEGY.md:67` — "freeze new
> subsystems." The mechanism is now a marker file in the working tree's own git directory.
> Changelog in §10.

---

## 1. What is and is not a collision

| Situation | Physically | Response |
|---|---|---|
| **2 sessions, 1 working tree** | Two processes write one inode. Last write wins. Git never sees the lost state — the file was overwritten before any commit. **Silent data loss.** | **Loud warning. Nothing else can help.** |
| 2 checkouts, same branch | Two independent files on two disks. Both commit; git merges or reports a conflict. | Deferred (§8) — git already handles it |
| 2 checkouts, different branches | Independent. Ordinary development. | Deferred (§8) |

**Cross-machine "collision" is a category error.** Two people on two checkouts cannot overwrite
each other; the worst case is a merge conflict, which git reports loudly and refuses to resolve
wrongly. The only silent-data-loss case is one shared working tree — which is what this detects.

Note a working tree **can** be shared across machines: an NFS/SMB mount, two WSL distros over one
Windows path, a devcontainer bind-mounting the host directory. Those are real collisions and §2's
mechanism catches them, because identity travels with the *tree*, not the *user*.

---

## 2. Decisions

**D1 — Entirely local. No server, no network, nothing transmitted.** Collision is a property of
one working tree, so the evidence belongs beside that tree. Consequences: no Relay change, no
presence fields, no MCP tool, no version skew (fastmcp 3.1.1 rejects unknown kwargs — verified),
no migration, no auth surface, and **no privacy question at all**, because no path, hostname or
branch ever leaves the disk it describes.

**D2 — Tree identity is the git directory.** `git rev-parse --absolute-git-dir`. Verified:

```
main repo         --git-dir = .git
linked worktree   --git-dir = <main>/.git/worktrees/<name>   ← distinct per worktree
                  --git-common-dir = <main>/.git             ← shared across worktrees
```

So two linked worktrees of one repo get different directories and correctly do **not** collide —
which is also the escape hatch this feature recommends. `.git/` is the right home: it is never
committed, needs no `.gitignore` cooperation, and is the conventional place for tool state.

**D3 — One file per session. This dissolves L1 rather than solving it.** The original defect was
that Relay presence is one slot per `agent_id`, so a second window overwrote the first. Separate
files mean there is no shared slot. Two windows under one identity are two files. No re-keying,
no registry, no migration.

**D4 — Liveness is PID-checked on the same host, timestamp-bounded otherwise.** See §4.

**D5 — Fail toward silence.** Not a git repo, unwritable `.git`, unreadable marker, malformed
JSON, any exception at all → no output. A false collision warning costs more than a missed one,
because it teaches the reader to skip the notice.

**D6 — Independent of the presence lifecycle fix.** This reads no presence, so the two land in
either order. (The lifecycle bug — `stop.py` deregistering every turn — is real and worth fixing
on its own merits; it is simply not a prerequisite here.)

---

## 3. Mechanism

```
<absolute-git-dir>/firekeep/sessions/<agent_id>__<window_id>.json
```

`window_id` is a uuid4 hex minted once per session and kept in the existing session stash
(`client/firekeep_client/state.py`), reused by every refresh and deleted at session end. It is
**not** the Bridge `session_id` and **not** the runtime payload id: review established that
`session_id` is empty or ambiguous at announce time on every runtime, and that presence's
`session_id` field already holds two incompatible id spaces. A client-minted id is non-empty by
construction and independent of every runtime's payload shape.

Filename components are validated against `^[A-Za-z0-9_-]+$` before use; `__` separates them and
cannot appear in either.

```json
{ "agent_id": "marat", "window_id": "9f2c…", "branch": "feat/x",
  "goal": "wire the licence gate", "pid": 48213, "hostname": "MARAT-PC",
  "started_at": 1769592000.0, "last_seen": 1769592840.0 }
```

**Lifecycle** — `session_start` writes it and reads siblings; `prompt` refreshes `last_seen` and
`branch` (branches change mid-session); session end deletes it. Writes are atomic: write to a
temp name in the same directory, then `os.replace`.

**Read** — list `sessions/*.json`, drop your own filename, drop any that fail §4's liveness test,
and report what remains.

---

## 4. Liveness

| Case | Test |
|---|---|
| `hostname` matches this host | `os.kill(pid, 0)` — dead process → stale, **removed on sight**. Near-instant. |
| `hostname` differs (shared tree) | `now - last_seen < STALE_AFTER_SECONDS` (default 900). A PID from another machine is meaningless and could match an unrelated local process, so it is never consulted. |
| `pid`/`hostname` absent or malformed | timestamp only |

**Why not an OS file lock**, which would be strictly better — the kernel releases it on crash, so
stale entries would be impossible: **Firekeep has no long-lived process per session.**
`session_start`, `prompt` and `stop` are separate short-lived invocations; there is nothing alive
to hold a lock. That constraint forces recorded liveness rather than inherent liveness.

`STALE_AFTER_SECONDS` governs *this file family only*. It is not a second opinion about Relay
presence — nothing here reads presence (D6) — so it does not recreate the multi-authority drift
that killed `_ACTIVE_PRESENCE_THRESHOLD` (`cortex/app/briefing/sections.py:28`, dead: its
declaration is its only reference repo-wide; delete it separately).

---

## 5. The notice

At most 3 entries, then `…and N more`. Appended to `session_start`'s existing return, which
already composes server text with client additions (`session_start.py:134`).

```
⚠  ANOTHER SESSION IS IN THIS WORKING TREE
   marat · feat/stage1-stranger-hands · seen 2m ago · pid 48213
   "wire the licence gate into auth middleware"
   You are on feat/stage0-install-unblock — you will overwrite each other.
   → isolate:  git worktree add ../fk-<branch> <branch>
```

The notice builder has its own `try/except` returning `""`. `never_raise`
(`client/firekeep_client/hooks/__init__.py:12`) returns `{}` for `session_start` — **no
systemMessage at all** — so an unguarded raise here would discard the entire briefing.

---

## 6. Degradation

| Condition | Behaviour |
|---|---|
| Not a git repo / `git` absent | no marker, no notice |
| `.git` read-only or permission-denied | write fails, swallowed, no notice |
| Marker unreadable or malformed JSON | that entry skipped, others still evaluated |
| Notice builder raises | `""` — **briefing still delivered** |
| Two worktrees of one repo | correctly not a collision |
| Concurrent writes | separate files; `os.replace` is atomic. No lock, no lost update |

---

## 7. Known limitations

**K1 — Cross-user cleanup.** Two OS users sharing a tree: A may lack permission to delete B's
file. *Detection* still works (reading suffices); cleanup falls back to the timestamp.

**K2 — Non-git working directories are invisible.** Accepted under D5.

**K3 — One-shot at session start.** A collision beginning mid-session does not reach the session
already running. Cheap future remedy: have `prompt` check siblings on refresh, since it is
already writing there every turn.

**K4 — A session killed hard on a *remote* host lingers** for `STALE_AFTER_SECONDS`. Same-host
kills are caught immediately by the PID check.

---

## 8. Deferred

**Divergence** (same project, same branch, different checkout) and **overlap** (same project,
different branch) require a second human running Firekeep, and cross-machine state, and therefore
a server registry. `docs/STRATEGY.md:55` already sequences "three teammates writing + recalling
daily for 60 days" ahead of work that needs teammates. Revisit when that is true; nothing here
forecloses it.

Also deferred: creating worktrees, blocking edits, containers.

---

## 9. Rejected alternatives

| Option | Why not |
|---|---|
| **Workspace fields on Relay presence** (rev 1–2) | Cross-machine false positives by construction; a digest crackable in 456 candidates; new kwargs break `relay_register` on un-upgraded servers (fastmcp 3.1.1 rejects unknown kwargs, verified with a passing control). |
| **Re-key presence by `agent_id`+`session_id`** | 12+ consumers across four services; `GET /presence/{agent_id}` would 404 permanently and flag every live session "crashed"; register and heartbeat write from two different id namespaces; `stop.py` never reads its payload. Fleet-breaking migration for one limitation. |
| **New Relay workspace registry** | A new subsystem, against `STRATEGY.md:67`. Its two server-requiring signals cannot fire without a teammate, and the one that can fire needs no server. |
| **Marker in the user's cache dir** | Misses every shared-tree case — NFS/SMB, WSL-over-host-path, devcontainer bind mounts — because each user reads their own cache. |
| **OS file lock** | Strictly better, but impossible: no long-lived per-session process to hold it (§4). |
| **Worktree or container per session** | The fix, not the detection. Detection is cheaper, reversible, and produces the evidence needed to judge whether the fix is worth building. |

---

## 10. Testing

Each positive case has a negative twin; without them the suite passes against an implementation
that warns unconditionally.

**Identity** — outside a git repo → no marker · linked worktree → its own git dir, not the main
repo's · `--absolute-git-dir` is absolute on Windows and POSIX.

**Detection** — a fresh sibling marker → **warns** · own filename → **never** warns · marker with
a dead PID on this host → **removed and does not warn** · marker with a live PID → warns · marker
from another hostname within the window → warns · same, beyond the window → does not warn ·
malformed JSON sibling → skipped, a valid sibling alongside it still warns.

**Robustness** — unwritable `.git` → no raise, no notice · notice builder raising → briefing still
returned · two sessions writing concurrently → two files, neither lost.

---

## 11. Changelog

**Revision 3** — mechanism replaced: Relay presence → a marker file in the working tree's git
directory. Removes the Relay change, the presence fields, the version-skew mitigation, the
machine salt, the `sha256` fingerprint, the `project_id` normalisation and the privacy section
(nothing is transmitted). L1 dissolves via one-file-per-session. Divergence and overlap deferred
until teammates exist. Gains PID-based liveness on the same host. No longer gated on the presence
lifecycle fix.

**Revision 2** — fixed a predicate that matched every session against itself; replaced a crackable
`sha256(hostname+path)` with a salted digest; resolved FastMCP skew empirically; corrected a false
`never_raise` safety claim and a wrong opt-out polarity. All superseded by revision 3's mechanism
change, except the `never_raise` finding (§5) and the notice/testing discipline, which carry over.
