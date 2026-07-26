# Plan: Foundation bundle — CI + build provenance + host-test fix

**Date:** 2026-05-30
**Status:** In progress
**Goal:** Remove "is it passing / is it deployed?" blind spots. No product-logic changes.

## Verified current state
- **No CI** (`.github/workflows/` does not exist).
- **Version hardcoded** `"0.6.0"` in 3 spots: `cortex/app/main.py` FastAPI app (L561),
  `/health` handler (L896), and `HealthResponse.version` default (`models.py`).
- **No git SHA** anywhere in the image or runtime; `restart` doesn't rebuild, so "are we
  latest?" requires introspecting live modules.
- **Dockerfile** (`cortex/Dockerfile`) has no build ARGs. Compose already uses an `args:` block
  (`INSTALL_TRAINING`) on cortex build targets — extend that convention.
- **Host tests**: cortex tests are fully mocked (conftest stubs graph/vector/redis), so they run
  on host with deps installed. Only `test_ranker.py` needs `joblib` — which `scikit-learn`
  (already in `cortex/requirements.txt`) pulls in transitively. The host just never had deps
  installed. Fix = a `cortex/requirements-dev.txt` (relay/sentinel already have this convention).
- **Test inventory**: cortex 52 files, symdex 30, bridge 10, relay 10, sentinel 5; shared
  replay 7 / corpus 9 / auth 2 / vault 2. `relay/requirements-dev.txt` pins `fakeredis`.

## Deliverables

### 1. Build provenance (TDD — testable)
- `cortex/app/version.py` (new): reads `GIT_SHA`, `BUILD_TIME`, `APP_VERSION` from env with safe
  defaults (`"unknown"` / `"dev"`). Single source of truth.
- `GET /version` endpoint → `{version, git_sha, build_time}`. Unauthenticated, no backend probes
  (unlike `/health`), so it's a cheap liveness+provenance check.
- Thread the version constant into the FastAPI `app(version=...)`, `/health`, and
  `HealthResponse` default — kill the triple-hardcoded `0.6.0`.
- Tests: `cortex/tests/test_version.py` — endpoint returns env-driven values; defaults when unset.

### 2. Dockerfile + compose provenance
- `cortex/Dockerfile`: `ARG GIT_SHA=unknown`, `ARG BUILD_TIME=unknown` → `ENV` so runtime reads them.
- `docker-compose.yml`: add `GIT_SHA`/`BUILD_TIME` to the `args:` blocks of cortex-api/mcp/worker/beat.
- `update.sh` / `install.sh`: export `GIT_SHA=$(git rev-parse --short HEAD)` and
  `BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)` before `docker compose build`.

### 3. CI pipeline
- `.github/workflows/ci.yml`: on push + PR.
- A `redis` service container (localhost:6379) shared across jobs so redis-touching tests work.
- Matrix job per service: install `requirements.txt` (+ `requirements-dev.txt` if present),
  run `pytest`. Cortex job installs `cortex/requirements.txt` (brings scikit-learn→joblib so the
  FULL suite incl. test_ranker runs) + `requirements-dev.txt`.
- A `lint` job: `ruff check` (add `ruff` to dev reqs; non-blocking to start — `continue-on-error`
  on first landing so it doesn't red-wall an unlinted codebase, with a note to flip it later).

### 4. Host-test fix
- `cortex/requirements-dev.txt`: pytest, pytest-asyncio, pytest-cov, ruff (joblib covered by
  scikit-learn but pin explicitly for clarity).
- Document `pip install -r requirements.txt -r requirements-dev.txt && pytest` in cortex/CLAUDE.md.

## Verification
- New `/version` unit tests pass on host.
- Full cortex suite still green in-container.
- `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"` parses.
- `docker compose config` validates after compose edits.
- Local build with `--build-arg GIT_SHA=test` → `curl /version` shows it (live check).

## Risks
- Don't break the existing `/health` contract — only swap the version literal for the constant.
- CI lint on a never-linted 311-file codebase will be noisy → start non-blocking.
- Shared-module tests may need real redis → the redis service container covers it; if any need
  Neo4j/Qdrant they'll be skipped/excluded (verify per-service in CI, don't assume).
