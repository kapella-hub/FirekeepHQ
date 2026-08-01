"""Entitlements can add seats, never disable existing Firekeep data paths."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from auth.entitlements import sign_licence, verify_licence


NOW = datetime(2026, 7, 31, tzinfo=timezone.utc)


def _keys():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    encoded = base64.urlsafe_b64encode(public).decode().rstrip("=")
    return private, encoded


def _payload(**changes):
    payload = {
        "workspace_id": "workspace-a",
        "customer": "Acme",
        "plan": "team",
        "max_members": 5,
        "issued_at": (NOW - timedelta(days=1)).isoformat(),
        "expires_at": (NOW + timedelta(days=365)).isoformat(),
    }
    payload.update(changes)
    return payload


def test_valid_team_entitlement_is_verified_offline():
    private, public = _keys()
    entitlement = verify_licence(
        sign_licence(_payload(), private),
        "workspace-a",
        public_key=public,
        now=NOW,
    )
    assert entitlement.verified is True
    assert entitlement.plan == "team"
    assert entitlement.max_members == 5


def test_absent_malformed_unsigned_wrong_workspace_and_expired_degrade_to_solo():
    private, public = _keys()
    expired = sign_licence(
        _payload(expires_at=(NOW - timedelta(seconds=1)).isoformat()), private
    )
    wrong_workspace = sign_licence(_payload(workspace_id="workspace-b"), private)
    unsigned = "fk_lic_v1." + base64.urlsafe_b64encode(b"{}").decode().rstrip("=") + ".AA"
    for document in (None, "garbage", unsigned, wrong_workspace, expired):
        entitlement = verify_licence(
            document,
            "workspace-a",
            public_key=public,
            now=NOW,
        )
        assert entitlement.plan == "solo"
        assert entitlement.max_members == 1
        assert entitlement.verified is False


def test_expiry_warning_starts_inside_thirty_days():
    private, public = _keys()
    document = sign_licence(
        _payload(expires_at=(NOW + timedelta(days=29)).isoformat()), private
    )
    entitlement = verify_licence(
        document, "workspace-a", public_key=public, now=NOW
    )
    assert entitlement.verified is True
    assert "expires in" in (entitlement.warning or "")
