"""Public enrollment contract: one hash in, no plaintext credential out."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import fakeredis.aioredis
import httpx
import pytest

from app.enroll.api import _suggested_agent_id, create_enroll_router
from app.enroll.store import EnrollmentStore
from auth import keys


TICKET = "A" * 43
HASH = "a" * 64
NONCE = "b" * 16


class StubStore:
    def __init__(self, outcome="ok", fields=None, ticket=None):
        self.outcome = outcome
        self.fields = fields or ["0123456789abcdef", "fedcba9876543210"]
        self.ticket = ticket or {
            "agent_label": "bob",
            "scopes": '["memory:read","vault:read"]',
            "kind": "ports",
            "host": "firekeep.example",
            "issuer": "alice",
            "issued_credential_expires_at": "2026-10-29T00:00:00+00:00",
        }
        self.consume_calls = 0

    async def consume(self, **kwargs):
        self.consume_calls += 1
        return self.outcome, self.fields, self.ticket

    async def anchor(self, tid):
        return "-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----" if tid == "a" * 16 else None

    async def list_outstanding(self):
        return []

    async def cancel(self, tid):
        return False


def client(store=None, *, auth_enabled=True):
    app = FastAPI()
    app.include_router(create_enroll_router(store=store or StubStore(), auth_enabled=auth_enabled))
    return TestClient(app)


def request_body(**changes):
    body = {
        "ticket": TICKET,
        "credential_hash": HASH,
        "device_nonce": NONCE,
        "hostname": "BOB-MBP.example",
    }
    body.update(changes)
    return body


def test_suggested_agent_id_slugifies_a_human_device_label():
    assert _suggested_agent_id(
        {"agent_label": "Bob's Laptop"}, "BOB-MBP.local"
    ) == "bob-s-laptop-bob-mbp"


def test_happy_path_shape_and_no_api_key():
    response = client().post("/enroll", json=request_body())
    assert response.status_code == 200
    assert response.json() == {
        "device_id": "fedcba9876543210",
        "credential_id": "0123456789abcdef",
        "suggested_agent_id": "bob-bob-mbp",
        "scopes": ["memory:read", "vault:read"],
        "kind": "ports",
        "host": "firekeep.example",
        "credential_expires_at": "2026-10-29T00:00:00+00:00",
        "server_version": response.json()["server_version"],
        "replay": False,
    }
    assert "api_key" not in response.json()


def test_malformed_hash_is_rejected_before_store():
    store = StubStore()
    response = client(store).post("/enroll", json=request_body(credential_hash="A" * 64))
    assert response.status_code == 400
    assert "malformed credential fingerprint" in response.json()["detail"]
    assert store.consume_calls == 0


def test_auth_off_resolves_to_enrollment_specific_409():
    response = client(auth_enabled=False).post("/enroll", json=request_body())
    assert response.status_code == 409
    assert "AUTH_ENABLED=false" in response.json()["detail"]


def test_idempotent_retry_is_successful_and_identified():
    store = StubStore(outcome="replay", fields=[
        "0123456789abcdef", "fedcba9876543210", "2026-07-31T00:00:00+00:00"
    ])
    response = client(store).post("/enroll", json=request_body())
    assert response.status_code == 200
    assert response.json()["replay"] is True
    assert response.json()["credential_id"] == "0123456789abcdef"


def test_distinct_failure_bodies_name_the_cause():
    cases = [
        ("unknown", [], 404, "does not recognise"),
        ("used", ["2026-07-31T00:00:00+00:00", "0123456789abcdef"], 409, "already redeemed"),
        ("expired", ["2026-07-30T00:00:00+00:00"], 410, "expired at"),
        ("cred_exists", [], 409, "already registered"),
        ("credential_gone", ["2026-07-31T00:00:00+00:00", "0123456789abcdef"], 409, "no longer present"),
        ("rate", [], 429, "rate limit"),
        ("scope_violation", ['["admin"]'], 500, "privileges"),
    ]
    for outcome, fields, status, phrase in cases:
        response = client(StubStore(outcome=outcome, fields=fields)).post(
            "/enroll", json=request_body()
        )
        assert response.status_code == status, outcome
        assert phrase in response.json()["detail"], outcome
        assert "api_key" not in response.json(), outcome


def test_anchor_returns_only_public_ca():
    good = client().get("/enroll/anchor", params={"tid": "a" * 16})
    assert good.status_code == 200
    assert set(good.json()) == {"ca_pem"}
    assert client().get("/enroll/anchor", params={"tid": "b" * 16}).status_code == 404


def test_auth_enroll_is_not_a_route():
    assert client().post("/auth/enroll", json=request_body()).status_code == 404


@pytest.mark.asyncio
async def test_admin_invite_mints_a_client_decodable_single_use_code(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=redis, enabled=True)
    admin = await keys.create_key("dashboard", ["*"])
    monkeypatch.setenv("VPS_IP", "203.0.113.9")
    app = FastAPI()
    app.include_router(
        create_enroll_router(store=EnrollmentStore(redis), auth_enabled=True)
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            response = await http.post(
                "/enroll/invite",
                headers={"X-API-Key": admin["api_key"]},
                json={"agent": "bob", "expires_days": 90},
            )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["code"].startswith("fk_join_")
        assert "api_key" not in data
        assert "FIREKEEP_JOIN=" in data["install_command_sh"]
        record = await redis.hgetall("auth:enroll:" + data["tid"])
        assert record["agent_label"] == "bob"
        assert record["ssh_target"] == "root@203.0.113.9"
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
        await redis.aclose()
