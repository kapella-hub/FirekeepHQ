# Firekeep releases on GitHub

GitHub is the public distribution path. Client releases
are cut via `.github/workflows/release.yml`; server images and the source-free
deployment bundle are cut via `.github/workflows/server-release.yml`. Release
manifests and client/bundle artifacts are served from **GitHub Pages**. There is
no separate enterprise or corporate edition/channel.

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
   `firekeep_client*`, no server code, so its source is readable by anyone who
   installs it either way. **Gating the
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

The server workflow is tag-only: it cannot be manually dispatched from an
unrelated branch. Each repository's version tag is also write-once. A rerun
pulls the existing image, verifies its Git SHA and application version, and
continues the public-access check without rebuilding or replacing the artifact.
For the documented first server tag (`v0.1.0`) only, a GHCR `denied` response is
cross-checked against GitHub's package API; the workflow creates a package only
when that API confirms it does not exist. Every image carries the source-repository
label GitHub uses to link the new package back to this workflow repository.

GHCR creates a new package private by default. On the first server release, the
four pushes can succeed and the anonymous-pull steps will intentionally fail.
In the `kapella-hub` package settings, change each package visibility to
**Public** once, then rerun the failed workflow:

- `firekeep-cortex`
- `firekeep-bridge`
- `firekeep-sentinel`
- `firekeep-relay`

Do not distribute a registry token as a workaround. Public download access is
the customer install path — the licence (BUSL-1.1) is a legal boundary, never a
download gate, and there is no entitlement system to fall back on.
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


- **0.1.36** — Single-product conversion: the Solo/Team split and the signed
  licence-key system are removed — the BUSL-1.1 LICENSE is the only boundary
  (legal terms, no technical enforcement). On the client that removes the
  doctor `licence` row (it read the server's entitlement off `GET /workspace`)
  and the plan label the member-join used to print; joining now simply
  confirms the membership. Auth is untouched — API keys, scopes, join codes
  and member enrollment all remain. The server counterpart ships in the
  matching server release.
- **0.1.35** — Side-by-side venvs: every release installs to its own
  `~/.firekeep/venvs/<version>`, selected by a `~/.firekeep/current` link (NTFS
  junction / POSIX symlink) that every rendered surface routes through. Updates
  provision the new venv beside whatever is running and flip the link, so they
  no longer ask you to close agent sessions — the old Windows guard printed a
  wall of ~93 raw PIDs and refused; the one refusal left is a forced reinstall
  of the running version, and it counts holders by process name. The Windows
  updater runs the bootstrap as a foreground child streaming to the caller's
  console instead of a detached process tearing across it, `firekeep update
  --to <prev>` is an instant flip while the previous venv survives GC (keep
  current + previous; liveness proven by rename-probe, never process
  enumeration), and a venv held by open sessions is kept with one humane line.
  Also fixes the two update-path defects that made the update to 0.1.34 print
  success while installing nothing: the version probe now runs `python -I` so
  a checkout's working directory cannot shadow the installed version into a
  false "already up to date", and Windows PowerShell 5.1 no longer inherits a
  pwsh-7 `PSModulePath` that broke `Get-FileHash` inside the checksum gate.
- **0.1.34** — UTF-8 stdio: the Windows client spoke JSON on stdio at the ANSI
  code page, silently corrupting every non-ASCII character an agent wrote
  through it (an em dash landed in the live store as `â€"`; the server was
  never involved). Every stdio entry point now pins stdin/stdout/stderr to
  UTF-8 with literal-LF newlines. Releases are signed: Ed25519/minisign over
  SHA256SUMS, verified against the key pinned in client 0.1.42+ (minted
  2026-08-12; verify-if-present until `require_signed` flips on production
  evidence; an invalid signature is always fatal), with the verified sums
  threaded through to the bootstrap rather than fetched twice. The first
  signed release was 0.1.42, serve-verification green on first execution.

## PyPI and the MCP registry

The `pypi` job in `release.yml` publishes `firekeep-client` and
`firekeep-symdex` to PyPI on every `client-v*` tag via **Trusted Publishing**
(OIDC — no token secret to mint, rotate, or leak). It fails loudly on a
missing or misconfigured publisher without touching the dist-host
publication; `skip-existing` makes an already-published symdex version a
no-op, since symdex bumps on its own cadence.

**One-time setup (operator, on pypi.org — required BEFORE the first tag with
this job):** log in → your account → Publishing → for EACH of
`firekeep-client` and `firekeep-symdex`, add a *pending publisher* with:
owner `kapella-hub`, repository `FirekeepHQ`, workflow `release.yml`,
environment `pypi`. Also create the `pypi` environment in the GitHub repo
settings (no reviewers needed). The first successful publish claims the
project names; both were verified unregistered on 2026-08-13.

**The MCP registry** (`registry.modelcontextprotocol.io`) is the follow-up
once the PyPI packages exist: `server.json` at the repo root holds the draft
entry (`io.github.kapella-hub/firekeep`, PyPI package `firekeep-client`,
stdio transport). Publish with the `mcp-publisher` CLI — `login github`
(device flow, proves the io.github.kapella-hub namespace), then
`mcp-publisher publish` from the repo root after bumping `server.json`'s two
version fields to the released client version. Ownership proof on the PyPI
side is the `mcp-name: io.github.kapella-hub/firekeep` line already carried
in `client/README.md` (the package README lands on the PyPI page, where the
registry's validator reads it). The CLI's validator is the schema authority;
if it rejects a field, trust it over the checked-in draft. The Windows lease gate fires again (and says why
  when it cannot), and gateway reconcile
  stops losing the session, the outcome, and every warning. Ships symdex
  0.2.16, whose `watch_folder` can finally see files that were added.
- **0.1.33** — Uncommitted work becomes recoverable: destructive git commands
  (`git checkout --`, `git restore`) snapshot the dirty tree locally before
  running — `firekeep restore --list|--apply` reads it back, and snapshots
  never leave the machine. Night Shift gains Ollama alongside LM Studio
  (detection, not protocol) and stops re-enqueuing per-turn duplicate distill
  tasks (193 of 200 pending tasks on the live queue were duplicates from
  unstamped sessions). Ships symdex 0.2.15 with the audited 12-language
  retrieval baseline.
- **0.1.32** — Windows updates preserve the optional `--non-interactive`
  handoff as a one-element argument array, so PowerShell does not splat it into
  individual characters before adapter re-rendering.
- **0.1.31** — Existing installs and forced reinstalls reuse their connection
  without prompting, and stored API keys are never rendered as prompt defaults.
  Kiro upgrades now remove the retired per-service MCP entries and grants from
  its named agent, leaving the single Firekeep gateway while preserving foreign
  servers and user grants.
- **0.1.30** — HTTP-backed MCP shims survive transient service restarts without
  replaying ambiguous requests; recovery covers POST failures, fragmented or
  terminated SSE bodies, and interrupted initialization. The package stays on
  supported MCP/httpx majors, and the release job installs the built wheel in a
  clean environment before publish. A runtime whose stdio child was already
  closed before this version is installed still needs one restart.
- **0.1.29** — Reliable Codex Decision Board wiring: gateway initialization
  carries compact recall/Decision Board guidance, installer failures to update
  `~/.codex/AGENTS.md` warn visibly, and `firekeep doctor` validates the local
  backends plus exact Codex MCP/instruction blocks.
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
