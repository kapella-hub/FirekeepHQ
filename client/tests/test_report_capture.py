import errno

import pytest

from firekeep_client import cli, hooklog, report
from firekeep_client.transport import TransportError


@pytest.fixture(autouse=True)
def enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("FIREKEEP_FAILURE_REPORT", "1")
    return tmp_path


def _capture_emits(monkeypatch):
    calls = []

    def fake_emit(kind, stage, **kw):
        calls.append((kind, stage, kw))
    monkeypatch.setattr(report, "emit", fake_emit)
    return calls


def test_stage_slug_fixed_and_interpolated():
    assert cli._stage_slug("create venv") == ("create-venv", {})
    assert cli._stage_slug("bootstrap ~/.firekeep") == ("bootstrap-home", {})
    assert cli._stage_slug("render kiro adapter") == ("render-adapter", {"runtime": "kiro"})
    assert cli._stage_slug("pip install maildex (local checkout dir)") == (
        "pip-install-dex", {"dex": "maildex"})
    assert cli._stage_slug("total nonsense") == ("", {})  # unmapped -> build_event drops


class _EP:
    headers = {}
    verify = True

    def __init__(self, svc):
        self.rest_base = f"http://x/{svc}"


def test_check_health_partial_failure_emits_per_service(monkeypatch):
    calls = _capture_emits(monkeypatch)

    def fake_get(url, headers, verify):
        if "cortex" in url:
            raise TransportError("refused", category="connection-refused")
        return {"ok": True}

    monkeypatch.setattr(cli.resolver, "resolve", lambda svc, cfg=None: _EP(svc))
    monkeypatch.setattr(cli, "get_json", fake_get)
    cli._check_health(cfg=None)
    assert [(k, s, kw.get("error")) for k, s, kw in calls] == [
        ("connectivity", "cortex", "connection-refused")]


def test_check_health_all_down_emits_one_server_event(monkeypatch):
    calls = _capture_emits(monkeypatch)

    def refuse(url, headers, verify):
        raise TransportError("refused", category="connection-refused")

    monkeypatch.setattr(cli.resolver, "resolve", lambda svc, cfg=None: _EP(svc))
    monkeypatch.setattr(cli, "get_json", refuse)
    cli._check_health(cfg=None)
    assert [(k, s, kw.get("error")) for k, s, kw in calls] == [
        ("connectivity", "server", "connection-refused")]


def test_log_failure_with_exc_emits_runtime_event(monkeypatch):
    calls = _capture_emits(monkeypatch)
    hooklog.log_failure("session_start", "GET /briefing failed",
                        exc=PermissionError(errno.EACCES, "x"))
    assert len(calls) == 1 and calls[0][:2] == ("runtime", "session-start")


def test_log_failure_without_exc_emits_nothing(monkeypatch):
    calls = _capture_emits(monkeypatch)
    hooklog.log_failure("stop", "just a message")
    assert calls == []


def test_never_raise_crash_emits_runtime_event(monkeypatch):
    """A hook core's run() crashing must emit — the dispatcher's own crash
    handler never sees it, since never_raise swallows it first (review fix:
    6 of 7 runtime stages had no emitting path before this)."""
    calls = _capture_emits(monkeypatch)
    from firekeep_client import hooks

    def _boom(payload):
        raise PermissionError(errno.EACCES, "x")
    _boom.__module__ = "firekeep_client.hooks.pre_tool"

    result = hooks.never_raise(0)(_boom)({})
    assert result == 0
    assert len(calls) == 1 and calls[0][:2] == ("runtime", "pre-tool")
