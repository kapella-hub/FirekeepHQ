"""Enrolled credentials track the CURRENT member ceiling, not their mint-day one.

Found live (2026-08-17), not in review: the first real `docdex sync` from an
enrolled workstation answered 403 — "source names under 'docdex:' are
reserved: requires scope 'dex:docdex' or 'admin'". The key was enrolled before
Phase V added `dex:docdex` to SCOPES, and enrollment stamps the scope list at
MINT time — so every key enrolled before a scope exists lacks it forever, and
every future dex scope re-creates the same failure for every existing install.

The fix is semantic, not a migration: the enrollment path never narrows —
every enrolled key is stamped the full `ENROLLABLE_SCOPES` of its day
(enroll/store.py), so the credential's CONTRACT is "member ceiling". The
ceiling is therefore applied at validation time for records carrying the
server-stamped `enrolled_via` marker. Admin-minted keys never carry the
marker and keep exactly the scopes they were minted with — narrowing a key
via /auth/keys still works, and no path widens anything to `admin` or `*`
(both are excluded from ENROLLABLE_SCOPES by construction, guarded here).
"""
from __future__ import annotations

import json

import fakeredis.aioredis
import pytest

from auth import keys


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _store(redis, api_key: str, record: dict):
    return redis.hset(f"{keys._KEY_PREFIX}{keys._hash_key(api_key)}", mapping=record)


def _record(scopes: list[str], **extra) -> dict:
    base = {
        "key_id": "k1",
        "credential_id": "k1",
        "member_id": "m1",
        "scopes": json.dumps(scopes),
        "created_at": "2026-07-01T00:00:00+00:00",
    }
    base.update(extra)
    return base


class TestEnrolledCeiling:
    @pytest.mark.asyncio
    async def test_pre_phase_v_enrolled_key_gains_the_new_dex_scope(self, redis):
        """The live 403: an enrolled key stamped before `dex:docdex` existed
        must validate WITH it — the record models a real pre-Phase-V mint."""
        old_ceiling = sorted(keys.ENROLLABLE_SCOPES - {"dex:docdex"})
        await _store(redis, "nxs_old", _record(old_ceiling, enrolled_via="tid123"))
        identity = await keys.validate_key("nxs_old", redis_client=redis)
        assert identity is not None
        assert "dex:docdex" in identity["scopes"]
        assert set(identity["scopes"]) >= keys.ENROLLABLE_SCOPES

    @pytest.mark.asyncio
    async def test_admin_minted_key_keeps_exactly_its_minted_scopes(self, redis):
        """No `enrolled_via` marker → a deliberately narrowed key stays narrow.
        This is the property that keeps /auth/keys able to mint least-privilege
        credentials at all."""
        await _store(redis, "nxs_narrow", _record(["vault:read"]))
        identity = await keys.validate_key("nxs_narrow", redis_client=redis)
        assert identity is not None
        assert identity["scopes"] == ["vault:read"]

    @pytest.mark.asyncio
    async def test_the_ceiling_never_adds_admin_or_star(self, redis):
        """The upgrade is a union with ENROLLABLE_SCOPES, which excludes
        `admin` and `*` by construction (test_enrollable_scopes.py) — assert it
        end-to-end anyway, because this is the line that would make the upgrade
        a privilege escalation instead of a contract."""
        await _store(redis, "nxs_e", _record(["vault:read"], enrolled_via="tid9"))
        identity = await keys.validate_key("nxs_e", redis_client=redis)
        assert "admin" not in identity["scopes"]
        assert "*" not in identity["scopes"]

    @pytest.mark.asyncio
    async def test_enrolled_scopes_are_sorted_and_deduplicated(self, redis):
        """Deterministic output: downstream code compares and logs scope lists;
        a set-ordered list would churn on every read."""
        await _store(redis, "nxs_s", _record(["vault:read", "vault:read"], enrolled_via="t"))
        identity = await keys.validate_key("nxs_s", redis_client=redis)
        assert identity["scopes"] == sorted(set(identity["scopes"]))

    @pytest.mark.asyncio
    async def test_expired_enrolled_key_still_refused(self, redis):
        """The ceiling upgrade must not touch expiry — an expired enrolled key
        is refused before scopes are even considered."""
        await _store(
            redis, "nxs_x",
            _record(sorted(keys.ENROLLABLE_SCOPES), enrolled_via="t",
                    expires_at="2020-01-01T00:00:00+00:00"),
        )
        assert await keys.validate_key("nxs_x", redis_client=redis) is None
