from __future__ import annotations

import json
import os
import stat

import pytest

from firekeep_client import join as J
from firekeep_client.transport import TransportError
from test_joincode import encode, payload


@pytest.fixture
def home(tmp_path, monkeypatch):
    config = tmp_path / ".firekeep" / "config"
    monkeypatch.setenv("FIREKEEP_CONFIG", str(config))
    monkeypatch.setattr(J.resolver, "is_bypassed", lambda: False)
    monkeypatch.setattr("firekeep_client.cli.run_doctor", lambda: [("doctor", "ok", "ready")])
    return config


def response():
    return {
        "device_id": "2" * 16,
        "credential_id": "1" * 16,
        "suggested_agent_id": "bob-laptop",
        "scopes": ["memory:read"],
        "kind": "ports",
        "host": "firekeep.example",
        "credential_expires_at": "2026-10-29T00:00:00+00:00",
        "server_version": "1.0.0",
    }


def test_pending_exists_before_network_and_is_deleted_after_config(home, monkeypatch, capsys):
    observed = []

    def get(url, **kwargs):
        pending = J.pending_path(home)
        observed.append((url, pending.exists()))
        return {"ok": True}

    monkeypatch.setattr(J, "get_json", get)
    monkeypatch.setattr(J, "post_json", lambda *a, **k: response())
    assert J.join(encode(payload())) == 0
    assert observed and all(exists for _, exists in observed)
    assert not J.pending_path(home).exists()
    text = capsys.readouterr().out
    assert "nxs_" not in text
    assert "credential " + "1" * 16 in text
    if os.name != "nt":
        assert stat.S_IMODE(home.stat().st_mode) == 0o600


def test_member_code_accepts_membership_then_runs_shared_device_join(home, monkeypatch, capsys):
    member_payload = payload()
    member_payload["m"] = member_payload.pop("q")
    member_code = encode(member_payload, prefix="fk_member_")
    calls = []

    monkeypatch.setattr(J, "get_json", lambda *a, **k: {"ok": True})

    def post(url, body, **kwargs):
        calls.append(url)
        if url.endswith("/members/invites/accept"):
            return {
                "membership": {"label": "Ada"},
                "entitlement": {"plan": "team"},
                "join_code": encode(payload()),
            }
        return response()

    monkeypatch.setattr(J, "post_json", post)
    assert J.join(member_code) == 0
    assert calls[0].endswith("/members/invites/accept")
    assert calls[1].endswith("/enroll")
    assert "member invite accepted for Ada — Team workspace" in capsys.readouterr().out


def test_failed_probe_sends_no_ticket_and_changes_no_config(home, monkeypatch):
    posts = []
    monkeypatch.setattr(J, "get_json", lambda *a, **k: (_ for _ in ()).throw(
        TransportError("offline")
    ))
    monkeypatch.setattr(J, "post_json", lambda *a, **k: posts.append(a))
    with pytest.raises(J.JoinError, match="NOT redeemed"):
        J.join(encode(payload()))
    assert posts == []
    assert not home.exists()
    assert J.pending_path(home).exists()


def test_resume_reuses_exact_secret_after_lost_response(home, monkeypatch):
    monkeypatch.setattr(J, "get_json", lambda *a, **k: {"ok": True})
    hashes = []

    def fail_post(url, body, **kwargs):
        hashes.append(body["credential_hash"])
        raise TransportError("response lost")

    monkeypatch.setattr(J, "post_json", fail_post)
    with pytest.raises(J.JoinError):
        J.join(encode(payload()))
    stored = json.loads(J.pending_path(home).read_text(encoding="utf-8"))["secret"]

    def succeed(url, body, **kwargs):
        hashes.append(body["credential_hash"])
        return response()

    monkeypatch.setattr(J, "post_json", succeed)
    assert J.join(encode(payload()), resume=True) == 0
    assert hashes[0] == hashes[1]
    assert stored.startswith("nxs_")


def test_personal_mode_refuses_before_even_parsing(monkeypatch):
    monkeypatch.setattr(J.resolver, "is_bypassed", lambda: True)
    with pytest.raises(J.JoinError, match="personal mode"):
        J.join("not a code")


def test_http_warning_is_explicit(home, monkeypatch, capsys):
    monkeypatch.setattr(J, "get_json", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(J, "post_json", lambda *a, **k: response())
    J.join(encode(payload()))
    assert "X-API-Key" in capsys.readouterr().out


def test_print_key_is_opt_in(home, monkeypatch, capsys):
    monkeypatch.setattr(J, "get_json", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(J, "post_json", lambda *a, **k: response())
    J.join(encode(payload()), print_key=True)
    assert "credential: nxs_" in capsys.readouterr().out


def test_anchor_mismatch_never_posts_ticket(home, monkeypatch):
    tls_payload = payload(t="tls", f="A" * 22)
    anchor_calls = []
    posts = []
    monkeypatch.setattr(J, "get_json", lambda *a, **k: (
        anchor_calls.append(a) or {"ca_pem": "wrong-ca"}
    ))
    monkeypatch.setattr(J, "post_json", lambda *a, **k: posts.append(a))
    with pytest.raises(J.JoinError, match="IDENTITY MISMATCH"):
        J.join(encode(tls_payload))
    assert anchor_calls
    assert posts == []


def test_tunnel_code_reuses_running_tunnel(home, monkeypatch):
    tunnel_payload = payload(t="tunnel", s="root@firekeep.example", h="127.0.0.1")
    monkeypatch.setattr("firekeep_client.connect._tunnel_running", lambda: True)
    started = []
    monkeypatch.setattr("firekeep_client.connect._start_tunnel", lambda target: started.append(target))
    monkeypatch.setattr(J, "get_json", lambda *a, **k: {"ok": True})
    tunnel_response = response()
    tunnel_response["host"] = "127.0.0.1"
    monkeypatch.setattr(J, "post_json", lambda *a, **k: tunnel_response)
    assert J.join(encode(tunnel_payload)) == 0
    assert started == []


@pytest.mark.parametrize(
    ("status", "message", "is_json", "expected_exit"),
    [
        (404, '{"detail":"does not recognise"}', True, 3),
        (404, "Not Found", False, 6),
        (409, '{"detail":"already redeemed"}', True, 3),
        (409, '{"detail":"AUTH_ENABLED=false"}', True, 6),
        (429, '{"detail":"rate limit"}', True, 6),
        (500, '{"detail":"privileges the server refuses"}', True, 5),
    ],
)
def test_enrollment_error_exit_codes(
    home, monkeypatch, status, message, is_json, expected_exit
):
    monkeypatch.setattr(J, "get_json", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        J,
        "post_json",
        lambda *a, **k: (_ for _ in ()).throw(
            TransportError(message, status=status, response_is_json=is_json)
        ),
    )
    with pytest.raises(J.JoinError) as error:
        J.join(encode(payload()))
    assert error.value.exit_code == expected_exit
    if status == 404 and not is_json:
        assert "predates client enrollment" in str(error.value)


def test_tunnel_setup_failure_is_actionable_exit_7(home, monkeypatch):
    from firekeep_client import connect

    tunnel_payload = payload(t="tunnel", s="root@firekeep.example", h="127.0.0.1")
    monkeypatch.setattr(connect, "_tunnel_running", lambda: False)
    monkeypatch.setattr(
        connect,
        "_start_tunnel",
        lambda target: (_ for _ in ()).throw(connect.ConnectError("ssh unavailable")),
    )
    with pytest.raises(J.JoinError, match="ssh unavailable") as error:
        J.join(encode(tunnel_payload))
    assert error.value.exit_code == 7
