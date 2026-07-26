"""Decision board suppression under personal mode — checked PER CALL (so a
mid-session /personal toggle suppresses immediately), guard is FIRST (no Cortex
call, no socket bound).
"""
from __future__ import annotations

import functools

import anyio
import pytest

from firekeep_client import resolver
from firekeep_client.decision import server


def _run(coro_func, *args, **kwargs):
    return anyio.run(functools.partial(coro_func, *args, **kwargs))


@pytest.fixture
def firekeep_home(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text("[active]\nprofile = personal\n", encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    monkeypatch.delenv("FIREKEEP_BYPASS", raising=False)
    monkeypatch.delenv("FIREKEEP_DECISION_HEADLESS", raising=False)
    server._BOARDS.clear()
    yield tmp_path
    server._BOARDS.clear()


def test_board_suppressed_when_personal_no_cortex_no_socket(firekeep_home):
    resolver.set_personal(True)

    def _must_not_call(*a, **k):
        raise AssertionError("Cortex must NOT be called under bypass (guard is first)")

    out = _run(server._run_decision_board, "ctx", ["q1?"], post_json=_must_not_call)

    assert isinstance(out, str)
    assert "personal mode" in out.lower()
    assert server._BOARDS == {}  # nothing bound


def test_board_check_suppressed_under_env_bypass(firekeep_home, monkeypatch):
    monkeypatch.setenv("FIREKEEP_BYPASS", "1")
    out = _run(server._run_decision_board_check, "any-id")
    assert isinstance(out, str)
    assert "personal mode" in out.lower()


def test_not_bypassed_reaches_normal_headless_path(firekeep_home, monkeypatch):
    # team mode + headless -> the REAL board path (inline spec with the question),
    # proving bypass did NOT swallow it.
    monkeypatch.setenv("FIREKEEP_DECISION_HEADLESS", "1")
    out = _run(server._run_decision_board, "ctx", ["q-real?"], post_json=lambda *a, **k: None)
    assert "q-real?" in out
    assert "personal mode" not in out.lower()
