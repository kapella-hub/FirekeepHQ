# Docdex — the Documents dex (design, pre-registered 2026-08-15)

**Status: approved design, not yet built.** Client build is gated on dex
milestone 1 (the manifest registry — ROADMAP §5); the server half is
independent and may land in any cortex release. Second dex after Symdex,
and the first proof that Personal targets general use: no domain is
privileged, and documents are the first non-code one.

Decision trail: five decision-board answers (per-folder visibility default
private; scheduled + on-demand scan; md/txt/pdf/docx; delete-on-sync;
build as registry consumer #2), approach A (no MCP server — an ingest
client whose folder controls are human-only), and the dex naming decision
(ROADMAP §5: plugins are **dexes**; a dex names what it indexes, and
nothing gets the suffix unless it is genuinely an index — the day a
capability cannot honestly be called an index, it belongs to the
capability broker, not the dex family).

## 1. What it is

A human tells Firekeep which folders it may understand:

```
firekeep docdex add ~/Notes              # private to me (default)
firekeep docdex add ~/team-runbooks --shared
firekeep docdex list                     # sources, counts, failures, staleness
firekeep docdex sync [--source <id>]     # force a scan now
firekeep docdex remove <id>              # delete the source AND its chunks
```

A scheduled scan extracts text from supported files, ingests it into the
existing corpus (Qdrant chunks, Redis source metadata), and the content
surfaces through ordinary `memory_recall` in that member's sessions —
**private by default, even on a shared Keep**. There is no new recall
surface, no MCP server, and no resident daemon.

**Deliberately not an agent tool.** Folder selection is a privacy
decision, so the agent-callable version of it does not exist — that is
approach A's core argument, structural rather than policy. An agent that
shells out to `firekeep docdex add` is running a Bash command the hook and
runbook layer observes like any other. Agents meet Docdex only through
recall results.

## 2. Client shape

Own wheel **`firekeep-docdex`** (module `firekeep_docdex`, console script
`firekeep-docdex`), registered in the dex registry as consumer #2 with
manifest id `firekeep.docdex`, `kind: ingest-client`. Precision that
matters for milestone 1: an ingest-client has NO MCP server, so the
gateway mounts nothing for it — the registry entry drives lifecycle,
doctor and the hook-triggered sync, and `kind` is exactly the field that
tells the gateway "nothing to mount here". The first non-MCP dex is why
the manifest carries `kind` at all.
Like `client/` and `symdex/`, the wheel is deliberately NOT hash-locked
(`tests/test_requirements_lock.py` rule): it installs into a user venv and
carries its own extraction dependencies (`pypdf`, `python-docx`).

Modules, one job each:

- **`sources.py`** — the folder registry: `~/.firekeep/docdex/sources.json`
  (0600), entries `{id: <8-hex minted at add>, path, visibility:
  "member"|"workspace", added_at}`. Paths are stored absolute and
  expanded; a path that no longer exists is reported by `list`, never
  silently dropped.
- **`extract.py`** — per-format text extraction: `.md`/`.txt` (stdlib),
  `.pdf` (pypdf), `.docx` (python-docx), case-insensitive extensions.
  Per-file failures are recorded, never raised.
- **`scan.py`** — walk a source, apply excludes, content-hash (sha256 of
  raw bytes), diff against the source's state → change set
  `{new, changed, deleted}`. A rename is a delete + an add, stated plainly.
- **`state.py`** — per-source last-scan state at
  `~/.firekeep/docdex/state/<source_id>.json`:
  `{relpath: {hash, ingested_at, truncated, error}}`. Atomic
  write-replace, the client `state.py` pattern.
- **`sync.py`** — orchestration: for each new/changed file, extract and
  `POST /corpus/ingest`; for each deleted file,
  `DELETE /corpus/sources/{source_name}`. Per-file failures are collected
  and the sync continues; an unreachable server aborts the sync cleanly
  with state untouched, so the next scan retries everything unsynced.
  Prints an honest summary (ingested / deleted / failed / truncated /
  skipped-unsupported / skipped-oversize).
- **CLI** — the `firekeep docdex …` subcommands above, plus a row in
  `firekeep dex doctor` (sources, last sync, failure counts).

**Scheduled sync.** The `session_start` hook (the symdex auto-index
precedent) checks a last-sync stamp; if older than
`FIREKEEP_DOCDEX_SYNC_INTERVAL_HOURS` (default 6) it spawns a detached
`firekeep-docdex sync --all --quiet` guarded by a lock file so runs never
overlap. **Personal mode suspends this entirely** (I3): the hook gate that
already silences the sidecar's comms gates the spawn, and the sync command
itself re-checks the marker — nothing uploads while bypassed.

## 3. Wire contract

Everything rides the existing corpus API; one field is new (§4).

- **Ingest** — `POST /corpus/ingest` per file:
  - `source_name`: `docdex:<source_id>:<relpath>` (forward slashes;
    treated as an opaque key server-side, URL-encoded in the DELETE path).
    Stable per file, so a changed file re-ingests through the corpus's
    existing atomic staged swap and a mid-ingest failure leaves the old
    generation intact.
  - `source_type`: `"document"`.
  - `visibility`: `"member"` or `"workspace"`, from the source's setting.
  - `metadata`: `{path: <relpath>, mtime, dex: "firekeep.docdex"}`. Never
    the absolute path — a shared chunk must not leak another member's
    home-directory layout.
  - `workspace_id`/`member_id` are NOT sent by the client — they come from
    the verified principal at the server, the `test_ingest_tenancy.py`
    rule. A dex never asserts its own tenancy.
- **Delete** — `DELETE /corpus/sources/{source_name}` on local deletion
  (next sync) and for every source chunk on `docdex remove` (immediate).

**Disclosed caps** (env-overridable, values chosen and stated rather than
discovered by users):

| Cap | Default | On breach |
|---|---|---|
| `FIREKEEP_DOCDEX_MAX_FILES` | 5000 / source | Sync REFUSES the source until narrowed with excludes — loud, no silent subset (the `FIREKEEP_SYMDEX_MAX_FILES` precedent) |
| `FIREKEEP_DOCDEX_MAX_FILE_MB` | 25 raw | File skipped, counted in the summary |
| `FIREKEEP_DOCDEX_MAX_EXTRACT_KB` | 400 extracted | Truncated at the cap, `truncated: true` in state, shown by `list` — truncation is visible, never silent |
| `FIREKEEP_DOCDEX_SYNC_INTERVAL_HOURS` | 6 | — |

**Default excludes**: dot-directories and dotfiles, `node_modules`,
`__pycache__`, `.git`, and the secret patterns the policy deny list
already names (`.env*`, `*.key`, `*.pem`, `*id_rsa*`). Excludes are a
mistake net, not a security boundary — the honest statement is "do not
add folders containing secrets", and the docs say it.

## 4. The one server change — corpus visibility

`POST /corpus/ingest` gains optional **`visibility: "workspace" |
"member"`** (absent = `"workspace"`, so every existing chunk, caller and
test is byte-identically unaffected). Stored on chunk payloads next to the
`member_id` the pipeline already stamps.

**Recall applies it as a hard filter, not ranking.** A
`visibility="member"` chunk is returned only to sessions of the owning
`member_id`. Concretely, inside the existing hard `workspace_id` must:
`should[ visibility absent, visibility=="workspace",
(visibility=="member" AND member_id==<caller's verified member>) ]`.
Enforced in **both** recall paths — `POST /memory/recall` AND the SSE
streaming path. The streaming path's known ranking divergences are
tolerable; a privacy filter diverging is not. A caller with no member
identity gets no private chunks — privacy fails closed.

**Delete authorization**: a `visibility="member"` source is deletable by
its owning member or an `admin` key; workspace sources keep today's
behavior. `GET /corpus/sources` reports each source's visibility and
owner.

The flag is general corpus infrastructure — Docdex is merely its first
setter. Nothing about it is docdex-specific, and a future dex (maildex)
inherits it for free.

## 5. Invariants

- **I1 — A private chunk never appears in another member's recall.**
  Hard filter, both recall paths. *Because* "private continuity" is
  Personal's promise, and on Teams a member's `~/Documents` in a
  teammate's recall is the single failure that would end trust in the
  product. Tested measured-live style (the tenancy-trap methodology): the
  same query must rank the chunk top-N for the owner and return it not at
  all for a teammate.
- **I2 — Folder selection is human-only, structurally.** No MCP tool
  exists to add/remove/re-scope a folder; the capability is absent, not
  policy-gated. *Because* an agent talked into indexing `~/Private` is a
  prompt-injection away, and absent beats guarded.
- **I3 — Personal mode suspends sync.** Hook gate plus an in-command
  re-check. *Because* "fully bypassed, nothing logged" must include
  background uploads, or the promise is false.
- **I4 — Deletion is honored.** Local delete → source deleted next sync;
  `docdex remove` → chunks deleted immediately. *Because* for personal
  folders, retained ghosts of deleted files are surveillance, not
  continuity — the Keep keeps memories ABOUT your work, never copies you
  chose to remove.
- **I5 — Every cap and gap is disclosed.** The caps table, the excludes,
  no OCR (a scanned PDF yields nothing, and the scan summary SAYS so),
  scan-cadence staleness in `list` output. *Because* silent caps are the
  repo-wide banned failure.
- **I6 — Nothing here can touch a blocking hook path.** Sync is a
  detached background process or an explicit CLI call; the session_start
  spawn is fire-and-forget. *Because* a slow PDF must never cost an edit.

## 6. Testing

- **Client**: extraction fixtures per format (including a scanned-PDF
  fixture asserting the honest zero-yield summary); scan diff semantics
  (new/changed/deleted, rename = delete+add); state atomicity (kill
  mid-write, state intact); wire shapes pinned against a fake transport
  (source_name scheme, visibility field, metadata WITHOUT absolute
  paths); personal-mode skip (both gates); excludes; every cap's breach
  behavior (refuse / skip / truncate-and-mark).
- **Server**: the I1 measured-live pair on BOTH recall paths; absent
  `visibility` byte-identical to today (back-compat suite must pass with
  zero edits — the lister/matcher rule); delete authorization
  (owner / admin / other-member 403); no-member-identity fail-closed.
- **Docs guards**: config table entries for the new settings in the dex
  guide, `test_procedure_docs.py`-style derivation where applicable.

## 7. Out of scope for round 1, stated

OCR (tesseract is a system dependency; round 2 at earliest); live file
watching (scheduled scan is the decision — documents change slowly);
cloud sources (Drive/Dropbox are OAuth products and belong after the
capability broker); semantic dedupe; rename detection; formats beyond the
four; per-file sharing (visibility is per-SOURCE — per-file is complexity
without a requester); member-private visibility for non-corpus memory
types (the flag ships corpus-only).

## 8. Phases and sequencing

- **Phase V (server, independent — may land any time)**: the visibility
  flag, both recall filters, delete authorization, sources listing,
  tests. No client dependency; general infrastructure.
- **Phase D1 (client, after dex milestone 1)**: the `firekeep-docdex`
  wheel — registry entry, CLI, extract/scan/state/sync, tests. Consumer
  #2 is what proves the manifest abstraction, per the board decision.
- **Phase D2**: scheduled trigger, `dex doctor` row, docs (dex guide
  section + consistency-checklist sweep), and the first dogfood: this
  workstation's real notes folder, private, for a week before anything
  is said publicly — the runbook rollout pattern.

Change-consistency checklist impact (for the implementation plan):
`corpus/api.py` + `corpus/models.py` + `corpus/pipeline.py` + recall
handlers (Phase V); client adapters/cli, dex registry, docs guides,
dashboard corpus/sources view if it renders visibility (D1/D2).
