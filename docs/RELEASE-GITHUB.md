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

## One-time repo setup (GitHub Pages)

Settings → Pages → **Deploy from a branch** → Branch: **`gh-pages`** / `(root)` → Save.
(The first release creates the `gh-pages` branch; enable Pages once it exists.)

## What teammates use

`FIREKEEP_DIST_BASE` is the Pages root — **version-agnostic**, with `latest/` and `<version>/`
path segments exactly as the bootstrap expects (no bootstrap changes needed):

```
FIREKEEP_DIST_BASE = https://kapella-hub.github.io/Firekeep
```

Install (macOS / Linux):
```bash
curl -fsSL https://kapella-hub.github.io/Firekeep/latest/install.sh | FIREKEEP_DIST_BASE=https://kapella-hub.github.io/Firekeep sh
```
Windows (PowerShell):
```powershell
$env:FIREKEEP_DIST_BASE='https://kapella-hub.github.io/Firekeep'; irm https://kapella-hub.github.io/Firekeep/latest/install.ps1 | iex
```

The bootstrap fetches `latest/latest.json` → resolves the version → fetches
`<version>/SHA256SUMS`, then the checksum-verified `uv` and both wheels, exactly as with
the GitLab registry. Artifacts carry no secrets; the repo is public.

## Release notes

**Pre-tag gate (added after the 0.1.2 self-destruct bug):** before pushing the tag, run the
e2e bootstrap suite with a real uv on PATH — `cd client && PATH="$HOME/.firekeep/bin:$PATH" 
python -m pytest tests/test_e2e_bootstrap.py -m e2e -q`. It executes the real `install.sh`
against the locally built wheel including the wizard hand-off — the default suite excludes
it (`-m 'not e2e'`) and no connected CI runs it, which is how 0.1.2 shipped a bootstrap
that wiped its own venv at the wizard step.


- **0.1.2** — corporate-network users on ≤0.1.1: re-run the curl|sh bootstrap — pre-0.1.2
  updaters cannot reach the release manifest through an intercepting proxy.
