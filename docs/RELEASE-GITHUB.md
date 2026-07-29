# Client releases on GitHub (interim)

The office path is the GitLab pipeline (`.gitlab-ci.yml` → GitLab package registry).
Until that exists, client releases are cut on GitHub via `.github/workflows/release.yml`
and served from **GitHub Pages**.

## How a release is cut

1. Bump the version in **all four** markers:
   - `client/pyproject.toml` → `version = "X.Y.Z"`
   - `client/firekeep_client/__init__.py` → `__version__ = "X.Y.Z"`
   - `client/tests/test_package.py` → `assert firekeep_client.__version__ == "X.Y.Z"`
   - `client/tests/test_e2e_bootstrap.py` → `VERSION = "X.Y.Z"`
   The `client-release` CI workflow guards only the first two against the tag (mismatch
   fails the workflow); the test suite (`test_package.py`) and the e2e bootstrap test
   (`test_e2e_bootstrap.py`) enforce the other two instead — a mismatch there fails a local
   or CI test run, not the release workflow itself. (Skip for the very first release if the
   code is already at the target version.)
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

## Release notes

**Pre-tag gate (added after the 0.1.2 self-destruct bug):** before pushing the tag, run the
e2e bootstrap suite with a real uv on PATH — `cd client && PATH="$HOME/.firekeep/bin:$PATH" 
python -m pytest tests/test_e2e_bootstrap.py -m e2e -q`. It executes the real `install.sh`
against the locally built wheel including the wizard hand-off — the default suite excludes
it (`-m 'not e2e'`) and no connected CI runs it, which is how 0.1.2 shipped a bootstrap
that wiped its own venv at the wizard step.


- **0.1.2** — corporate-network users on ≤0.1.1: re-run the curl|sh bootstrap — pre-0.1.2
  updaters cannot reach the release manifest through an intercepting proxy.
