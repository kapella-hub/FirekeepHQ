"""Bridge must be able to trigger an eval with the key it actually holds.

WHY THIS EXISTS. `POST /evals/sessions/{sid}/compute` was gated with
`require_scope("admin")`. Bridge posts to it on EVERY `ctx_complete_session`,
carrying `FIREKEEP_INTERNAL_KEY`, whose scopes are
`["memory:write", "session:read", "eval:read", "eval:write"]` — deliberately
NOT admin (`deploy/bootstrap-keys.sh` says so in a comment). So every session
completion got HTTP 403, Bridge treated 4xx as permanent and never retried, and
the live logs read `Eval trigger permanent failure for session ...: HTTP 403`
for four consecutive sessions.

The blast radius is everything downstream of eval outcomes: all 19 stored evals
carried `trigger="manual"` and the newest was 12 days old against 54 completed
sessions in the recent window; OWM's ranking signal joins replay `memory_read`
events to auto-eval outcomes and therefore had no fresh evidence; quality
trends and regression detection were frozen; the pattern A/B tip-effectiveness
join had nothing to join.

`eval:write` is not a widening. `auth/keys.py` already defines it, the internal
key already holds it, and computing one session's eval is exactly what it
names. `admin` gates decrypted vault reads and key minting.

BUT SWAPPING THE GATE OUTRIGHT WAS ITSELF A REGRESSION, and that is what the
`admin` half of these tests pins. `keys.scopes_allow` treats only `"*"` as a
superset — NOT `"admin"` — so a key holding a literal `["admin"]` and no
wildcard could call this endpoint before the change and got 403 after it. The
gate is therefore `eval:write` OR `admin` via `require_any_scope`, which is the
only shape FastAPI can express for OR (stacked dependencies are ANDed).
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.evals.api import create_evals_router


def _route(path_suffix: str, method: str):
    async def _fake_redis():
        return None

    router = create_evals_router(_fake_redis)
    for route in router.routes:
        if route.path.endswith(path_suffix) and method in route.methods:
            return route
    raise AssertionError(f"route {method} …{path_suffix} not found")


def _scopes_of(route) -> frozenset[str]:
    """Every scope that satisfies this route's gate.

    Handles both dependency shapes: `require_scope` closes over a single
    `scope`, `require_any_scope` over a `scopes` tuple.
    """
    for param in inspect.signature(route.endpoint).parameters.values():
        default = param.default
        dependency = getattr(default, "dependency", None)
        if dependency is None:
            continue
        closure = getattr(dependency, "__closure__", None) or ()
        code = getattr(dependency, "__code__", None)
        if code is None:
            continue
        for name, cell in zip(code.co_freevars, closure):
            if name == "scope":
                return frozenset({cell.cell_contents})
            if name == "scopes":
                return frozenset(cell.cell_contents)
    raise AssertionError("no scope dependency on this route")


def test_compute_accepts_eval_write():
    """The one-line root cause of 12 days of missing auto-evals."""
    assert "eval:write" in _scopes_of(_route("/sessions/{session_id}/compute", "POST"))


def test_compute_still_accepts_admin():
    """Widening for Bridge must not lock out the key that already worked.

    `scopes_allow` does NOT treat `admin` as a superset of anything, so a
    literal `["admin"]` key — the operator's own — loses access the moment the
    gate becomes `eval:write` alone.
    """
    assert "admin" in _scopes_of(_route("/sessions/{session_id}/compute", "POST"))


@pytest.mark.asyncio
async def test_an_admin_only_key_actually_passes_the_gate():
    """Behavioural, not introspective: run the real dependency.

    The scope-name assertions above would keep passing if `require_any_scope`
    were mis-wired; this exercises the gate an `["admin"]`-only key meets.
    """
    from auth import keys as _keys
    from auth.middleware import require_any_scope

    check = require_any_scope("eval:write", "admin")
    request = SimpleNamespace(headers={"X-API-Key": "k"})

    with patch.object(_keys, "_AUTH_ENABLED", True), patch(
        "auth.middleware.validate_key",
        new=AsyncMock(return_value={"scopes": ["admin"], "member_id": "m"}),
    ):
        assert (await check(request))["member_id"] == "m"

    with patch.object(_keys, "_AUTH_ENABLED", True), patch(
        "auth.middleware.validate_key",
        new=AsyncMock(return_value={"scopes": ["eval:write"], "member_id": "bridge"}),
    ):
        assert (await check(request))["member_id"] == "bridge"

    # And a key with neither is still refused — the gate is OR, not off.
    with patch.object(_keys, "_AUTH_ENABLED", True), patch(
        "auth.middleware.validate_key",
        new=AsyncMock(return_value={"scopes": ["memory:read"], "member_id": "x"}),
    ):
        with pytest.raises(HTTPException) as exc:
            await check(request)
        assert exc.value.status_code == 403


def test_read_routes_keep_their_own_scope():
    """Widening the write gate must not touch the read gates."""
    assert _scopes_of(_route("/sessions/{session_id}", "GET")) == {"eval:read"}
    assert _scopes_of(_route("/summary", "GET")) == {"eval:read"}


def test_trigger_is_a_parameter_not_a_hardcoded_literal():
    """`trigger="manual"` was hardcoded, so a Bridge-initiated eval would have
    been indistinguishable from a human one even once it worked — and "all 19
    evals say manual" was the signal that revealed the outage."""
    params = inspect.signature(
        _route("/sessions/{session_id}/compute", "POST").endpoint
    ).parameters
    assert "trigger" in params
