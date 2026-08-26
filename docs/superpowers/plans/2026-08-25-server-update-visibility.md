# Server Update Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect when a deployed Keep is behind `server/latest/server.json` and tell the operator — doctor row + daily briefing line — never applying anything.

**Architecture:** One new stdlib client module (`serverupdate.py`: live cortex `/version` read, day-cached manifest fetch, three-way relation, per-version ack) surfaces through a new doctor row and a session-start line. `_check_versions` and `cmd_version` are untouched; only the new row ever *judges*.

**Tech Stack:** Python 3.9+ stdlib, pytest.

**Spec:** `docs/superpowers/specs/2026-08-25-server-update-visibility-design.md` — read before any task; the state matrix in its Surfaces section is the contract.

## Global Constraints

- stdlib-only client; every public function never raises; two 3s timeouts max per `check()` call; the repo ruff gate is blocking (`ruff check --config ruff.toml` on every touched file).
- **Cache the manifest fetch only, never the verdict** (spec decision 5): scratch key `server_update_check`, value `today|<version-or-empty>`, negatives cached; cortex `/version` read live on every call.
- Comparison only via `updater.is_newer` inside `try/except updater.UpdateError` — it RAISES on malformed input, never returns False. Strip `v` with `removeprefix("v")`, never `lstrip`.
- `relation` is four-valued: `behind | current | ahead | unjudged` — ahead and current render differently.
- Ack: `[dist] server_update_ack = vX.Y.Z` matches `latest` exactly; a newer latest re-arms automatically.
- Never auto-apply anything (spec decision 4). The tell always names `bash update.sh --to vY`.
- `_check_versions` (row id `versions`) and `cmd_version` stay byte-identical; the invariant test asserts exactly one doctor row contains the update-command text.
- Commit per task with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: `serverupdate.py` + the promoted `dist_ssl_context`

**Files:**
- Create: `client/firekeep_client/serverupdate.py`
- Modify: `client/firekeep_client/updater.py` (rename `_dist_ssl_context` → `dist_ssl_context`; update every call site — grep `_dist_ssl_context` across `client/` first: `updater.py` internal uses and `wizard.py:167` at minimum), `client/firekeep_client/serverinit.py` (its manifest fetch at :46 gains `context=updater.dist_ssl_context()` — fixing the corporate-TLS inconsistency the spec names)
- Test: `client/tests/test_serverupdate.py`

**Interfaces:**
- Produces: `ServerUpdateStatus` dataclass (`running: str`, `latest: str | None`, `relation: str`, `ack: bool`); `check(cfg) -> ServerUpdateStatus | None` (None ONLY when cortex `/version` did not answer); `nudge_line(status) -> str` (the briefing line or `""` — Task 3 consumes); `updater.dist_ssl_context()` public.
- Consumes: `resolver.resolve("cortex", cfg=cfg)` (`.rest_base/.headers/.verify`), `transport.get_json`, `state.read_scratch/write_scratch`, `updater.dist_base/is_newer/UpdateError`, `updater.fetch_manifest`'s urllib shape (:87-89) as the pattern for the server-manifest fetch.

- [ ] **Step 1: Failing tests**

```python
# client/tests/test_serverupdate.py
"""serverupdate.check(): live cortex read, day-cached manifest, four-way
relation, per-version ack (spec decisions 2, 5, 6)."""
import configparser

import pytest

from firekeep_client import serverupdate, updater


def _cfg(extra=""):
    cfg = configparser.ConfigParser()
    cfg.read_string("[server]\nkind = ports\nhost = 127.0.0.1\n"
                    "[dist]\nbase_url = https://dist.example\n" + extra)
    return cfg


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_SCRATCH_DIR", str(tmp_path))
    # If state's scratch override env differs, adapt the MECHANICS to whatever
    # state._scratch_file honors (read state.py first); assertions must stand.
    return tmp_path


def _wire(monkeypatch, running="v1.2.0", latest="v1.3.0"):
    calls = {"manifest": 0}
    monkeypatch.setattr(serverupdate, "_fetch_running",
                        lambda cfg: running)
    def fake_manifest(cfg):
        calls["manifest"] += 1
        return latest
    monkeypatch.setattr(serverupdate, "_fetch_latest_uncached", fake_manifest)
    return calls


@pytest.mark.parametrize("running,latest,relation", [
    ("v1.2.0", "v1.3.0", "behind"),
    ("v1.3.0", "v1.3.0", "current"),
    ("v1.3.0", "v1.2.1", "ahead"),
    ("v1.2.1-67-g040d0ed", "v1.3.0", "unjudged"),
    ("v1.2.0", None, "unjudged"),
    ("v1.2.0", "not-a-version", "unjudged"),
])
def test_relation_matrix(monkeypatch, running, latest, relation):
    _wire(monkeypatch, running=running, latest=latest)
    status = serverupdate.check(_cfg())
    assert status is not None and status.relation == relation
    assert status.running == running


def test_none_only_when_cortex_silent(monkeypatch):
    monkeypatch.setattr(serverupdate, "_fetch_running", lambda cfg: None)
    assert serverupdate.check(_cfg()) is None


def test_manifest_day_cached_but_running_live(monkeypatch):
    calls = _wire(monkeypatch)
    serverupdate.check(_cfg())
    serverupdate.check(_cfg())
    assert calls["manifest"] == 1  # decision 5: fetch cached...
    # ...but a live running-version change shows immediately (post-update run)
    monkeypatch.setattr(serverupdate, "_fetch_running", lambda cfg: "v1.3.0")
    status = serverupdate.check(_cfg())
    assert status.relation == "current"  # no stale 'behind' from any cache


def test_negative_manifest_cached(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(serverupdate, "_fetch_running", lambda cfg: "v1.2.0")
    def fail(cfg):
        calls["n"] += 1
        raise updater.UpdateError("down")
    monkeypatch.setattr(serverupdate, "_fetch_latest_uncached", fail)
    assert serverupdate.check(_cfg()).relation == "unjudged"
    assert serverupdate.check(_cfg()).relation == "unjudged"
    assert calls["n"] == 1  # one 3s cost per day, not per call


def test_ack_matches_exact_version_and_rearms(monkeypatch):
    _wire(monkeypatch, running="v1.2.0", latest="v1.3.0")
    assert serverupdate.check(_cfg("server_update_ack = v1.3.0\n")).ack is True
    assert serverupdate.check(_cfg("server_update_ack = v1.2.9\n")).ack is False
    _wire(monkeypatch, running="v1.2.0", latest="v1.4.0")
    assert serverupdate.check(_cfg("server_update_ack = v1.3.0\n")).ack is False


def test_no_dist_section_is_unjudged_not_none(monkeypatch):
    monkeypatch.setattr(serverupdate, "_fetch_running",
                        lambda cfg: "v1.2.1-67-g040d0ed")
    cfg = configparser.ConfigParser()
    cfg.read_string("[server]\nkind = ports\nhost = 127.0.0.1\n")
    status = serverupdate.check(cfg)
    assert status is not None and status.relation == "unjudged"
    assert status.latest is None  # source-checkout row needs only cortex


def test_check_never_raises(monkeypatch):
    def explode(cfg):
        raise RuntimeError("boom")
    monkeypatch.setattr(serverupdate, "_fetch_running", explode)
    assert serverupdate.check(_cfg()) is None


def test_nudge_line():
    s = serverupdate.ServerUpdateStatus("v1.2.0", "v1.3.0", "behind", False)
    line = serverupdate.nudge_line(s)
    assert "v1.2.0 -> v1.3.0" in line and "update.sh --to v1.3.0" in line
    for quiet in [
        serverupdate.ServerUpdateStatus("v1.2.0", "v1.3.0", "behind", True),
        serverupdate.ServerUpdateStatus("v1.3.0", "v1.3.0", "current", False),
        serverupdate.ServerUpdateStatus("v1.3.0", "v1.2.1", "ahead", False),
        serverupdate.ServerUpdateStatus("x", None, "unjudged", False),
    ]:
        assert serverupdate.nudge_line(quiet) == ""
```

- [ ] **Step 2: Run to verify failure** — `cd client && python -m pytest tests/test_serverupdate.py -v` — FAIL (module missing). First READ `client/firekeep_client/state.py`'s scratch helpers to fix the fixture's env override to whatever the code honors.

- [ ] **Step 3: Promote the TLS helper** — grep `_dist_ssl_context` across `client/`; rename the def in `updater.py` to `dist_ssl_context`, update every caller (updater internal, `wizard.py:167`, any others the grep finds), no alias left. Point `serverinit.py:46`'s `urlopen` at `context=updater.dist_ssl_context()` (read the surrounding function; keep its error contract). Run `python -m pytest tests/ -k "updater or wizard or serverinit" -q` — green.

- [ ] **Step 4: Implement `serverupdate.py`**

```python
# client/firekeep_client/serverupdate.py
"""Server update visibility — detect-and-tell only.

Spec: docs/superpowers/specs/2026-08-25-server-update-visibility-design.md.
Never raises; never applies updates (spec decision 4: server updates can
carry irreversible store migrations — the tell always routes through
`bash update.sh --to vY`, which backs up first). Cortex /version is read
LIVE on every call; only the dist-manifest fetch is day-cached (decision 5 —
a cached verdict lied to the operator's post-update doctor run).
"""
from __future__ import annotations

import datetime
import json
import urllib.request
from dataclasses import dataclass

from firekeep_client import resolver, state, updater
from firekeep_client.transport import get_json

_TIMEOUT = 3.0
_CACHE_KEY = "server_update_check"


@dataclass
class ServerUpdateStatus:
    running: str
    latest: str | None
    relation: str  # "behind" | "current" | "ahead" | "unjudged"
    ack: bool


def _fetch_running(cfg) -> str | None:
    """Live cortex /version — the _check_versions fetch pattern, 3s."""
    try:
        ep = resolver.resolve("cortex", cfg=cfg)
        data = get_json(f"{ep.rest_base}/version", headers=ep.headers,
                        timeout=_TIMEOUT, verify=ep.verify)
        running = str((data or {}).get("version", "")).strip()
        return running or None
    except Exception:  # noqa: BLE001 — no answer means nothing to say
        return None


def _fetch_latest_uncached(cfg) -> str | None:
    """server/latest/server.json's version — updater.fetch_manifest's shape,
    the promoted dist_ssl_context (same trust story as the client manifest)."""
    base = updater.dist_base(cfg)  # raises UpdateError on checkout installs
    req = urllib.request.Request(f"{base}/server/latest/server.json")
    kwargs = {"timeout": _TIMEOUT}
    ctx = updater.dist_ssl_context()
    if ctx is not None:
        kwargs["context"] = ctx
    with urllib.request.urlopen(req, **kwargs) as resp:  # noqa: S310 — https dist host
        data = json.loads(resp.read().decode("utf-8"))
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise updater.UpdateError("malformed server manifest")
    return version.strip()


def _fetch_latest(cfg) -> str | None:
    """Day-cached (negatives too): one 3s cost per day, not per session."""
    try:
        today = datetime.date.today().isoformat()
        cached = state.read_scratch(_CACHE_KEY) or ""
        if cached.startswith(today + "|"):
            return cached.split("|", 1)[1] or None
        latest = ""
        try:
            latest = _fetch_latest_uncached(cfg) or ""
        finally:
            state.write_scratch(_CACHE_KEY, f"{today}|{latest}")
        return latest or None
    except Exception:  # noqa: BLE001
        return None


def _relation(running: str, latest: str | None) -> str:
    if latest is None:
        return "unjudged"
    try:
        run = running.strip().removeprefix("v")
        lat = latest.strip().removeprefix("v")
        if updater.is_newer(lat, run):
            return "behind"
        if updater.is_newer(run, lat):
            return "ahead"
        return "current"
    except updater.UpdateError:
        return "unjudged"


def _acked(cfg, latest: str | None) -> bool:
    try:
        if latest is None:
            return False
        return cfg.get("dist", "server_update_ack", fallback="").strip() == latest
    except Exception:  # noqa: BLE001
        return False


def check(cfg) -> ServerUpdateStatus | None:
    """None ONLY when cortex /version did not answer. Never raises."""
    try:
        running = _fetch_running(cfg)
        if running is None:
            return None
        latest = _fetch_latest(cfg)
        return ServerUpdateStatus(
            running=running, latest=latest,
            relation=_relation(running, latest),
            ack=_acked(cfg, latest),
        )
    except Exception:  # noqa: BLE001 — visibility must never cost a command
        return None


def nudge_line(status: ServerUpdateStatus | None) -> str:
    """The briefing line, or '' — behind + unacked only."""
    try:
        if status is None or status.relation != "behind" or status.ack:
            return ""
        return (f"\n\n[firekeep] server update available: {status.running} -> "
                f"{status.latest} — run `bash update.sh --to {status.latest}` "
                f"on the server host")
    except Exception:  # noqa: BLE001
        return ""
```

- [ ] **Step 5: Run** — `cd client && python -m pytest tests/test_serverupdate.py -v` then `ruff check firekeep_client/serverupdate.py firekeep_client/updater.py firekeep_client/serverinit.py tests/test_serverupdate.py --config ../ruff.toml` (adjust the config path to how other tasks ran it from the worktree root) — all green.
- [ ] **Step 6: Full client suite** — `python -m pytest tests/ -q -m "not e2e"` — no regressions (wizard/serverinit call-site renames covered).
- [ ] **Step 7: Commit** — `git add -A client && git commit -m "feat(serverupdate): live-vs-latest comparison with day-cached manifest and per-version ack"`

---

### Task 2: Doctor row `server-version`

**Files:**
- Modify: `client/firekeep_client/cli.py` (new `_check_server_version(cfg)`; wired into `run_doctor` after the `client-version` row; `_check_versions`/`cmd_version` byte-untouched)
- Test: `client/tests/test_cli_doctor.py` (append)

**Interfaces:**
- Consumes: `serverupdate.check(cfg)` (Task 1).
- Produces: doctor row id `server-version`, renderings per the spec's state matrix (quoted verbatim in the brief below).

- [ ] **Step 1: Failing tests** — append to `test_cli_doctor.py` (match the file's existing stub/fixture style — read its `versions`-row tests at :87-149 first):

```python
def _status(running, latest, relation, ack=False):
    from firekeep_client import serverupdate
    return serverupdate.ServerUpdateStatus(running, latest, relation, ack)


def test_server_version_behind_warns_with_command(monkeypatch):
    monkeypatch.setattr(cli.serverupdate, "check",
                        lambda cfg: _status("v1.2.0", "v1.3.0", "behind"))
    row = cli._check_server_version(cfg=None)
    assert row[0] == "server-version" and row[1] == "warn"
    assert "bash update.sh --to v1.3.0" in row[2] and "backs up" in row[2]


def test_server_version_acked_is_ok_but_states_the_fact(monkeypatch):
    monkeypatch.setattr(cli.serverupdate, "check",
                        lambda cfg: _status("v1.2.0", "v1.3.0", "behind", ack=True))
    row = cli._check_server_version(cfg=None)
    assert row[1] == "ok" and "v1.3.0 available" in row[2] and "acknowledged" in row[2]


def test_server_version_current_and_ahead_render_differently(monkeypatch):
    monkeypatch.setattr(cli.serverupdate, "check",
                        lambda cfg: _status("v1.3.0", "v1.3.0", "current"))
    assert "is current" in cli._check_server_version(cfg=None)[2]
    monkeypatch.setattr(cli.serverupdate, "check",
                        lambda cfg: _status("v1.3.0", "v1.2.1", "ahead"))
    row = cli._check_server_version(cfg=None)
    assert row[1] == "ok" and "ahead of published latest v1.2.1" in row[2]


def test_server_version_source_checkout_needs_no_manifest(monkeypatch):
    monkeypatch.setattr(cli.serverupdate, "check",
                        lambda cfg: _status("v1.2.1-67-g040d0ed", None, "unjudged"))
    row = cli._check_server_version(cfg=None)
    assert row[1] == "ok" and "source checkout" in row[2] and "git" in row[2]


def test_server_version_clean_but_unjudged_no_row(monkeypatch):
    # clean running version, manifest unavailable: client-version's
    # "cannot check for updates" owns dist-host trouble — this row stays out
    monkeypatch.setattr(cli.serverupdate, "check",
                        lambda cfg: _status("v1.2.0", None, "unjudged"))
    assert cli._check_server_version(cfg=None) is None


def test_server_version_silent_when_cortex_silent(monkeypatch):
    monkeypatch.setattr(cli.serverupdate, "check", lambda cfg: None)
    assert cli._check_server_version(cfg=None) is None


def test_exactly_one_row_judges_the_server_version(monkeypatch):
    """The narrowed invariant: `versions` reports, only server-version JUDGES
    (carries the update command)."""
    monkeypatch.setattr(cli.serverupdate, "check",
                        lambda cfg: _status("v1.2.0", "v1.3.0", "behind"))
    # build run_doctor with the file's existing all-stubs pattern (see the
    # stub sites around :551) so only the two version rows are live
    rows = _run_doctor_with_stubs(monkeypatch)   # adapt to the file's helper style
    judging = [r for r in rows if "update.sh --to" in r[2]]
    assert len(judging) == 1 and judging[0][0] == "server-version"
    assert any(r[0] == "versions" for r in rows)  # the report row survives
```

The unjudged-with-clean-running distinction needs the row to re-derive "is
this a clean vX.Y.Z" — expose that from Task 1 rather than duplicating:
add `is_clean_release(version: str) -> bool` to `serverupdate.py`
(parse succeeds → True) and use `status.relation == "unjudged" and
serverupdate.is_clean_release(status.running)` → no row; unclean → the
source-checkout row.

- [ ] **Step 2: Verify failure**, then **Step 3: implement** `_check_server_version` per the matrix (import `serverupdate` at cli.py's top with the existing grouped imports; wire into `run_doctor` directly after the `client_version` append, same optional-row pattern: `row = _check_server_version(cfg)` / `if row is not None: results.append(row)`). Add `is_clean_release` to serverupdate with a two-line test in test_serverupdate.py.
- [ ] **Step 4: Run** — the new tests + the untouched `versions` suite (`python -m pytest tests/test_cli_doctor.py tests/test_cli.py tests/test_serverupdate.py -q`) then full suite; ruff.
- [ ] **Step 5: Commit** — `"feat(doctor): server-version row — the one row that judges"`

---

### Task 3: Briefing line

**Files:**
- Modify: `client/firekeep_client/hooks/session_start.py` (import `serverupdate` in the grouped import; call after `_update_nudge`'s composition)
- Test: `client/tests/test_report_flush_points.py` style — a focused new test in `client/tests/test_serverupdate.py` or the session-start test file (read `client/tests/` for where session_start's systemMessage composition is asserted; follow that file)

**Interfaces:** consumes `serverupdate.check(cfg)` + `nudge_line(status)`.

- [ ] **Step 1: Failing test** — in the file where session_start's return is already asserted (grep `_update_nudge` or `systemMessage` in client/tests):

```python
def test_session_start_appends_server_update_line(monkeypatch):
    from firekeep_client import serverupdate
    from firekeep_client.hooks import session_start
    monkeypatch.setattr(
        session_start.serverupdate, "check",
        lambda cfg: serverupdate.ServerUpdateStatus("v1.2.0", "v1.3.0", "behind", False))
    # stub the rest per the file's existing pattern (resolver/load_config/agent_id)
    out = _run_session_start_stubbed(monkeypatch)
    assert "server update available: v1.2.0 -> v1.3.0" in out["systemMessage"]


def test_session_start_quiet_when_current_or_acked(monkeypatch):
    ...  # same harness, relation="current" then ack=True -> line absent
```

- [ ] **Step 2: Implement** — in `session_start.run`'s return composition, alongside the other appended nudges:

```python
    return {"systemMessage": rendered + _update_nudge(cfg) + _unsigned_notice()
            + serverupdate.nudge_line(serverupdate.check(cfg))
            + symdexindex.index_nudge(cfg, payload)
            ...}
```

(`check` never raises and costs one live cortex GET + the day-cached manifest — decision 5's stated budget.)
- [ ] **Step 3: Run** the touched test files + full suite + ruff. **Step 4: Commit** — `"feat(hooks): daily server-update line beside the client nudge"`

---

### Task 4: Docs

**Files:**
- Modify: `docs/guides/client-kit.md` (a "Server update visibility" subsection near the field-failure section: the state matrix in prose, the ack key with its re-arm semantics, the never-auto-apply invariant and why, the hookless-runtime coverage note, the privacy paragraph from the spec verbatim), root `CLAUDE.md` (one sentence beside the failure-reporting paragraph), `docs/guides/backup-and-restore.md` or wherever `update.sh` is documented — one line noting doctor/briefing now surface being-behind (grep for update.sh mentions first)
- Test: `cd client && python -m pytest tests/ -k "doc or matrix" -q` stays green

- [ ] **Step 1: Write the sections** (read the neighboring prose registers first; the spec's Surfaces + privacy text is the source). **Step 2: Run** doc-agreement + full client suite. **Step 3: Commit** — `"docs(serverupdate): guide section, ack key, privacy note"`

---

## Self-review record

Spec coverage: decisions 1-6 → Tasks 1 (module/cache/ack/comparator), 2 (matrix + invariant), 3 (line), 4 (docs); non-goals honored (no cmd_version/_check_versions edits — test-guarded). Type consistency: `ServerUpdateStatus(running, latest, relation, ack)` and `check/nudge_line/is_clean_release` used identically across tasks. Known judgment the executor must not "fix": doctor may show BOTH `versions` (report) and `server-version` (verdict) — deliberate; the no-row rule for clean-but-unjudged is deliberate (client-version owns dist trouble). Mechanics marked adaptable: scratch env override in Task 1's fixture, stub-harness helpers in Tasks 2-3 (assertions never weaken).

## Final verification

`cd client && python -m pytest tests/ -q -m "not e2e"` green; ruff clean; then a LIVE probe from this machine (personal deployment, source checkout): `firekeep doctor` must show `[OK] server-version: server v1.2.1-<suffix> (source checkout — update via git)` and no briefing nag — the spec's decision-2 promise demonstrated against the real Keep.
