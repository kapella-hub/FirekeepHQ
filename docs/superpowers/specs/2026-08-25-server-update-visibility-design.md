# Server update visibility — design

**Status:** approved in brainstorming, not yet implemented
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
  backwards-motion protection (`is_newer_release` guards `latest` from moving
  backwards). `firekeep init --version` / `update.sh --to` already consume it.
- Every published image bakes `APP_VERSION` (the release tag) and answers
  `GET /version` with it.
- Doctor's `_check_versions` already reads cortex's version — and
  **deliberately does not judge it** (its docstring records why: the old
  `version-skew` row compared server tags against *client* tags, two
  independent series where equality is meaningless, so every correct install
  warned and the row taught readers to skip the report).

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

**2. Only clean `vX.Y.Z` running versions are judged.** `GET /version` on a
bundle deployment returns the bare tag; on a source checkout it returns a
git-describe string (observed live: `v1.2.1-67-g040d0ed`). A suffixed version
means "built from source" — those operators update by `git pull`, the
comparison against a bundle manifest is not meaningful, and nagging them would
be noise. The comparator parses a clean `vMAJOR.MINOR.PATCH` or declines to
judge, reusing `updater.is_newer` with the `v` stripped — never a new version
parser.

**3. Everyone is told, on both existing surfaces.** A doctor row and a
once-daily session-start briefing line, mirroring exactly how client updates
surface. This was weighed against doctor-only (operators who never run doctor
never learn — the same silence this spec exists to end) and
provisioning-machine-only (breaks when the provisioning machine is not the
operator's daily driver). One line per day, team-wide, is the accepted cost.

**4. Never auto-applied — a hard invariant, not a default.** The client
auto-updates itself because a client venv swap is reversible and per-machine.
A server update can carry an **irreversible Neo4j store-format migration**
(the image-pinning guide's core lesson — the reason `neo4j:5-community`'s
floating tag was banned). The tell always routes through
`bash update.sh --to vY`, which takes a volume backup by default before
touching anything. No flag, config, or future convenience may make this spec
apply an update.

**5. One fetch per day, everything best-effort.** The check follows
`_update_nudge`'s exact discipline: once per calendar day, scratch-cached
(negative results too — an unreachable dist host costs at most one 3s timeout
per day, not one per session), never raises, never blocks, never prints a
traceback. Checkout-installed clients have no `[dist]` section
(`updater.dist_base` raises there): they skip silently.

## Non-goals

- Performing or scheduling server updates (decision 4).
- The server-side leg: a Sentinel collector, dashboard card, or server-behind
  event. Follow-up, deliberately out.
- Release notes. The manifest carries none; the tell names the version and
  the command, nothing more.
- Multi-service skew analysis. Cortex is the version authority, as it already
  is for doctor; a deployment mid-update resolves itself within minutes.
- Judging source checkouts (decision 2) or floating `:dev` images.

## Detection — `client/firekeep_client/serverupdate.py`

One new module, the shape of `autoupdate`/`_update_nudge`:

```python
def check(cfg) -> ServerUpdateStatus | None:
    """Compare the running Keep against server/latest/server.json.

    Once per calendar day (scratch key "server_update_check", negative
    results cached), best-effort, never raises. Returns None when there is
    nothing to SHOW at all: no [dist] section, cortex /version unreachable,
    or (for an unjudgeable running version) the manifest also unreadable.
    A running version that is not a clean vX.Y.Z still returns a status —
    doctor's source-checkout row needs the raw string — just never a
    behind=True one.
    """
```

`ServerUpdateStatus` carries `running` (the raw string), `latest`
(`None` when unfetched/malformed), `judged: bool`, and `behind: bool`
(`True` only when `judged`). The
running version comes from cortex `GET /version` (3s timeout, via the
resolver's endpoint + headers); the latest from
`{dist_base}/server/latest/server.json` (3s timeout, same TLS context the
updater uses for release-host fetches). Cache stores the composed verdict, so
doctor and the hook share one fetch per day rather than one each.

Comparison: strip the `v`, require three numeric components on both sides,
`updater.is_newer(latest, running)`. Any parse failure → `judged=False` —
the hook stays silent, doctor shows the source-checkout row.

## Surfaces

**Doctor row `server-version`** (inserted beside the existing `client-version`
row). `_check_versions`' cortex-version reporting is superseded by this row —
the invariant, test-guarded, is that doctor never shows two rows reporting the
server's version; whether the remainder of `_check_versions` survives or
collapses into the other version rows is the implementation plan's call after
reading the actual row body:

- current: `[OK] server-version: server v1.3.0 is current`
- behind: `[WARN] server-version: server v1.2.0, latest v1.3.0 — run
  `bash update.sh --to v1.3.0` on the server host (it backs up volumes first)`
- source checkout: `[OK] server-version: server v1.2.1-67-g040d0ed (source
  checkout — update via git)`
- unreachable / no dist / malformed: **no row**. The health rows already own
  "unreachable", and a row that cannot say anything true says nothing.

**Session-start briefing line**, appended exactly where `_update_nudge`'s
line lands, subject to the same once-daily cache:

```
[firekeep] server update available: v1.2.0 -> v1.3.0 — run `bash update.sh --to v1.3.0` on the server host
```

No line when current, unjudgeable, or already shown today.

**Coverage honesty:** hookless runtimes (codex, Claude Desktop, generic) get
the doctor row only — the same asymmetry client-update nudges already have,
recorded in `docs/guides/client-kit.md` beside the existing coverage notes.

## Testing

- Comparator: behind / current / ahead (latest older than running — servers
  can legitimately run ahead of `latest` during a staged release; not a warn),
  git-describe suffix → not judged, malformed manifest → not judged, missing
  `[dist]` → silent skip.
- Doctor row: all four states above, including the no-row cases; the
  superseded cortex-version line is gone (no duplicate version rows).
- Briefing: line appears when behind, absent when current, cached once per
  day (second call same day → no fetch, verified with a counting stub).
- Discipline: `check` never raises against a hanging endpoint (short
  monkeypatched timeout), a refusing endpoint, and garbage JSON.
- Doc-agreement tests stay green for the client-kit.md additions.

## Risks

- **Nag fatigue.** Every member's briefing carries the line until the
  operator updates. Accepted in brainstorming (decision 3) — it is one line
  per day, and the alternative is the silence this spec exists to end.
- **`latest` mis-publish.** If `server/latest/server.json` ever pointed at a
  bad version, every deployment would be told to update to it. Mitigations
  already exist upstream: the release workflow's immutability checks and
  backwards-motion guard, and `update.sh`'s default backup. This spec adds no
  new authority — it repeats what the release pipeline published.
- **Two sources of "server version" truth in doctor** if the superseding of
  `_check_versions`' cortex line is missed. The test asserting no duplicate
  version rows is the guard.

## Files

`client/firekeep_client/serverupdate.py` (new);
`client/firekeep_client/cli.py` (doctor row, `_check_versions` supersession);
`client/firekeep_client/hooks/session_start.py` (one appended line);
`docs/guides/client-kit.md`; tests
(`client/tests/test_serverupdate.py`, doctor/briefing additions).
