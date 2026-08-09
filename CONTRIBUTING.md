# Contributing to Firekeep

Thanks for your interest. This document covers the licensing situation first —
because it is unusual and you should understand it before writing code — then
the practical mechanics.

---

## 1. Licensing: read this before you contribute

Firekeep is **source-available, not open source.** The distinction is real and
we hold ourselves to it: please don't describe the repository as open source,
and we won't either.

**The Licensed Work is under the [Business Source License 1.1](LICENSE)**
(Licensor: Omnicron, LLC). In short:

- You may **read, copy, modify, and redistribute** the source, and make
  **non-production** use of it freely.
- **Production use is free for one natural person, in one Firekeep workspace,
  on one deployment** — with unlimited devices, client runtimes, agent
  identities, terminals, and background workers for that person. Production use
  by more than one member requires a commercial license from Omnicron, LLC.
- Each version **converts to Apache License 2.0** on the fourth anniversary of
  that version's first public distribution.

Read [`LICENSE`](LICENSE) for the authoritative text — the summary above is not
a substitute for it.

Some components are planned to move to Apache-2.0 (the client kit, runtime
adapters, protocol-facing SDKs, and a standalone Symdex Core). Until that
happens, **everything in this repository is BUSL-licensed**, including
`client/` and `symdex/`. Don't assume otherwise from a directory name.

### 1.1 The CLA is required

Because Firekeep is licensed under more than one set of terms — and will be
under more as the Apache split lands — we need the right to license your
contribution under all of them. Without that, a single contributed patch could
permanently freeze a component's license.

Before your first pull request is merged, you'll need a Contributor License
Agreement on file (until an automated CLA check is wired up, a maintainer
coordinates signing in the pull-request thread — the requirement is the same
either way):

- **[Individual CLA](CLA/ICLA.md)** — everyone signs this.
- **[Corporate CLA](CLA/CCLA.md)** — *additionally* required when you're
  contributing in the course of employment, or your employer owns IP you
  create. The two are complementary: the ICLA binds you, the CCLA binds the
  employer whose rights would otherwise encumber the work.

**You keep the copyright in your contributions.** The CLA is a license to us,
not an assignment, and it is not exclusive — you remain free to use, sell, and
relicense your own work however you like.

Signing is recorded against your GitHub account with the agreement version and
a timestamp. Trivial changes (typo fixes, comment corrections) may be merged
without one at a maintainer's discretion.

### 1.2 Third-party code and AI assistance

- **Don't paste in code you didn't write** without flagging it. If a
  contribution includes third-party material, say so in the PR and name its
  license. See ICLA §7 for the mechanism.
- **AI-assisted contributions are welcome** — this codebase is itself heavily
  AI-assisted — but you are responsible for what you submit. Review it,
  understand it, and be able to defend it in review. Disclose AI assistance
  where it's material to the originality representations in ICLA §5.
- **Dependencies are gated.** GPL/AGPL-licensed dependencies are rejected by CI
  (`scripts/check_licenses.py`). Permissive and MPL licenses pass. Adding a
  dependency means adding its notice to [`NOTICE`](NOTICE) —
  `scripts/generate_notice.py` regenerates it.

---

## 2. Reporting security issues

**Do not open a public issue for a security vulnerability.** Follow the
disclosure process in [`SECURITY.md`](SECURITY.md).

---

## 3. Getting set up

Firekeep is a Python monorepo: four server services plus shared modules, a
client kit, and a code-intelligence server. Python **3.11+** for the server
services; the client kit and Symdex support **3.10+**.

```bash
git clone <repo> && cd Firekeep

# shared-module tests need Redis only, no application containers
docker compose -f docker-compose.test.yml up -d

# client kit, from the checkout
cd client && ./install          # .\install.ps1 on Windows
```

Each service is tested from its own root. **Do not run `pytest` at the
repository root without a path** — several `tests` packages collide:

```bash
cd cortex   && pytest tests/ -v
cd bridge   && pytest tests/ -v
cd sentinel && pytest tests/ -v
cd relay    && pytest tests/ -v
cd symdex   && pytest tests/ -v
cd client   && python -m pytest tests -q
python -m pytest tests -q                      # repo-level guards, from root
python -m pytest replay/tests auth/tests vault/tests corpus/tests -v
```

---

## 4. What CI will check

Every job below is **blocking**. Running the relevant ones locally before
pushing will save you a round trip.

| Job | What it enforces |
|---|---|
| `test` | Service test suites |
| `client-windows`, `symdex-windows` | The Windows-only paths (junction/registry/PowerShell behaviour that self-skips elsewhere) |
| `lint (ruff)` | `ruff check .` clean — config in `ruff.toml` |
| `security` | `pip-audit --strict` over each shipped dependency set in its own clean venv; publishes CycloneDX SBOMs. **Starts from zero CVEs — keep it there** |
| `secrets` | gitleaks over the working tree **and full history** |
| `licenses` | No GPL/AGPL dependencies, in three isolated venvs (cortex, client, symdex) |
| `forbidden-tokens` | `scripts/check_forbidden_tokens.py` — placeholder/residual tokens that must never ship |
| `benchmarks` | Symdex retrieval baseline |

---

## 5. House rules that bite

These are the conventions most likely to fail a review. They exist because each
one has already caused a real failure.

**Follow the Change Consistency Checklist.** Adding, removing, or renaming an
MCP tool, REST endpoint, env var, or config setting means updating *every* file
in the checklist in [`CLAUDE.md`](CLAUDE.md) — MCP server, lifespan wiring, REST
routes, compose files, the relevant `docs/guides/<area>.md`, client adapters and
installer output, the dashboard, and `CLAUDE.md` itself. Stale references after
a removal are bugs, not cleanup debt.

**Dependency locking.** `<svc>/requirements.txt` holds loose ranges and is the
*input*; `<svc>/requirements.lock` is the hash-pinned *output* the Dockerfiles
install. Regenerate after any edit — CI fails on drift:

```bash
uv pip compile <svc>/requirements.txt --python-platform linux \
  --python-version 3.11 --generate-hashes --output-file <svc>/requirements.lock
```

`--python-platform linux` is load-bearing: the lock is generated on your machine
but must install into the pinned `python:3.11.15-slim` base. **`client/` and
`symdex/` are deliberately NOT locked** — they ship as wheels into users'
virtualenvs, and pinning transitive dependencies would force them on every
consumer. `tests/test_requirements_lock.py` asserts they stay unlocked.

**Image pinning.** Every `image:` and `FROM` is pinned by **tag *and* digest**,
and the digest must be the **top-level manifest-list digest**, never a
per-platform one — a platform digest breaks every other architecture and fails
on someone else's machine, not yours. See the "Image pinning" section of
`CLAUDE.md` and run `pytest tests/test_image_pins.py`.

**Line endings and encoding.** Shell scripts are LF (`.gitattributes` enforces
`*.sh text eol=lf`); a CRLF `install.sh` fails on Linux with
`set: Illegal option -`. Read and write files as **UTF-8 explicitly** — the
platform default on Windows is cp1252, which silently corrupts every non-ASCII
character that crosses a stdio boundary. There is a whole module
(`client/firekeep_client/stdio.py`) about this and a test suite guarding it.

**Tests encode invariants — don't delete one to go green.** If a guard fails
because the design changed, rewrite it to guard the *new* invariant and say in
the docstring what it protects and why. Several tests in this repo exist as
tombstones for approaches that were tried and failed; deleting them to pass CI
reintroduces the bug they were written to prevent.

**Write comments that explain *why*.** The prevailing style records the failure
that motivated the code — measured behaviour, error messages, dates. Match it.

---

## 6. Pull requests

- **Open an issue first** for anything non-trivial. A design disagreement is
  much cheaper before the code exists.
- **One concern per PR.** Mixed refactor-plus-behaviour changes are hard to
  review and harder to revert.
- **Explain the failure you're fixing**, not just the change. If there's a
  reproduction, include it.
- **Commit messages**: a `type(scope): summary` subject line, then a body
  explaining the reasoning. Look at `git log` for the register.
- Contributions may be declined if they don't fit the project's direction. That
  is not a judgement of the work — please ask before investing significant
  effort.

---

## 7. What we're most glad to receive

- **Bug reports with reproductions**, especially on platforms we don't test
  daily (macOS, Linux desktop, non-English Windows locales).
- **Runtime adapters** for MCP clients we don't ship yet.
- **Documentation corrections** — particularly anywhere the docs and the code
  disagree. That's a bug in the docs' favour.
- **Failure reports from real deployments.** The most useful thing in this
  repository is the accumulated record of what broke and why.

---

*Firekeep is a project of Omnicron, LLC. By contributing you agree that your
contributions are licensed under the terms of the CLA you have signed.*
