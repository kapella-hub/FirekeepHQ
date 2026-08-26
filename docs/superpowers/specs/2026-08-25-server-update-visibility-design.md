# Server update visibility — design

**Status:** approved in brainstorming; revised same day after adversarial
review (5-agent verification against the code; 3 major findings absorbed).
Not yet implemented.
**Date:** 2026-08-25
**Scope:** the "separate spec" the field-failure-reporting spec's scope note
promised: detect when a deployed Keep is behind the latest published server
release, and tell the operator. Detect-and-tell only — this spec performs no
update, ever.

## Problem

A customer's Keep can sit on an old server version indefinitely with nothing
telling anyone. The pieces around the gap all exist:

- `server-release.yml` publishes, on every `vX.Y.Z` tag, the four service
  images **and** a manifest the dist site serves at
  `server/latest/server.json` (`{version, file, sha256}`), with
  backwards-motion protection (`is_newer_release` keeps `latest` from moving
  backwards; a hotfix on an old line publishes versioned-only with
  `publish_latest=0`). `firekeep init --version` / `update.sh --to` already
  consume it.
- Every published image bakes `APP_VERSION` (the release tag) and answers
  `GET /version` with it; source checkouts bake a git-describe string
  (observed live: `v1.2.1-67-g040d0ed`).
- Doctor's `versions` row already reads cortex's version — and **deliberately
  does not judge it** (its docstring records why: the old `version-skew` row
  compared server tags against *client* tags, two independent series where
  equality is meaningless, so every correct install warned and the row taught
  readers to skip the report).

What is missing is exactly one comparison — the running server's version
against `server/latest/server.json`, the *right* peer this time — and two
sentences telling the operator about it.

## Decisions, and why

**1. Detection is client-side.** The client already talks to both ends: it
reads cortex `/version` today, and it fetches the same dist base daily for its
own update check. Doing the comparison in the client retrofits detection onto
**every existing deployment the moment the client autoupdates** — a
server-side detector could never tell anyone about servers too old to contain
it, and the client is where every surface the operator already reads lives.
The server-side leg (a Sentinel event, a dashboard card) is a named follow-up,
not part of this spec.

**2. Only clean `vX.Y.Z` running versions are judged.** A git-describe suffix
means "built from source" — those operators update by `git pull`, the
comparison against a bundle manifest is not meaningful, and nagging them would
be noise. The comparator strips the `v` and reuses `updater.parse_version` /
`updater.is_newer` — which **raise `UpdateError` on anything malformed rather
than returning False**, so the comparison sits in a `try/except UpdateError`,
never a truthiness check, and never a new version parser. (The server-side
`is_newer_release` accepts prerelease tags; the client comparator does not —
irrelevant in practice since the release workflow only fires on clean tags,
and a hand-published prerelease manifest lands in the unjudged path.)

**3. Everyone is told, on both existing surfaces.** A doctor row and a
once-daily-shown briefing line. This was weighed against doctor-only
(operators who never run doctor never learn — the same silence this spec
exists to end) and provisioning-machine-only (breaks when the provisioning
machine is not the operator's daily driver). One line per day, team-wide, is
the accepted cost — **bounded by decision 6's acknowledgment key**, because
"until the operator updates" is not always the end state.

**4. Never auto-applied — a hard invariant, not a default.** The client
auto-updates itself because a client venv swap is reversible and per-machine.
A server update can carry an **irreversible Neo4j store-format migration**
(the image-pinning guide's core lesson). The tell always routes through
`bash update.sh --to vY`, which takes a volume backup by default before
touching anything. No flag, config, or future convenience may make this spec
apply an update. Decision 6's suppression key silences the *telling*; nothing
ever automates the *doing*.

**5. Cache the manifest fetch, never the verdict.** The first draft cached a
composed verdict for the day; review killed it with the concrete scenario: an
operator sees the warn, runs `update.sh --to v1.3.0`, re-runs doctor to
confirm — and doctor repeats the stale warn until midnight, lying at the
exact moment it is consulted. `_update_nudge` is the correct precedent
*precisely* read: it day-caches only the **external** half (the dist manifest,
`today|latest`, negatives included — an unreachable dist host costs one 3s
timeout per day) and compares against the **live** local version at render
time, self-correcting instantly. This spec does the same: the
`server_update_check` scratch key caches only the manifest fetch; cortex
`/version` is read live on every render (doctor already makes several cortex
calls per run; the hook already fetches `/briefing` from cortex every
session — one more LAN GET with a 3s timeout is noise). A cortex outage
therefore suppresses at most one render, never the rest of the day.

**6. A per-version acknowledgment, because pinning is a supported choice.**
The release workflow explicitly supports maintaining an old line
(`publish_latest=0` hotfixes), and decision 4's own rationale — irreversible
migrations — is exactly why a team might deliberately stay on 1.2.x. Without
an off-switch the warn becomes *permanently wrong* for them, which re-teaches
readers to skip the report — the precise failure the old `version-skew` row
was removed for. So: `[dist] server_update_ack = v1.3.0` acknowledges that
specific latest — doctor drops to an ok row that still states the fact
(`server v1.2.0 (v1.3.0 available, acknowledged)`), the briefing line stops.
**The ack names a version, so it re-arms automatically when a newer latest
appears** — it can never rot into permanent silence. It is per-machine (the
config is per-machine); a team silences it per machine or the operator
updates. A boolean kill-switch was rejected: it outlives its reason.

## Non-goals

- Performing or scheduling server updates (decision 4).
- The server-side leg (Sentinel collector, dashboard card). Follow-up.
- Release notes. The manifest carries none; the tell names the version and
  the command.
- Multi-service skew analysis. Cortex is the version authority, as today.
- Judging source checkouts or prerelease manifests (decision 2).
- Touching `_check_versions` or `firekeep version`. The `versions` row stays
  exactly as it is: it is the verdict-free client+cortex *report*, it is the
  body of `cmd_version` (cli.py:82), it is the only version display on
  checkout-installed clients (where `client-version` and the new row both
  go silent), and its "cortex reported no version" warn catches
  pre-`APP_VERSION` servers. The invariant is narrowed accordingly: **only
  the new `server-version` row ever JUDGES the server version** — reporting
  and judging are different rows with different jobs, and doctor may show
  both.

## Detection — `client/firekeep_client/serverupdate.py`

One new module:

```python
def check(cfg) -> ServerUpdateStatus | None:
    """Compare the running Keep against server/latest/server.json.

    Best-effort, never raises, never blocks beyond two 3s timeouts.
    Cortex /version is read LIVE on every call (decision 5); only the
    manifest fetch is day-cached (scratch key "server_update_check",
    value "today|<version-or-empty>", negatives cached).
    Returns None only when cortex /version did not answer — with no
    running version there is nothing to say on any surface.
    """
```

`ServerUpdateStatus` carries `running` (the raw string), `latest`
(`None` when no `[dist]` section, unfetchable, or malformed), and
`relation: "behind" | "current" | "ahead" | "unjudged"` — three-way plus
unjudged, because `behind=False` must not conflate *equal* with *ahead*
(decision on rendering below). `ack: bool` reflects whether `[dist]
server_update_ack` matches `latest`.

Fetches: cortex `/version` via `resolver.resolve("cortex", cfg=cfg)` with
`headers=ep.headers, verify=ep.verify` (the `_check_versions` pattern),
3s timeout. The manifest via the promoted-public
`updater.dist_ssl_context()` — currently the private `_dist_ssl_context`
(truststore/OS-trust for the release host); this spec **promotes it and
points `serverinit.fetch_manifest` at it too**, fixing the existing
inconsistency where a corporate-TLS-intercepted workstation can fetch the
client manifest but not the server one.

## Surfaces

The full state matrix. `running` rows; `latest` columns:

| | latest fetched | latest unfetchable/malformed | no `[dist]` |
|---|---|---|---|
| **clean vX.Y.Z, behind** | WARN (or acked-OK) | no row, no line | no row, no line |
| **clean vX.Y.Z, current** | OK "is current" | no row, no line | no row, no line |
| **clean vX.Y.Z, ahead** | OK "(ahead of published latest vY)" | no row, no line | no row, no line |
| **git-describe suffix** | OK "(source checkout — update via git)" | **same OK row** — it never needed the manifest | same OK row |
| **cortex /version unanswered** | nothing (check() → None) | nothing | nothing |

The source-checkout row depends **only** on cortex answering — review caught
the first draft holding it hostage to the public dist host's availability.
Dist-host unreachability is owned by the existing `client-version` row's
"cannot check for updates" warn, not by this row and not by the health rows
(which own *cortex* reachability, a different endpoint).

**Doctor row `server-version`** (live check on every doctor run, decision 5):

- behind: `[WARN] server-version: server v1.2.0, latest v1.3.0 — run
  `bash update.sh --to v1.3.0` on the server host (it backs up volumes
  first)`
- behind + acked: `[OK] server-version: server v1.2.0 (v1.3.0 available,
  acknowledged — clear [dist] server_update_ack to re-enable the warning)`
- current: `[OK] server-version: server v1.3.0 is current`
- ahead: `[OK] server-version: server v1.3.0 (ahead of published latest
  v1.2.1)` — real during the release pipeline's measured 11-25min Pages
  window and under `publish_latest=0`; "is current" would assert an equality
  that never happened.
- source checkout: `[OK] server-version: server v1.2.1-67-g040d0ed (source
  checkout — update via git)`

**Session-start briefing line** — appended beside `_update_nudge`'s line,
only for `relation == "behind"` and not acked:

```
[firekeep] server update available: v1.2.0 -> v1.3.0 — run `bash update.sh --to v1.3.0` on the server host
```

Shown-frequency mirrors the client nudge honestly: the *fetch* is day-cached;
the line renders at each session start while the state persists (same as the
client-update line today). The ack key is the remedy for a team that finds
that too chatty — not a hidden second cache.

**Coverage honesty:** hookless runtimes (codex, Claude Desktop, generic) get
the doctor row only — the same asymmetry client-update nudges already have,
recorded in `docs/guides/client-kit.md` beside the existing coverage notes.

**Privacy note (for client-kit.md too):** the check adds one daily GET of
`server/latest/server.json` to the same dist host, under the same `[dist]`
gate, at the same cadence as the existing client-update check — no previously
invisible machine becomes visible. New signal the dist host gains: it can
distinguish "machines whose team runs a bundle-deployed Keep" from
client-only installs, by request path. What it never learns: the running
version, or anything else — the comparison is client-side and `GET`-only; the
Keep's version never leaves the tailnet. No consent gate applies for the same
reason none applies to the client-update check it rides beside; absence of
`[dist]` disables both.

## Testing

- Comparator: behind / current / ahead / git-describe suffix → unjudged /
  malformed manifest → `latest=None` / missing `[dist]` → `latest=None`;
  every malformed input path proves `UpdateError` is caught, not
  truthiness-masked.
- Doctor row: every cell of the state matrix, including **doctor immediately
  after an update** (cached manifest from this morning + live cortex now
  reporting the new version → OK row, no stale warn — the decision-5
  regression test), the acked rendering, the ahead rendering **asserted on
  message text** (not merely not-warn), and no row when cortex is silent.
- `versions` row and `cmd_version` untouched: the existing tests at
  test_cli.py:52, test_cli_doctor.py:87-149 (and the stub sites: 551, 584,
  808, 941, 1073, 1252; test_kit_smoke.py:187) keep passing unmodified —
  the narrowed invariant's guard is a test asserting exactly one row JUDGES
  (contains the update-command text), while `versions` still reports.
- Briefing: line when behind+unacked; absent when current, ahead, acked,
  unjudged; manifest fetched at most once per day (counting stub); cortex
  read live each render.
- Ack: silences warn+line for the named version only; a newer latest re-arms
  both; parsing follows the `[dist]` conventions.
- Discipline: `check` never raises against hanging (short monkeypatched
  timeout), refusing, and garbage-returning endpoints on either fetch.
- Doc-agreement stays green for the client-kit.md additions.

## Risks

- **Nag fatigue.** Bounded two ways now: the ack key (decision 6) for
  deliberate pinning, and the line stopping the moment the live check sees
  the new version (decision 5). The first draft's "until the operator
  updates" claim was wrong on both edges.
- **`latest` mis-publish.** Upstream mitigations: release-workflow
  immutability checks, backwards-motion guard, `update.sh`'s default backup.
  This spec adds no new authority — it repeats what the pipeline published.
- **Duplicate-looking rows.** Doctor shows `versions` (report) and
  `server-version` (verdict). Different jobs, deliberately both; the
  narrowed invariant and its test keep judgment single-sourced.

## Files

`client/firekeep_client/serverupdate.py` (new);
`client/firekeep_client/updater.py` (promote `_dist_ssl_context` →
`dist_ssl_context`); `client/firekeep_client/serverinit.py` (point its
manifest fetch at the promoted helper); `client/firekeep_client/cli.py`
(new doctor row only — `_check_versions` and `cmd_version` untouched);
`client/firekeep_client/hooks/session_start.py` (one appended line);
`docs/guides/client-kit.md`; tests (`client/tests/test_serverupdate.py`,
doctor/briefing additions; existing `versions`-row tests untouched).
