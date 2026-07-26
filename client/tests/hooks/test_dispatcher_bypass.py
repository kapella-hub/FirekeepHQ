"""Personal / bypass mode at the dispatcher: when bypassed, cores no-op LIVE.

The dispatcher re-checks the gate on every invocation, so a mid-session
`/personal` toggle takes effect at once. session_start/prompt announce personal
mode; pre_tool/post_tool allow (exit 0) without any gateway call; `stop` is the
one core NOT short-circuited — it self-handles bypass to clear the marker.
"""
from __future__ import annotations

import io
import json
import sys

import pytest

from firekeep_client import resolver
from firekeep_client.hooks import __main__ as dispatcher


def _stub_core(monkeypatch, core, record):
    """Replace a core's run() with a recorder so we can assert it did/didn't run."""
    class Core:
        @staticmethod
        def run(payload):
            record.append(core)
            return {"systemMessage": "REAL-CORE-RAN"}
    monkeypatch.setitem(dispatcher._CORE_MODULES, core, Core)


@pytest.mark.parametrize("core", ["session_start", "prompt"])
def test_bypassed_dict_core_announces_and_skips_real_core(client_env, monkeypatch, capsys, core):
    resolver.set_personal(True)
    ran = []
    _stub_core(monkeypatch, core, ran)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    rc = dispatcher.main([core])

    assert rc == 0
    assert ran == []  # the real briefing/prompt core never ran
    out = json.loads(capsys.readouterr().out)
    assert "PERSONAL MODE" in out["systemMessage"]
    assert "REAL-CORE-RAN" not in out["systemMessage"]


@pytest.mark.parametrize("core", ["pre_tool", "post_tool"])
def test_bypassed_int_core_allows_without_running(client_env, monkeypatch, core):
    resolver.set_personal(True)
    ran = []

    class Core:
        @staticmethod
        def run(payload):
            ran.append(core)
            return 1  # would BLOCK if it ran
    monkeypatch.setitem(dispatcher._CORE_MODULES, core, Core)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    # even with --block-exit 2, bypass returns 0 (allow) without the gateway call
    rc = dispatcher.main([core, "--block-exit", "2"])

    assert rc == 0
    assert ran == []


def test_stop_is_not_short_circuited_when_bypassed(client_env, monkeypatch):
    """stop MUST still run so it can clear the marker + skip comms itself."""
    resolver.set_personal(True)
    ran = []
    _stub_core(monkeypatch, "stop", ran)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    rc = dispatcher.main(["stop"])

    assert rc == 0
    assert ran == ["stop"]  # dispatcher ran the real stop core


def test_env_bypass_also_short_circuits(client_env, monkeypatch, capsys):
    monkeypatch.setenv("FIREKEEP_BYPASS", "1")  # no marker; env alone triggers it
    ran = []
    _stub_core(monkeypatch, "session_start", ran)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    rc = dispatcher.main(["session_start"])

    assert rc == 0
    assert ran == []
    assert "PERSONAL MODE" in json.loads(capsys.readouterr().out)["systemMessage"]


def test_not_bypassed_runs_core_normally(client_env, monkeypatch, capsys):
    # No marker, no env -> normal team mode -> the real core runs.
    ran = []
    _stub_core(monkeypatch, "prompt", ran)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    rc = dispatcher.main(["prompt"])

    assert rc == 0
    assert ran == ["prompt"]
    assert "REAL-CORE-RAN" in json.loads(capsys.readouterr().out)["systemMessage"]
