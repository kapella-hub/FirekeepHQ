# Maildex — the email dex, round 1 (design, 2026-08-19)

**Status: approved direction (owner decisions 2026-08-19: build now; IMAP
read-only with an app password in the vault; INBOX + Sent, 90-day backfill
then incremental). Build follows immediately.** Sequencing override and its
rationale recorded in ROADMAP §5 (what changed: the docdex chassis, I1
live-proven, the deletion lifecycle, `untrusted_content`, the vault; what
did not: OAuth custody still waits for the broker).

## 1. What it is

A human connects a mailbox read-only; Maildex indexes recent mail into the
corpus, where it surfaces through ordinary recall — **always private to that
member**. Registry consumer #3, `kind: ingest-client` on the docdex chassis:
no MCP server, no resident daemon, no new recall surface.

```
firekeep maildex add imap.gmail.com you@example.com   # prompts for the app password
firekeep maildex list                                  # accounts, folders, counts, failures, staleness
firekeep maildex sync [--account <id>]                 # force a sync now
firekeep maildex remove <id>                           # delete the account AND its corpus replicas
```

## 2. The invariant set (M1–M7)

- **M1 — Mail is member-private, structurally.** No `--shared` flag exists.
  Every chunk carries `visibility: "member"`; there is no code path that
  writes anything else. Sharing mail is not a smaller feature of this dex —
  it is a different dex, not designed.
- **M2 — No send or mutate capability exists anywhere in the wheel.** Every
  mailbox open is `select(readonly=True)` — IMAP `EXAMINE`, enforced by the
  SERVER, not by our discipline: even a bug cannot flag, move, or delete a
  message, and SMTP appears nowhere. The docdex structural answer applied to
  the send/read ambiguity.
- **M3 — The app password lives only in the Keep's vault.** `maildex add`
  prompts (never an argv secret), stores via `vault_store` under
  `maildex/<account_id>`, and the client keeps NOTHING on disk — each sync
  `vault_retrieve`s it into memory for the duration of the connection.
  Revocation is the provider's app-password page or `vault_delete`, either
  alone suffices.
- **M4 — Email is the archetype of untrusted input.** Every chunk carries
  `untrusted_content: true`; retrieved mail is evidence, never instruction.
  A prompt-injection payload in a message becomes exactly as inert as a
  poisoned document — no more, and stated no less.
- **M5 — Deletion, scoped honestly for round 1.** `maildex remove` bulk-
  deletes the account's replicas (`maildex:<account_id>` via the dex-sources
  route) immediately, tombstoned and retried until confirmed. What round 1
  does NOT do, disclosed everywhere the feature is described: mirror
  provider-side deletions — mail expunged at the provider stays in the
  corpus until `remove`/re-`add` (the manual rebuild) or round 2's expunge
  sync. Append-mostly mail makes this tolerable; it is still a gap, and it
  is said.
- **M6 — Every cap disclosed** (env-overridable):

  | Cap | Default | On breach |
  |---|---|---|
  | `FIREKEEP_MAILDEX_BACKFILL_DAYS` | 90 | older mail never fetched (until re-add with a larger horizon) |
  | `FIREKEEP_MAILDEX_MAX_PER_SYNC` | 500 messages | sync stops, says so, continues next run from the watermark |
  | `FIREKEEP_MAILDEX_MAX_MESSAGE_KB` | 200 extracted | truncated + flagged in state, shown by `list` |
  | `FIREKEEP_MAILDEX_INGEST_TIMEOUT_SECONDS` | 180 | the docdex timeout semantics, verbatim (timed-out ≠ unreachable) |
  | `FIREKEEP_MAILDEX_SYNC_INTERVAL_HOURS` | 6 | staleness threshold for the session-start trigger |

  Attachments are NOT ingested in round 1 — filenames only, listed in
  metadata. Folders default to INBOX + Sent; `add --folders` overrides.
- **M7 — UIDVALIDITY is honored.** A folder whose UIDVALIDITY changes is
  re-baselined from scratch; watermarks are per-(folder, uidvalidity) and
  never mixed. A provider-side rebuild must never cause silent gaps or
  duplicate floods (source_name dedupes the latter by construction).

## 3. Mechanics

- **Wheel `firekeep-maildex`** (module `firekeep_maildex`), **pure stdlib**:
  `imaplib`, `email` (policy.default), an `html.parser` text-stripper. Zero
  third-party dependencies — deliberately lighter than docdex.
- **accounts.py** — `~/.firekeep/maildex/accounts.json` (0600): `{id:
  hex128, host, port(993), username, folders, backfill_days, added_at}`.
  No secrets in this file, ever.
- **imapio.py** — the EXAMINE-only session wrapper; `IMAP4_SSL` with the
  stdlib default context; all fetches `BODY.PEEK[]` (PEEK: even the \Seen
  flag is never set — reading the Keep's copy must not mark the human's
  mail read).
- **parse.py** — headers (From, To, Cc, Date, Subject, Message-ID,
  In-Reply-To), body text/plain preferred, text/html stripped otherwise;
  per-message failures recorded, never raised.
- **state.py** — `~/.firekeep/maildex/state/<account_id>.json`, atomic:
  per folder `{uidvalidity, last_uid}` + per message `{uid, ingested_at,
  truncated, error}`. The docdex seen/ingested retry split applies to
  ingest failures.
- **sync.py** — per-account lock (docdex's lock/stale semantics); initial
  backfill `UID SEARCH SINCE <90d>`, then `UID <last+1>:*`;
  `resolver.is_bypassed()` re-checked per batch (I3); honest summary;
  `_ServerLost`-style timed-out vs unreachable aborts, verbatim semantics
  from docdex's fix.
- **wire.py** — `source_name: maildex:<account_id>:<sha256(folder |
  uidvalidity | uid | message_id)>`; `source_type: "email"`; `visibility:
  "member"` (M1); `metadata: {folder, subject, from, date, message_id,
  attachments: [names], dex: "firekeep.maildex", untrusted_content:
  "true"}` — string values, no absolute anything, tenancy server-stamped.
- **Trigger** — `maildexsync.py`, the docdexsync twin: registered +
  ≥1 account + stale → detached `python -m firekeep_maildex.sync --all
  --quiet` on session start; same interval-bucket claim, same honest
  "sync on the next supported session start" naming and coverage table.
- **Server (small, mirrored from docdex):** `maildex` joins
  `KNOWN_DEX_IDS` → `dex:maildex` scope (the enrolled-member ceiling grants
  it to every member automatically — the v1.0.0 semantics working as
  designed); the `maildex:` source prefix reserved under the same
  `require_dex_scope` rules; `source_type` pattern gains `"email"`; the
  bulk dex-sources delete route verified generic (generalized if it
  hardcodes docdex). Existing corpus suite passes with zero edits.

## 4. Testing

Unit (fake `imaplib` connection object — the suite runs offline): EXAMINE-
only asserted (a test that FAILS if any mutating IMAP verb is ever called —
grep-level guard plus a spy connection that raises on APPEND/STORE/EXPUNGE);
PEEK asserted; backfill vs incremental UID math; UIDVALIDITY re-baseline;
caps per-breach; parse fixtures (plain, html, multipart, broken MIME,
oversize); wire shapes byte-exact incl. `visibility: "member"` on every
payload; vault_retrieve called per sync and the password never touching
disk (spy on open/write during sync); bypass suspension; lock races;
remove → bulk delete → tombstone retry. Server: scope/prefix/type tests
mirroring docdex's, drift guard already ties KNOWN_DEX_IDS to SCOPES.

Live e2e before release (the house rule): a throwaway Dovecot container on
the VPS (localhost-only port), a seeded test mailbox (APPEND from the seed
script — the seeding tool may write; the wheel may not), then the real
`firekeep maildex add → sync → recall` from the workstation, the I1
two-member probe against mail content, `remove` bulk-delete proof, and a
provider sanity pass against a real mailbox as the owner's dogfood.

## 5. Out of scope for round 1, stated

OAuth (Gmail API / MS Graph) — post-broker; provider-side deletion
mirroring (expunge sync — round 2, disclosed per M5); attachments' content;
shared mail (not a smaller feature — M1); threading reconstruction beyond
In-Reply-To metadata; OS-scheduler sync; POP3; self-signed IMAP endpoints
(stdlib default TLS verification only, no insecure flag).

## 6. Docs & release

`docs/guides/dexes.md` gains the Maildex section (invariants M1–M7, caps,
coverage, the M5 disclosure); CLAUDE.md service table row; client-kit
paragraph; bootstrap/release wiring identical to docdex's (4th wheel:
fetch+verify, combined single-resolution install, make_release guard,
release.yml build/copy/pypi-maildex leg, not-locked list, license
exclusion). Client rides `client-v1.1.0` (a new dex is a minor, not a
patch); server rides `v1.1.0`. Site: dexes page Maildex section after
release only, with M5 stated as plainly there as here.
