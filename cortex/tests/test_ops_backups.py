"""Backup status (member) and backup download (admin) over the read-only mount.

The split is the whole design (spec §3). Status reveals existence and age so
`firekeep doctor` and the dashboard can nag about a stale backup; download
streams raw volume tars that contain every member's private corpus and the
deployment's `.env` — VAULT_KEY included — and is admin-only, permanently.

Wires the REAL router behind the REAL FirekeepKeyAuthMiddleware (the mini-app
pattern from test_dashboard_auth.py) so these are genuine ASGI requests through
the actual scope checks, not assertions about decorators.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.ops_backups import create_ops_backups_router
from auth import keys
from auth.asgi import FirekeepKeyAuthMiddleware

SKIP_PREFIXES = ("/health", "/version", "/docs", "/redoc", "/openapi.json")


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest_asyncio.fixture
async def member_key(redis):
    """A teammate key: authenticated, deliberately NOT admin."""
    await keys.init_auth(redis_client=redis, enabled=True)
    key = await keys.create_key("teammate", ["memory:read"])
    yield key["api_key"]
    await keys.init_auth(redis_client=None, enabled=False)


@pytest_asyncio.fixture
async def admin_key(redis, member_key):
    key = await keys.create_key("owner", ["admin"])
    return key["api_key"]


def _app(redis) -> FastAPI:
    app = FastAPI()
    app.include_router(create_ops_backups_router())
    app.add_middleware(
        FirekeepKeyAuthMiddleware,
        enabled=True,
        redis_url="redis://unused/7",
        redis_client=redis,
        skip_paths=SKIP_PREFIXES,
        skip_exact_paths=(),
    )
    return app


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _make_backup(root: Path, stamp: str, *, indexed: bool = True,
                 payload: bytes = b"tarball-bytes") -> Path:
    """Write a backup directory shaped exactly like backup-cron.sh writes one."""
    d = root / f"firekeep-backup-{stamp}"
    d.mkdir(parents=True)
    (d / "neo4j_data.tar.gz").write_bytes(payload)
    (d / "env").write_text("VAULT_KEY=DUMMY_VAULT_KEY_FOR_TESTS\n", encoding="utf-8")
    if indexed:
        files = []
        total = 0
        for name in ("neo4j_data.tar.gz", "env"):
            data = (d / name).read_bytes()
            files.append({"name": name,
                          "sha256": hashlib.sha256(data).hexdigest(),
                          "bytes": len(data)})
            total += len(data)
        (d / "manifest.json").write_text(json.dumps({
            "stamp": stamp, "mode": "cold", "commit": "abc123",
            "sensitive": True, "files": files, "total_bytes": total,
        }), encoding="utf-8")
    return d


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


@pytest.fixture
def backups_dir(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "backups"
    root.mkdir()
    monkeypatch.setenv("FIREKEEP_BACKUPS_DIR", str(root))
    return root


# --- Status: member-readable -------------------------------------------------

class TestStatusIsMemberReadable:
    @pytest.mark.asyncio
    async def test_keyless_401(self, redis, member_key, backups_dir):
        async with _client(_app(redis)) as c:
            resp = await c.get("/ops/backups")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_member_key_200(self, redis, member_key, backups_dir):
        """A teammate key must read this: it powers `firekeep doctor`'s backup
        row, and a row only the owner can see nags nobody."""
        _make_backup(backups_dir, _stamp(datetime.now(timezone.utc)))
        async with _client(_app(redis)) as c:
            resp = await c.get("/ops/backups", headers={"X-API-Key": member_key})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["enabled"] is True
        assert len(body["backups"]) == 1
        assert body["policy"]

    @pytest.mark.asyncio
    async def test_status_never_reveals_file_names(self, redis, member_key, backups_dir):
        """Existence and age only (spec §3). Archive contents are admin
        territory, and a listing of them is the map to it."""
        _make_backup(backups_dir, _stamp(datetime.now(timezone.utc)))
        async with _client(_app(redis)) as c:
            resp = await c.get("/ops/backups", headers={"X-API-Key": member_key})
        assert "neo4j_data.tar.gz" not in resp.text
        assert "env" not in resp.json()["backups"][0]

    @pytest.mark.asyncio
    async def test_indexed_backup_reports_mode_size_and_age(
        self, redis, member_key, backups_dir
    ):
        taken = datetime.now(timezone.utc) - timedelta(hours=5)
        _make_backup(backups_dir, _stamp(taken))
        async with _client(_app(redis)) as c:
            resp = await c.get("/ops/backups", headers={"X-API-Key": member_key})
        entry = resp.json()["backups"][0]
        assert entry["indexed"] is True
        assert entry["mode"] == "cold"
        assert entry["total_bytes"] > 0
        # ~5h, allowing for clock granularity and test runtime.
        assert 17_000 < entry["age_seconds"] < 19_000

    @pytest.mark.asyncio
    async def test_unindexed_backup_is_listed_not_hidden(
        self, redis, member_key, backups_dir
    ):
        """update.sh's ad-hoc archives have no manifest. They are real backups
        and retention will never delete them, so hiding them would make the
        dashboard understate what the operator actually has."""
        _make_backup(backups_dir, "20260817T090000Z", indexed=False)
        async with _client(_app(redis)) as c:
            resp = await c.get("/ops/backups", headers={"X-API-Key": member_key})
        entry = resp.json()["backups"][0]
        assert entry["indexed"] is False
        assert entry["stamp"] == "20260817T090000Z"
        assert entry["age_seconds"] >= 0
        assert entry["mode"] is None
        assert entry["total_bytes"] is None

    @pytest.mark.asyncio
    async def test_backups_are_sorted_newest_first(self, redis, member_key, backups_dir):
        for stamp in ("20260816T043000Z", "20260818T043000Z", "20260817T043000Z"):
            _make_backup(backups_dir, stamp)
        async with _client(_app(redis)) as c:
            resp = await c.get("/ops/backups", headers={"X-API-Key": member_key})
        stamps = [b["stamp"] for b in resp.json()["backups"]]
        assert stamps == ["20260818T043000Z", "20260817T043000Z", "20260816T043000Z"]

    @pytest.mark.asyncio
    async def test_absent_directory_reports_disabled_not_error(
        self, redis, member_key, tmp_path, monkeypatch
    ):
        """No mount, or a deployment that has never run the nightly: the answer
        is "no backups yet", not a 500 the dashboard has to interpret."""
        monkeypatch.setenv("FIREKEEP_BACKUPS_DIR", str(tmp_path / "nope"))
        async with _client(_app(redis)) as c:
            resp = await c.get("/ops/backups", headers={"X-API-Key": member_key})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "enabled": False, "backups": [], "count": 0,
            "policy": resp.json()["policy"],
        }

    @pytest.mark.asyncio
    async def test_unrelated_directories_are_ignored(
        self, redis, member_key, backups_dir
    ):
        (backups_dir / "not-a-backup").mkdir()
        (backups_dir / "loose-file.txt").write_text("x", encoding="utf-8")
        _make_backup(backups_dir, "20260818T043000Z")
        async with _client(_app(redis)) as c:
            resp = await c.get("/ops/backups", headers={"X-API-Key": member_key})
        assert [b["stamp"] for b in resp.json()["backups"]] == ["20260818T043000Z"]

    @pytest.mark.asyncio
    async def test_a_corrupt_manifest_degrades_to_unindexed(
        self, redis, member_key, backups_dir
    ):
        """A half-written manifest must not 500 the status call — that would
        take out the doctor row and the dashboard card for every member."""
        d = _make_backup(backups_dir, "20260818T043000Z")
        (d / "manifest.json").write_text("{not json", encoding="utf-8")
        async with _client(_app(redis)) as c:
            resp = await c.get("/ops/backups", headers={"X-API-Key": member_key})
        assert resp.status_code == 200, resp.text
        assert resp.json()["backups"][0]["indexed"] is False


# --- Download: admin only ----------------------------------------------------

class TestDownloadIsAdminOnly:
    @pytest.mark.asyncio
    async def test_keyless_401(self, redis, member_key, backups_dir):
        _make_backup(backups_dir, "20260818T043000Z")
        async with _client(_app(redis)) as c:
            resp = await c.get("/ops/backups/20260818T043000Z/neo4j_data.tar.gz")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_member_key_403(self, redis, member_key, backups_dir):
        """The non-negotiable one. A raw volume tar is every member's private
        corpus; `env` is VAULT_KEY. A valid teammate key is not enough."""
        _make_backup(backups_dir, "20260818T043000Z")
        async with _client(_app(redis)) as c:
            resp = await c.get(
                "/ops/backups/20260818T043000Z/neo4j_data.tar.gz",
                headers={"X-API-Key": member_key},
            )
        assert resp.status_code == 403, resp.text

    @pytest.mark.asyncio
    async def test_member_key_403_for_the_env_file_too(
        self, redis, member_key, backups_dir
    ):
        _make_backup(backups_dir, "20260818T043000Z")
        async with _client(_app(redis)) as c:
            resp = await c.get(
                "/ops/backups/20260818T043000Z/env",
                headers={"X-API-Key": member_key},
            )
        assert resp.status_code == 403
        assert "VAULT_KEY" not in resp.text

    @pytest.mark.asyncio
    async def test_admin_key_streams_the_bytes(self, redis, admin_key, backups_dir):
        _make_backup(backups_dir, "20260818T043000Z", payload=b"the-real-tarball")
        async with _client(_app(redis)) as c:
            resp = await c.get(
                "/ops/backups/20260818T043000Z/neo4j_data.tar.gz",
                headers={"X-API-Key": admin_key},
            )
        assert resp.status_code == 200, resp.text
        assert resp.content == b"the-real-tarball"

    @pytest.mark.asyncio
    async def test_admin_can_fetch_the_manifest(self, redis, admin_key, backups_dir):
        """`firekeep backup pull` reads the manifest first — it is the list of
        files to fetch and the checksums to verify them against — and
        `firekeep backup link` proves a key is admin by asking for it."""
        _make_backup(backups_dir, "20260818T043000Z")
        async with _client(_app(redis)) as c:
            resp = await c.get(
                "/ops/backups/20260818T043000Z/manifest.json",
                headers={"X-API-Key": admin_key},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["sensitive"] is True

    @pytest.mark.asyncio
    async def test_a_file_not_in_the_manifest_is_refused(
        self, redis, admin_key, backups_dir
    ):
        """Resolution goes through the manifest, never a path join. A file that
        appeared in the directory by some other means is not servable."""
        d = _make_backup(backups_dir, "20260818T043000Z")
        (d / "id_rsa").write_text("not-in-the-manifest", encoding="utf-8")
        async with _client(_app(redis)) as c:
            resp = await c.get(
                "/ops/backups/20260818T043000Z/id_rsa",
                headers={"X-API-Key": admin_key},
            )
        assert resp.status_code == 404, resp.text

    @pytest.mark.asyncio
    async def test_an_unindexed_backup_is_not_downloadable(
        self, redis, admin_key, backups_dir
    ):
        """No manifest means no checksums, so nothing a pull could verify."""
        _make_backup(backups_dir, "20260817T090000Z", indexed=False)
        async with _client(_app(redis)) as c:
            resp = await c.get(
                "/ops/backups/20260817T090000Z/neo4j_data.tar.gz",
                headers={"X-API-Key": admin_key},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_unknown_stamp_404(self, redis, admin_key, backups_dir):
        async with _client(_app(redis)) as c:
            resp = await c.get(
                "/ops/backups/20991231T235959Z/manifest.json",
                headers={"X-API-Key": admin_key},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("attack", [
        "..%2f..%2f.env",
        "..%2fmanifest.json",
        "%2fetc%2fpasswd",
        "....//manifest.json",
    ])
    async def test_path_traversal_is_refused(
        self, redis, admin_key, backups_dir, attack
    ):
        _make_backup(backups_dir, "20260818T043000Z")
        (backups_dir.parent / ".env").write_text("VAULT_KEY=host-secret\n", encoding="utf-8")
        async with _client(_app(redis)) as c:
            resp = await c.get(
                f"/ops/backups/20260818T043000Z/{attack}",
                headers={"X-API-Key": admin_key},
            )
        assert resp.status_code != 200, resp.text
        assert "host-secret" not in resp.text

    @pytest.mark.asyncio
    async def test_traversal_in_the_stamp_is_refused(
        self, redis, admin_key, backups_dir
    ):
        _make_backup(backups_dir, "20260818T043000Z")
        async with _client(_app(redis)) as c:
            resp = await c.get(
                "/ops/backups/..%2f..%2fetc/passwd", headers={"X-API-Key": admin_key},
            )
        assert resp.status_code != 200


# --- The member ceiling ------------------------------------------------------

def test_no_backup_scope_exists():
    """No `backup:*` scope may ever be added to SCOPES.

    Since v1.0.0 an enrolled member is stamped ENROLLABLE_SCOPES = SCOPES −
    {admin, *} at validation time. Adding a scope here does not create a
    permission an owner can grant selectively — it hands it to every member of
    the workspace automatically. For an endpoint that serves raw volume tars
    and `.env`, a new scope IS admin-equivalence (spec §3), so the download
    route stays gated on `admin` and this test is the thing that keeps a
    well-meaning refactor from "fixing" the 403.
    """
    offenders = sorted(s for s in keys.SCOPES if "backup" in s.lower())
    assert offenders == [], (
        f"backup-ish scopes appeared in SCOPES: {offenders}. Every enrolled "
        "member would receive them automatically."
    )
    assert "admin" not in keys.ENROLLABLE_SCOPES
