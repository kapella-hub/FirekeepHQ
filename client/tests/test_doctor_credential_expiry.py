from __future__ import annotations

import configparser
from datetime import datetime, timedelta, timezone

from firekeep_client.cli import _check_credential_expiry


def cfg(expires=None):
    value = configparser.ConfigParser()
    value["server"] = {"kind": "ports"}
    if expires:
        value["server"]["credential_expires_at"] = expires
    return value


def test_absent_is_silent_for_legacy_keys():
    assert _check_credential_expiry(cfg()) is None


def test_inside_fourteen_days_warns():
    expires = (datetime.now(timezone.utc) + timedelta(days=13)).isoformat()
    assert _check_credential_expiry(cfg(expires))[1] == "warn"


def test_expired_fails():
    expires = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert _check_credential_expiry(cfg(expires))[1] == "fail"


def test_healthy_is_ok():
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    assert _check_credential_expiry(cfg(expires))[1] == "ok"
