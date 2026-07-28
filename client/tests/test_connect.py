"""`firekeep connect` — the decisions it makes, not the ssh it shells out to.

Every case here is one of the nine manual steps that made the real onboarding
unusable. `_ssh` is stubbed so the DECISIONS are under test: does it detect a
loopback binding, does it reuse an existing tunnel instead of stacking another,
does it diagnose a server too old to mint a key, does it refuse coherently when
told not to tunnel against a server that requires one.
"""
from __future__ import annotations

import configparser

import pytest

from firekeep_client import connect as C


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / ".firekeep" / "config"))
    return tmp_path


def _fake_ssh(*, bind_addr="127.0.0.1", has_local_mint=True, key="fk_testkey123"):
    """A scripted server. Returns (rc, output) per remote command shape."""
    def run(target, remote_cmd, *, timeout=60):
        if "FIREKEEP_SSH_OK" in remote_cmd:
            return 0, "FIREKEEP_SSH_OK\n"
        if "docker-compose.yml" in remote_cmd:
            return 0, "/opt/Firekeep\n"
        if "COMMIT=" in remote_cmd:
            return 0, f"COMMIT=abc1234\nBIND_ADDR={bind_addr}\nAUTH_ENABLED=true\n"
        if "keys create" in remote_cmd:
            if has_local_mint:
                return 0, '{"agent_id": "a", "key_id": "kid", "api_key": "%s"}\n' % key
            return 1, ""                       # old server: prompts, gets EOF, says nothing
        if "grep -c mint_local" in remote_cmd:
            return 0, ("1\n" if has_local_mint else "0\n")
        return 0, ""
    return run


def _read_cfg(home):
    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp.read(home / ".firekeep" / "config", encoding="utf-8")
    return cp


class TestTunnelDecision:
    def test_loopback_server_reuses_a_running_tunnel(self, home, monkeypatch):
        """Re-running connect must never stack a second forwarder on a working one."""
        monkeypatch.setattr(C, "_ssh", _fake_ssh())
        monkeypatch.setattr(C, "_tunnel_running", lambda: True)
        started = []
        monkeypatch.setattr(C, "_start_tunnel", lambda t: started.append(t))
        monkeypatch.setattr("firekeep_client.cli.run_doctor", lambda: [("x", "ok", "")])

        assert C.connect("root@h", profile="personal", agent_id="alex") == 0
        assert started == [], "started a second tunnel over a working one"
        assert _read_cfg(home)["personal"]["host"] == "127.0.0.1"

    def test_loopback_server_starts_a_tunnel_when_none_is_running(self, home, monkeypatch):
        monkeypatch.setattr(C, "_ssh", _fake_ssh())
        monkeypatch.setattr(C, "_tunnel_running", lambda: False)
        started = []
        monkeypatch.setattr(C, "_start_tunnel", lambda t: started.append(t))
        monkeypatch.setattr("firekeep_client.cli.run_doctor", lambda: [("x", "ok", "")])

        C.connect("root@h", profile="personal", agent_id="alex")
        assert started == ["root@h"]

    def test_public_server_needs_no_tunnel_and_uses_the_real_host(self, home, monkeypatch):
        """A server NOT bound to loopback must skip the tunnel entirely — the point
        of probing rather than assuming."""
        monkeypatch.setattr(C, "_ssh", _fake_ssh(bind_addr="0.0.0.0"))
        started = []
        monkeypatch.setattr(C, "_start_tunnel", lambda t: started.append(t))
        monkeypatch.setattr("firekeep_client.cli.run_doctor", lambda: [("x", "ok", "")])

        C.connect("root@203.0.113.9", profile="personal", agent_id="alex")
        assert started == []
        assert _read_cfg(home)["personal"]["host"] == "203.0.113.9"

    def test_no_tunnel_against_a_loopback_server_fails_with_the_reason(self, home, monkeypatch):
        monkeypatch.setattr(C, "_ssh", _fake_ssh())
        with pytest.raises(C.ConnectError, match="loopback"):
            C.connect("root@h", agent_id="alex", use_tunnel=False)


class TestKeyProvisioning:
    def test_the_minted_key_lands_in_the_profile(self, home, monkeypatch):
        """`install` used to finish 'successfully' with no api_key, so every call
        401'd. The whole point of connect is that this cannot happen."""
        monkeypatch.setattr(C, "_ssh", _fake_ssh(key="fk_theminted"))
        monkeypatch.setattr(C, "_tunnel_running", lambda: True)
        monkeypatch.setattr("firekeep_client.cli.run_doctor", lambda: [("x", "ok", "")])

        C.connect("root@h", profile="personal", agent_id="alex")
        cfg = _read_cfg(home)["personal"]
        assert cfg["api_key"] == "fk_theminted"
        assert cfg["agent_id"] == "alex"

    def test_a_server_too_old_to_mint_says_so_and_says_how_to_fix_it(self, home, monkeypatch):
        """The failure that actually happened on the live server. An empty error
        here is the same dead end connect exists to remove."""
        monkeypatch.setattr(C, "_ssh", _fake_ssh(has_local_mint=False))
        monkeypatch.setattr(C, "_tunnel_running", lambda: True)
        with pytest.raises(C.ConnectError) as exc:
            C.connect("root@h", agent_id="alex")
        msg = str(exc.value)
        assert "predates local key minting" in msg
        assert "git pull" in msg, "an error must name the remedy, not just the fault"


class TestProbeFailures:
    def test_unreachable_host_is_reported_plainly(self, home, monkeypatch):
        monkeypatch.setattr(C, "_ssh", lambda *a, **k: (255, "Permission denied (publickey)."))
        with pytest.raises(C.ConnectError, match="cannot ssh"):
            C.connect("root@h", agent_id="alex")

    def test_missing_server_install_names_where_it_looked(self, home, monkeypatch):
        def run(target, remote_cmd, *, timeout=60):
            return (0, "FIREKEEP_SSH_OK\n") if "FIREKEEP_SSH_OK" in remote_cmd else (0, "")
        monkeypatch.setattr(C, "_ssh", run)
        with pytest.raises(C.ConnectError) as exc:
            C.connect("root@h", agent_id="alex")
        assert "/opt/Firekeep" in str(exc.value)
        assert "--remote-dir" in str(exc.value)
