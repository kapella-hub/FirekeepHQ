# Client releases on GitHub (interim)

The office path is the GitLab pipeline (`.gitlab-ci.yml` → GitLab package registry).
Until that exists, client releases are cut on GitHub via `.github/workflows/release.yml`
and served from **GitHub Pages**.

## How a release is cut

1. Bump the version in **three** markers:
   - `client/pyproject.toml` → `version = "X.Y.Z"`
   - `client/firekeep_client/__init__.py` → `__version__ = "X.Y.Z"`
   - `client/tests/test_package.py` → `assert firekeep_client.__version__ == "X.Y.Z"`
   The `client-release` workflow guards the first two against the tag (a mismatch fails
   the workflow); `test_package.py` enforces the third, so a mismatch there fails the
   test run rather than the release.
   `client/tests/test_e2e_bootstrap.py` used to be a fourth marker and no longer is — it
   now DERIVES the version from `pyproject.toml`, so listing it here was an instruction to
   edit a line that does not exist.
2. Tag and push:
   ```bash
   git tag client-vX.Y.Z
   git push origin client-vX.Y.Z
   ```
3. The `client-release` workflow builds the client + symdex wheels, mirrors the pinned
   `uv` binaries, runs `make_release.py` (SHA256SUMS + latest.json), and publishes to the
   `gh-pages` branch — **accumulatively**, so old `<version>/` dirs survive for
   `firekeep update --to`.

## One-time setup — DONE (2026-07-29)

Recorded because none of it is discoverable from the code, and the previous
instruction here ("enable Pages once the first release creates `gh-pages`") had the
dependency backwards: Pages cannot be enabled for a branch that does not exist, so
the branch has to be seeded first. It is:

| | State |
|---|---|
| `kapella-hub/firekeep-dist` | created, **public**, artifacts only |
| `gh-pages` branch | seeded with `.nojekyll` + a landing page |
| GitHub Pages | enabled, serving `gh-pages` `/`, HTTPS enforced |
| Deploy key on `firekeep-dist` | installed, **write-enabled** |
| `FIREKEEP_DIST_DEPLOY_KEY` secret here | set |

`.nojekyll` is load-bearing: without it Jekyll drops files and directories starting
with `_` or `.` and can rewrite others, which would silently corrupt a release.

## What teammates use

`FIREKEEP_DIST_BASE` is the Pages root — **version-agnostic**, with `latest/` and `<version>/`
path segments exactly as the bootstrap expects (no bootstrap changes needed):

```
FIREKEEP_DIST_BASE = https://kapella-hub.github.io/firekeep-dist
```

Install (macOS / Linux):
```bash
curl -fsSL https://kapella-hub.github.io/firekeep-dist/latest/install.sh | FIREKEEP_DIST_BASE=https://kapella-hub.github.io/firekeep-dist sh
```
Windows (PowerShell):
```powershell
$env:FIREKEEP_DIST_BASE='https://kapella-hub.github.io/firekeep-dist'; irm https://kapella-hub.github.io/firekeep-dist/latest/install.ps1 | iex
```

The bootstrap fetches `latest/latest.json` → resolves the version → fetches
`<version>/SHA256SUMS`, then the checksum-verified `uv` and both wheels, exactly as with
the GitLab registry.

### Why artifacts live in a second, public repo

`kapella-hub/firekeep-dist` is **public** and holds nothing but a `gh-pages`
branch of release artifacts. This repo — the product source — stays **private**.

A previous version of this document justified publishing with *"Artifacts carry no
secrets; the repo is public."* That was false: this repo is private, and Pages
cannot serve from a private repo on a non-Enterprise plan, so the release path was
pointed at a channel that could never have worked. The three constraints that
forced the split:

1. **Pages needs a public repo.** Not available for a private one below Enterprise.
2. **Making *this* repo public was the wrong fix.** It would publish the server —
   Cortex's memory pipeline, the pattern engine, the eval machinery — which is the
   actual product. The client wheel is `py3-none-any` and packages only
   `firekeep_client*`, no server code, and the Free Tier is deliberately gratis, so
   its source is readable by anyone who installs it either way. **Gating the
   download protects nothing.** The property that matters for artifacts is
   integrity, not secrecy, and `SHA256SUMS` already provides it.
3. **The bootstraps fetch unauthenticated** — no `Authorization` header in either
   script — so a token- or SSO-gated origin returns login HTML and `updater.py`
   fails with `malformed manifest`. Any private channel needs new client code on
   the security-critical path.

**Cross-repo publishing** uses an SSH deploy key scoped to `firekeep-dist` alone,
stored here as the `FIREKEEP_DIST_DEPLOY_KEY` secret. The built-in `GITHUB_TOKEN`
cannot do this: its write access stops at the repo running the workflow. The key
is write-enabled on exactly one repo and has no expiry; if it leaks, the blast
radius is overwriting artifacts that are public and checksum-verified anyway.

Rotating it: generate a new `ed25519` pair, replace the deploy key on
`firekeep-dist`, and overwrite the secret here. No client change is needed —
clients never see it.

## Server releases

Publish the client containing `firekeep init` first, then tag the server:

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

`.github/workflows/server-release.yml` builds and pushes the Cortex, Bridge,
Sentinel, and Relay images, logs out of GHCR, and proves each image can be pulled
anonymously before it builds the source-free deployment bundle. The bundle and
strict `server.json` manifest are then published to both
`server/vX.Y.Z/` and (when the tag is newer) `server/latest/` in
`firekeep-dist`. A release is incomplete until both the anonymous image pulls
and the live Pages fetch pass.

GHCR creates a new package private by default. On the first server release, the
four pushes can succeed and the anonymous-pull steps will intentionally fail.
In the `kapella-hub` package settings, change each package visibility to
**Public** once, then rerun the failed workflow:

- `firekeep-cortex`
- `firekeep-bridge`
- `firekeep-sentinel`
- `firekeep-relay`

Do not distribute a registry token as a workaround. Public download access is
required for Solo; the signed offline entitlement is the Solo/Team boundary.
The bundle publishing job reuses the existing `FIREKEEP_DIST_DEPLOY_KEY`.

## Release notes

**Pre-tag gate (added after the 0.1.2 self-destruct bug) — NOW RUNS IN CI.** The e2e
bootstrap suite executes the real `install.sh` against the locally built wheel including
the wizard hand-off, which is how 0.1.2 shipped a bootstrap that wiped its own venv at
that step. It is a step in `release.yml`'s `test` job, which the `release` job needs, so
a tag cannot publish without it.

It used to be documented here as a MANUAL pre-tag step, and that instruction could not be
followed on the machine reading it: the suite skips wholesale on `os.name == "nt"`
(`test_e2e_bootstrap.py:23`), so on Windows all five tests skip even with `uv` installed —
and it skips itself again when `uv` is absent (`:102`). Attempting it before 0.1.24
produced "5 skipped", which is not a pass. CI runs it on ubuntu with `uv` installed first.

To run it by hand on macOS/Linux:
```bash
cd client && python -m pytest tests/test_e2e_bootstrap.py -m e2e -q
```


- **0.1.28** — Single-entry `firekeep gateway` adapters for Claude Code, Codex,
  Kiro, and OpenCode; single-use device/member join codes; Solo/Team entitlement
  status; and verified `firekeep init` download/update of the public source-free
  server bundle. This is the first client paired with workspace-scoped member
  attribution and the one-member Solo product boundary.
- **0.1.27** — FIRST PUBLISHED RELEASE. Four version numbers were tagged before
  one published; every one was stopped by its own gate on a release path that had
  never executed before. 0.1.24: no pytest in the release job, then a malformed
  workflow file. 0.1.25: a staged-venv guard unsatisfiable under the test
  harness's stubbed uv. 0.1.26: the staged venv itself — a venv is not
  relocatable, so renaming it left every console script pointing at a directory
  that no longer existed. Provisioning is in place again, with the exposure it
  carries documented rather than wished away.
- **0.1.26** — tagged, never published. Superseded by 0.1.27. Original notes: 0.1.24 and 0.1.25 were tagged and never
  published; each was stopped by its own test gate, which is the gate working.
  0.1.24: no pytest in the release job, then a malformed workflow file. 0.1.25: the
  new staged-venv guard aborted every install under the test harness's stubbed uv.
  All fixed and guarded here.
- **0.1.25** — tagged, never published. Superseded by 0.1.26. Original notes: 0.1.24 was tagged and never published: its
  own test gate caught a missing pytest install, then a malformed workflow file
  (an empty `with:` GitHub rejects and PyYAML accepts). Both fixed here, both
  guarded. The gate working is why nothing broken shipped.
- **0.1.24** — tagged, never published. Superseded by 0.1.25. Original notes: Nothing before this was ever built or
  served by the release workflow, so there is no upgrade path to describe; every
  earlier version number exists only in the source history.
  - Agents now recall memory when they should. The rendered instruction layer had
    no memory protocol at all, so an agent asked "deploy to my vps" answered that
    it did not know while the answer sat in memory at 100% confidence.
  - Codex gets `~/.codex/AGENTS.md`. It has no hook surface, so it previously
    received the MCP tools and nothing about when to use them.
  - Vault reads no longer require `admin`: new `vault:read` scope, minted into
    teammate keys. Writes and deletes stay admin-only.
  - The bootstrap builds beside and swaps instead of clearing the live venv, so a
    background auto-update no longer breaks the hooks of running POSIX sessions.
  - Dashboard: Firekeep mark, ember palette, and a health check that stops
    reporting HTTP 404 as a healthy service.

- **0.1.2** — corporate-network users on ≤0.1.1: re-run the curl|sh bootstrap — pre-0.1.2
  updaters cannot reach the release manifest through an intercepting proxy.
