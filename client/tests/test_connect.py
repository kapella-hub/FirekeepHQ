"""`firekeep connect` issues over SSH and redeems through the shared join core."""

from __future__ import annotations

import json

import pytest

from firekeep_client import connect as C
from test_joincode import encode, payload


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / ".firekeep" / "config"))
    return tmp_path


def _fake_ssh(*, bind_addr="127.0.0.1", has_invite=True):
    invite_payload = payload(t="tunnel", s="root@h")
    invite_payload.pop("h", None)
    invite_payload["h"] = "127.0.0.1"
    code = encode(invite_payload)

    def run(target, remote_cmd, *, timeout=60):
        if "FIREKEEP_SSH_OK" in remote_cmd:
            return 0, "FIREKEEP_SSH_OK\n"
        if "docker-compose.yml" in remote_cmd:
            return 0, "/opt/Firekeep\n"
        if "COMMIT=" in remote_cmd:
            return 0, f"COMMIT=abc1234\nBIND_ADDR={bind_addr}\nAUTH_ENABLED=true\n"
        if " invite " in remote_cmd:
            return (0, json.dumps({"code": code}) + "\n") if has_invite else (1, "")
        if "grep -c 'invite)'" in remote_cmd:
            return 0, ("1\n" if has_invite else "0\n")
        return 0, ""

    return run


def test_connect_issues_then_redeems_through_join(home, monkeypatch):
    monkeypatch.setattr(C, "_ssh", _fake_ssh())
    calls = []
    monkeypatch.setattr(
        "firekeep_client.join.join",
        lambda code, **kwargs: calls.append((code, kwargs)) or 0,
    )
    assert C.connect("root@h", agent_id="alex") == 0
    assert calls[0][0].startswith("fk_join_")
    assert calls[0][1] == {"agent_id": "alex", "force": True}


def test_mint_key_and_direct_config_writer_are_gone():
    assert not hasattr(C, "_mint_key")
    assert not hasattr(C, "_write_server")


def test_no_tunnel_against_loopback_fails_before_issue(home, monkeypatch):
    monkeypatch.setattr(C, "_ssh", _fake_ssh())
    with pytest.raises(C.ConnectError, match="loopback"):
        C.connect("root@h", agent_id="alex", use_tunnel=False)


def test_public_server_still_uses_same_join_core(home, monkeypatch):
    monkeypatch.setattr(C, "_ssh", _fake_ssh(bind_addr="0.0.0.0"))
    calls = []
    monkeypatch.setattr("firekeep_client.join.join", lambda code, **kwargs: calls.append(code) or 0)
    assert C.connect("root@203.0.113.9", agent_id="alex") == 0
    assert len(calls) == 1


def test_old_server_names_update_remedy(home, monkeypatch):
    monkeypatch.setattr(C, "_ssh", _fake_ssh(has_invite=False))
    with pytest.raises(C.ConnectError) as exc:
        C.connect("root@h", agent_id="alex")
    assert "predates client enrollment" in str(exc.value)
    assert "git pull" in str(exc.value)


def test_unreachable_host_is_plain(home, monkeypatch):
    monkeypatch.setattr(C, "_ssh", lambda *a, **k: (255, "Permission denied (publickey)."))
    with pytest.raises(C.ConnectError, match="cannot ssh"):
        C.connect("root@h", agent_id="alex")
