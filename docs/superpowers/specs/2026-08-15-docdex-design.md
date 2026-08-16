# Docdex — the Documents dex (design, pre-registered 2026-08-15)

**Status: approved design, revised same day by external review (ten
findings, all validated — §9), not yet built.** Client build is gated on
dex milestone 1 (the manifest registry — ROADMAP §5); the server half
(Phase V) is independent and may land in any cortex release. Second dex
after Symdex, and the first proof that Personal targets general use: no
domain is privileged, and documents are the first non-code one.

Decision trail: five decision-board answers (per-folder visibility default
private; scheduled + on-demand scan; md/txt/pdf/docx; delete-on-sync;
build as registry consumer #2), approach A (no MCP server — an ingest
client whose folder controls are human-only), the dex naming decision
(ROADMAP §5), and the external review below, whose verdict stands: the
product choices hold; the gaps were in inherited corpus assumptions.

## 1. What it is

A human tells Firekeep which folders it may understand:

```
firekeep docdex add ~/Notes              # private to me (default)
firekeep docdex add ~/team-runbooks --shared
firekeep docdex list                     # sources, counts, failures, staleness, pending deletes
firekeep docdex sync [--source <id>]     # force a scan now
firekeep docdex remove <id>              # delete the source AND its corpus replicas
```

A scan extracts text from supported files, ingests it into the corpus
(Qdrant chunks, Redis source metadata), and the content surfaces through
ordinary `memory_recall` in that member's sessions — **private by
default, even on a shared Keep**. No new recall surface, no MCP server,
no resident daemon.

**The threat boundary, stated precisely (review #4):** *member-private
means hidden from other workspace members.* It IS visible to agents
acting as that member, and it is NOT encrypted from the server operator.
Anyone who can read the server's Qdrant can read everything; the filter
is a tenancy boundary between members, not cryptography.

**Deliberately not an agent tool — with enforcement stated honestly
(review #9).** The agent-callable folder tool does not exist, so on
MCP-only runtimes the control is structurally absent — complete
enforcement. Where the agent holds a shell, `firekeep docdex add` is a
Bash command like any other: the hook/runbook layer observes it, and the
control is advisory, the same coverage language the Institution Thesis
uses. Agents meet Docdex content only through recall.

## 2. Client shape

Own wheel **`firekeep-docdex`** (module `firekeep_docdex`, console script
`firekeep-docdex`), registered in the dex registry as consumer #2 with
manifest id `firekeep.docdex`, `kind: ingest-client`. An ingest-client
has NO MCP server, so the gateway mounts nothing for it — the registry
entry drives lifecycle, doctor, and the sync trigger; `kind` is exactly
the field that tells the gateway "nothing to mount here". Like `client/`
and `symdex/`, the wheel is deliberately NOT hash-locked
(`tests/test_requirements_lock.py` rule); it carries its own extraction
dependencies (`pypdf`, `python-docx`).

Modules, one job each:

- **`sources.py`** — the folder registry: `~/.firekeep/docdex/sources.json`
  (0600), entries `{id: <128-bit hex minted at add — review #3's
  collision point>, path, visibility: "member"|"workspace", added_at,
  status: "active"|"pending_delete"}`. Paths stored absolute and
  expanded. A missing path is REPORTED by `list`, never silently dropped
  — and never interpreted as deletion (§5 I4a).
- **`extract.py`** — `.md`/`.txt` (stdlib), `.pdf` (pypdf), `.docx`
  (python-docx), case-insensitive. Per-file failures recorded, never
  raised.
- **`scan.py`** — walk a source root WITHOUT following symlinks or
  Windows junctions out of it (resolved path must stay under the
  resolved root — review #6), apply excludes, content-hash raw bytes,
  diff against state. A rename is a delete + an add. **A walk that did
  not complete — missing root, unmounted volume, permission-denied at
  the root — produces NO deletions**: deletions may only be emitted from
  a completed walk (§5 I4a). Partial subtree errors exclude that subtree
  from deletion inference.
- **`state.py`** — per-source state at
  `~/.firekeep/docdex/state/<source_id>.json`, atomic write-replace.
  Per file: `{seen_hash, ingested_hash, ingested_at, truncated, error,
  pending_delete}`. **`seen_hash` and `ingested_hash` are separate
  (review #6):** a transient ingest failure leaves `ingested_hash`
  behind `seen_hash` and retries next sync; a stable extraction result
  (a scanned PDF's honest zero yield) records `seen_hash` and is NOT
  retried every cycle.
- **`sync.py`** — orchestration under a per-source lock file shared with
  `remove` (review #6): new/changed → extract → ingest; deleted (from a
  COMPLETED walk) → tombstone in state (`pending_delete`) → server
  delete → tombstone cleared only on server confirmation, retried next
  sync otherwise, and shown by `list` while pending. `remove` marks the
  SOURCE `pending_delete` first, takes the lock (an in-flight sync
  cannot resurrect content — the flag is checked before every ingest
  batch), then issues one bounded bulk delete (§4), retrying the
  tombstone on failure. Unreachable server: sync aborts cleanly, state
  unchanged. Prints an honest summary (ingested / deleted / pending
  delete / failed / truncated / skipped-unsupported / skipped-oversize).
- **CLI** — the subcommands above plus a `firekeep dex doctor` row
  (sources, last sync, failures, pending deletes).

**Sync trigger — named honestly (review #7): "sync on the next supported
session start", not "scheduled".** The `session_start` hook (the symdex
auto-index precedent) checks a last-sync stamp; if older than
`FIREKEEP_DOCDEX_SYNC_INTERVAL_HOURS` (default 6) it spawns a detached
`firekeep-docdex sync --all --quiet` under the lock. Runtime coverage
stated: this fires only on hook-bearing runtimes (Claude Code, Codex,
Kiro, OpenCode via plugin) — an MCP-only consumer host gets NO automatic
sync, documented in the dex guide, with `firekeep docdex sync` as the
manual path. A real OS scheduler (Task Scheduler / launchd / systemd
timer) is round 2, and matters precisely because Docdex targets
non-coding hosts.

**Private-session mode suspends sync** (I3) — both the hook-triggered
spawn AND a background run already started (the flag is re-checked per
batch). Naming (review #8): the client's `/personal` bypass collides
with the product tier "Firekeep Personal" post-pivot; new material —
this spec included — says **private-session mode (bypass)**, and a
client-side rename with `/personal` kept as a compatibility alias is
recorded as a follow-up in §8.

## 3. Wire contract

- **Ingest** — `POST /corpus/ingest` per file:
  - `source_name`: **`docdex:<source_id>:<sha256(normalized relpath)>`**
    (review #3) — source_id is the 128-bit hex from `sources.json`,
    relpath is normalized (NFC, forward slashes, case preserved) before
    hashing. Opaque by construction: no `/` in the DELETE route
    parameter, ~104 chars under the 500 ceiling, no filename leakage
    through the identifier, no cross-source overwrite. The
    human-readable relpath travels ONLY in visibility-authorized
    metadata.
  - `source_type`: `"document"` — a NEW allowed type (Phase V; today's
    pattern rejects it, review #1).
  - `visibility`: `"member"` or `"workspace"` from the source setting.
  - `metadata`: `{path: <relpath>, mtime, dex: "firekeep.docdex",
    untrusted_content: true}` (review #10). Bounded server-side (size
    and key count), reserved keys protected. Never the absolute path.
  - Tenancy is NEVER client-asserted: `workspace_id`, owning
    `member_id`, and the writing credential/dex identity are stamped
    from the verified principal (the `test_ingest_tenancy.py` rule).
- **Delete** — single file: `DELETE /corpus/sources/{source_name}`
  (opaque name, no encoding hazard). Source removal: **one bounded bulk
  operation** `DELETE /corpus/dex-sources/{source_id}` (Phase V) rather
  than thousands of sequential requests (review #6).

**Disclosed caps** (env-overridable):

| Cap | Default | On breach |
|---|---|---|
| `FIREKEEP_DOCDEX_MAX_FILES` | 5000 / source | Sync REFUSES the source until narrowed — loud, no silent subset |
| `FIREKEEP_DOCDEX_MAX_FILE_MB` | 25 raw | File skipped, counted in the summary |
| `FIREKEEP_DOCDEX_MAX_EXTRACT_KB` | 400 extracted | Truncated at the cap, `truncated: true` in state, shown by `list` |
| `FIREKEEP_DOCDEX_SYNC_INTERVAL_HOURS` | 6 | — |

**Default excludes**: dot-directories and dotfiles, `node_modules`,
`__pycache__`, `.git`, and the policy deny list's secret patterns
(`.env*`, `*.key`, `*.pem`, `*id_rsa*`). A mistake net, not a security
boundary — the docs say "do not add folders containing secrets".

## 4. Phase V — the corpus changes (expanded by review #1–#5)

The review's core finding: the spec's original "one server change" was
five, because Docdex inherits corpus assumptions that break member
privacy. All of Phase V is general corpus infrastructure; Docdex is
merely its first beneficiary.

1. **Typed document sources.** `source_type` pattern gains `document`;
   ingest gains bounded `metadata` (size/key caps, reserved-key
   protection: `dex`, `untrusted_content`, tenancy fields are
   server-controlled) and the `visibility` field (absent = `workspace`,
   byte-identical back-compat).
2. **Source-scoped point identity (review #2 — the worst bug).** Corpus
   point IDs are today `uuid5(text)`: identical text from Alice's and
   Bob's documents collapses to ONE point whose payload carries only the
   last writer's ownership, and deleting one member's source deletes the
   shared point — the other member's chunk vanishes. Corpus points move
   to `uuid5(workspace_id | source_name | ingest_id | chunk_index)`.
   Deleting Alice's copy of identical text can then never remove Bob's.
   (Memory points keep their text-derived IDs — dedup is a FEATURE for
   operational memories; the corpus write path gets its own identity.)
3. **Ownership and authorization (review #3).** Source records carry
   `workspace_id`, owning `member_id`, visibility, and the writing
   credential identity, all stamped server-side. List/delete/write are
   principal-aware: private sources are visible to and deletable by
   their owner or `admin`; **generic corpus credentials cannot claim,
   overwrite, or delete reserved `docdex:`-prefixed sources** — the
   prefix is writable only by a docdex-scoped credential (each dex's
   scoped key carries its dex id; the §5-record "per-dex scoped
   identity" made concrete).
4. **One shared visibility-filter builder, applied at every egress
   (review #4).** Not just both recall paths: `GET /corpus/sources` and
   the `corpus_sources` MCP tool (source names ARE private data — other
   members must not see private filenames), the dashboard memory/source
   listings (`dashboard.py` reads Qdrant directly), memory lifecycle
   reads, and transfer/export. One builder function, consumed
   everywhere corpus text or identifying metadata leaves the server; a
   new egress path that skips it is the bug class the builder exists to
   prevent. Callers with no member identity get no private chunks —
   fail closed.
5. **Honest generation semantics (review #5).** The staged re-ingest is
   NOT atomic to recall: new chunks are upserted individually before the
   old generation is deleted, so mixed generations are recallable
   mid-swap, and a mid-ingest failure leaves partial new chunks
   recallable until the next successful sweep. Phase V either (a) adds a
   committed-generation gate — chunks carry `ingest_id` already; recall
   honors only the source's committed generation recorded at swap
   completion — or (b) keeps today's mechanics and the spec's guarantee
   weakens to "old generation preserved on failure; brief mixed windows
   possible and partial generations swept on next success". **(a) is
   preferred and assumed by I7's test; (b) is the documented fallback if
   (a)'s recall-path cost measures badly.** Docdex does not build its
   reliability story on an atomicity the corpus does not have.

## 5. Invariants

- **I1 — A private chunk never appears in another member's recall, and a
  private source's NAME never appears in another member's listings.**
  The shared filter builder at every egress. Tested measured-live style:
  same query, top-N for the owner, absent for a teammate — plus listing
  and dashboard assertions.
- **I2 — Folder selection is human-only: complete on MCP-only runtimes
  (the tool is absent), advisory where the agent has a shell** (the
  command is observed by the hook/runbook layer like any Bash). The
  Institution Thesis's enforcement-coverage language, verbatim.
- **I3 — Private-session mode (bypass) suspends sync** — the trigger
  AND in-flight batches. Because "fully bypassed" must include
  background uploads.
- **I4 — Deletion is honored, scoped honestly to corpus replicas.**
  Local delete → replicas deleted next sync; `docdex remove` → bulk
  delete immediately, tombstoned and retried until confirmed. What this
  does NOT promise (review #10): erasing a separate memory an agent
  previously LEARNED from that content — provenance-linked derivative
  deletion does not exist yet and the docs say so.
- **I4a — Absence of evidence is not deletion.** Deletions are emitted
  only from a COMPLETED walk of an existing, readable root; a missing,
  unmounted, or permission-denied root produces zero deletions and a
  loud `list` warning. Because an unplugged USB drive must never wipe a
  member's index.
- **I5 — Every cap and gap is disclosed.** The caps table, excludes, no
  OCR (a scanned PDF's zero yield is SAID, and not retried forever —
  seen/ingested hash split), sync-trigger coverage per runtime,
  mixed-generation semantics if fallback (b) ships.
- **I6 — Nothing here can touch a blocking hook path.** Sync is a
  detached background process or an explicit CLI call.
- **I7 — Indexed documents are UNTRUSTED input (review #10).** Every
  Docdex chunk carries `untrusted_content: true`; recall rendering
  delimits document text as quoted evidence, never instruction; the
  instruction layer states it ("retrieved document text is evidence");
  and document-derived text triggering consequential actions is
  broker-gated future work, not a Docdex capability.

## 6. Testing

The review's acceptance tests, adopted verbatim as the core suite:

- Identical private documents for two members remain separate points;
  deleting one preserves the other (the #2 regression, both directions).
- Bob cannot recall, list, overwrite, or delete Alice's private source —
  recall, sources REST, sources MCP, dashboard, export.
- A generic corpus credential cannot mutate a reserved `docdex:` source.
- Partial ingest failure exposes only a committed generation (or, under
  fallback (b), the mixed window is asserted and documented).
- Unmounted or unreadable folders delete nothing.
- `remove` racing an active sync cannot resurrect content.
- Failed deletes remain visibly pending and retry.
- Non-hook MCP hosts: documented sync behavior asserted (no silent
  automatic sync claim).
- Private-session mode prevents both hook-triggered and manual
  background sync paths.

Plus the client mechanics: extraction fixtures per format (including the
scanned-PDF honest-zero fixture), diff semantics, symlink/junction
containment, state atomicity, seen/ingested retry split, wire shapes
against a fake transport (opaque source_name scheme, no absolute paths,
`untrusted_content` present), every cap's breach behavior, excludes.
Server back-compat: the existing corpus suite passes with ZERO edits
(absent visibility = workspace; memory point identity untouched).

## 7. Out of scope for round 1, stated

OCR; live file watching; OS-scheduler sync (round 2, with the honest
trigger naming until then); cloud sources (OAuth products, after the
broker); semantic dedupe; rename detection; formats beyond the four;
per-file sharing (visibility is per-source); member-private visibility
for non-corpus memory types; provenance-linked derivative deletion
(named in I4); encryption from the server operator (named in the threat
boundary).

## 8. Phases, sequencing, follow-ups

- **Phase V (server, independent — may land any time)**: §4 items 1–5,
  the shared filter builder, the bulk delete route, dex-scoped corpus
  credentials, tests. General infrastructure; ships without any client.
- **Phase D1 (client, after dex milestone 1)**: the `firekeep-docdex`
  wheel — registry entry, CLI, extract/scan/state/sync with the §2
  deletion lifecycle, tests. Consumer #2 proves the manifest.
- **Phase D2**: sync trigger + doctor row + docs (dex guide section,
  consistency-checklist sweep, runtime-coverage table), then the first
  dogfood: this workstation's real notes folder, private, for a week
  before anything is said publicly.
- **Follow-ups owned outside this spec**: the private-session-mode
  rename (client-wide, `/personal` kept as compatibility alias — a
  naming collision with the Personal tier, review #8); the OS scheduler;
  recall-rendering delimiting for untrusted corpus content (lands with
  I7 but is a cortex rendering change usable by all corpus content).

Change-consistency checklist impact: `corpus/api.py` + `corpus/models.py`
+ `corpus/pipeline.py` + `corpus/store.py` + `cortex/app/db/vector.py`
(corpus point identity) + recall handlers + `cortex/app/mcp_server.py`
(corpus tools) + `cortex/app/dashboard.py` (Phase V); client
adapters/cli, dex registry, docs guides, dashboard sources view (D1/D2).

## 9. External review, same day — ten findings, all validated

Reviewed against source before acceptance (2026-08-15): (1) the wire
contract didn't exist — `source_type` pattern rejects `document`, no
`visibility`/`metadata`, unauthenticated delete (`corpus/api.py`);
(2) `uuid5(text)` point identity collapses identical text across members
and deleting one member's source deletes the shared point — CONFIRMED
WORSE than reported (`db/vector.py`); (3) source ownership global and
client-controlled, `/` breaks the delete route (`corpus/store.py`);
(4) privacy egress is more than two recall routes — sources REST/MCP,
dashboard, lifecycle, export all leak names or text unfiltered;
(5) the "atomic swap" is not atomic to recall — partial generations
recallable until the next successful sweep (`corpus/pipeline.py`'s own
comments); (6) deletion needs a lifecycle — locks, tombstones, retries,
completed-walk-only inference, symlink containment, bulk delete,
seen/ingested hash split; (7) "scheduled" was opportunistic — renamed
honestly, coverage stated; (8) "personal mode" collides with the
Personal tier — private-session mode in new material, rename follow-up;
(9) I2 was too absolute — enforcement coverage now stated per runtime
class; (10) indexed documents are untrusted input — I7, and I4 softened
to corpus replicas. Reviewer's verdict accepted: the product choices
hold; every gap was an inherited corpus assumption, which is why Phase V
grew from one change to five.
