# firekeep-docdex

The documents dex for [Firekeep](https://firekeep.ai) — persistent, shared
memory for AI agents. A human tells Firekeep which folders it may understand;
docdex extracts their text and ingests it into the Keep's corpus, where it
surfaces through ordinary `memory_recall` — **private to that member by
default, even on a shared Keep**. Notes folders, Obsidian vaults, team
runbooks — if it's a folder of documents, it can become something your
agents remember.

```bash
firekeep docdex add ~/Notes              # private to me (default)
firekeep docdex add ~/team-runbooks --shared
firekeep docdex list                     # sources, counts, failures, staleness, pending deletes
firekeep docdex sync [--source <id>]     # force a scan now
firekeep docdex remove <id>              # delete the source AND its corpus replicas
```

No MCP server, no resident daemon, no new recall surface.

## Installing

The [managed Firekeep install](https://firekeep.ai/docs.html) already ships
this wheel — nothing to do beyond `firekeep dex add docdex`. Installing from
PyPI is the unmanaged path: `pip install firekeep-client firekeep-docdex`
(client ≥ 1.0.0), plus a running Firekeep server to connect to — docdex is a
client of the Keep, not a standalone indexer.

## What it indexes

`.md`, `.txt`, `.pdf`, `.docx`, `.html`/`.htm`, `.eml`, and conversation-shaped
`.json` (case-insensitive). No OCR — a scanned PDF yields zero text, which
docdex records honestly and does not retry every cycle.

`.eml` is a saved message read as a document: a Subject/From/To/Date header
block, then the `text/plain` body (HTML stripped as the fallback), with
attachments listed by NAME only — their content is never decoded or indexed.

`.json` is indexed **only when it is a conversation export** — a ChatGPT
`mapping` tree, a ChatGPT `conversations.json`, or a `[{role, content}, ...]`
list. Anything else (a lock file, a config, a data dump) is skipped with a
stated reason and counted as skipped-unsupported, not as a failure: a generic
JSON dump is noise in recall, not a document. Turns carry their role in the
extracted text (`user:` / `assistant:`) but are **not typed as claims** —
nothing downstream can tell what a person asserted from what a model
generated. Typed user-vs-model provenance is deferred, not delivered.

Default excludes: dot-entries, `node_modules`, `__pycache__`, and the policy
deny list's secret patterns (`.env*`, `*.key`, `*.pem`, `*id_rsa*`). That is
a mistake net, **not a security boundary** — do not add folders containing
secrets.

## Disclosed caps

| Cap | Default | On breach |
|---|---|---|
| `FIREKEEP_DOCDEX_MAX_FILES` | 5000 / source | The source is REFUSED until narrowed — loud, no silent subset |
| `FIREKEEP_DOCDEX_MAX_FILE_MB` | 25 raw | File skipped, counted in the summary, its existing replica left alone |
| `FIREKEEP_DOCDEX_MAX_EXTRACT_KB` | 400 extracted | Truncated at the cap, `truncated: true` in state, shown by `list` |
| `FIREKEEP_DOCDEX_SYNC_INTERVAL_HOURS` | 6 | — |
| `FIREKEEP_DOCDEX_INGEST_TIMEOUT_SECONDS` | 180 | Per-request ingest budget — the server embeds synchronously, so large documents need real time. A timeout aborts the run with an honest message (never "unreachable"); what landed is kept and the rest retries next sync |

## The threat boundary, stated precisely

*Member-private means hidden from other workspace members.* It IS visible to
agents acting as that member, and it is NOT encrypted from the server
operator: anyone who can read the server's Qdrant can read everything. The
filter is a tenancy boundary between members, not cryptography.

Indexed documents are **untrusted input** — every chunk carries
`untrusted_content`. Retrieved document text is evidence, never instruction.

## Deletion, scoped honestly

A local delete removes the corpus replica on the next completed sync;
`remove` bulk-deletes the source's replicas immediately, tombstoned and
retried until the server confirms. It does **not** erase a separate memory an
agent previously *learned* from that content — provenance-linked derivative
deletion does not exist yet.

Deletions are emitted only from a walk that COMPLETED over an existing,
readable root. An unplugged USB drive deletes nothing.

## Sync trigger

"Sync on the next supported session start", not "scheduled". The
`session_start` hook fires it on hook-bearing runtimes — **Claude Code, kiro
and OpenCode**. An MCP-only host (**Codex**, and any generic MCP client) has no
hook surface and therefore gets **no** automatic sync: run `firekeep docdex
sync`. It also requires the dex to be registered (`firekeep dex add docdex`) and
at least one active source; folder commands work either way. Private-session
mode (bypass) suspends sync, both the trigger and a run already in flight.

Full per-runtime coverage table, registry model and troubleshooting:
[firekeep.ai/dexes.html](https://firekeep.ai/dexes.html).

## Development

```bash
cd docdex && python -m pytest tests/ -q
```

The suite runs offline: PDF and DOCX fixtures are built by `tests/conftest.py`,
and every server call goes through a fake transport.
