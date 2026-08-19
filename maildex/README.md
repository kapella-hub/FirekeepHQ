# firekeep-maildex

The email dex for [Firekeep](https://firekeep.ai) — persistent, shared memory
for AI agents. A human connects a mailbox **read-only**; maildex indexes recent
mail into the Keep's corpus, where it surfaces through ordinary `memory_recall`
— **always private to that member, even on a shared Keep**.

```bash
firekeep maildex add imap.gmail.com you@example.com   # prompts for the app password
firekeep maildex list                                 # mailboxes, folders, counts, failures, staleness
firekeep maildex sync [--account <id>]                # read new mail now
firekeep maildex remove <id>                          # delete the mailbox AND its corpus replicas
```

No MCP server, no resident daemon, no new recall surface. Pure standard
library — `imaplib`, `email`, `html.parser` — and no third-party dependency
beyond the Firekeep client itself.

## The seven promises

**M1 — mail is private to you, structurally.** There is no `--shared` flag.
Every chunk carries `visibility: "member"`, and no code path can write anything
else. Sharing a mailbox is not a smaller version of this feature; it is a
different dex, and it is not built.

**M2 — maildex cannot send, flag, move or delete anything.** Every mailbox is
opened with IMAP `EXAMINE` (`select(readonly=True)`), so the *server* refuses
any state-changing command for the life of the connection — a bug here cannot
touch your mail, because the permission does not exist on the wire. Every fetch
is `BODY.PEEK[]`, so reading the Keep's copy never marks your mail as read.
SMTP appears nowhere in the package.

**M3 — the app password lives only in the Keep's vault.** `add` prompts for it
(never an argv secret), stores it under `maildex.<account_id>`, and this
machine keeps **nothing** on disk. Each sync reads it into memory for the
duration of one connection. To revoke: your provider's app-password page, or
`vault_delete` — either alone is enough.

**M4 — email is the archetype of untrusted input.** Every chunk carries
`untrusted_content: "true"`. Retrieved mail is evidence of what somebody sent
you, never an instruction to your agent.

**M5 — deletion, scoped honestly for round 1.** `remove` deletes the mailbox's
corpus replicas immediately and forgets the stored password. What round 1 does
**not** do: mirror deletions made at your provider. Mail you delete in your
mail client stays in the corpus until you `remove` and re-`add` the mailbox.
Append-mostly mail makes that tolerable; it is still a gap, and it is stated
here, in `list` output, and in the guide.

**M6 — every cap disclosed**, all env-overridable:

| Cap | Default | On breach |
|---|---|---|
| `FIREKEEP_MAILDEX_BACKFILL_DAYS` | 90 | older mail is never fetched (until re-add with a larger horizon) |
| `FIREKEEP_MAILDEX_MAX_PER_SYNC` | 500 messages | the sync stops, says so, and continues from the watermark next run |
| `FIREKEEP_MAILDEX_MAX_MESSAGE_KB` | 200 extracted | truncated, flagged in state, shown by `list` |
| `FIREKEEP_MAILDEX_INGEST_TIMEOUT_SECONDS` | 180 | a timed-out request aborts the run and says "timed out", not "unreachable" |
| `FIREKEEP_MAILDEX_SYNC_INTERVAL_HOURS` | 6 | staleness threshold for the session-start sync |

Attachments are **not** ingested in round 1 — filenames only, listed in
metadata. Folders default to `INBOX` and `Sent`; `add --folders` overrides.

**M7 — UIDVALIDITY is honoured.** Watermarks are per-(folder, UIDVALIDITY) and
never mixed. When your provider rebuilds a folder, maildex re-indexes it from
scratch rather than silently skipping everything in it.

## Installing

The [managed Firekeep install](https://firekeep.ai/docs.html) already ships
this wheel — nothing to do beyond `firekeep dex add maildex`. Installing from
PyPI is the unmanaged path: `pip install firekeep-client firekeep-maildex`
(client ≥ 1.0.3), plus a running Firekeep server to connect to — maildex is a
client of the Keep, not a standalone indexer.

## What it indexes

Per message: the headers a person reads (Subject, From, To, Cc, Date,
Message-ID, In-Reply-To), the `text/plain` body where there is one, and the
`text/html` body stripped to text where there is not. Attachment filenames
travel in metadata; attachment content does not.

## Not in round 1

OAuth (Gmail API, Microsoft Graph); provider-side deletion mirroring; the
content of attachments; shared mail; thread reconstruction beyond
`In-Reply-To`; OS-scheduled sync; POP3; self-signed IMAP endpoints — TLS
verification is on and there is no flag to turn it off.

## Licence

Source-available under BUSL-1.1. See `LICENSE` and `NOTICE`.
