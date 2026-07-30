# Key-ID Resolution Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `revoke_key` and `list_keys` resolve a short `key_id` by verifying the record's own stored id instead of guessing at the first glob match, so a prefix ambiguity fails loudly rather than deleting the wrong credential or hiding a live one.

**Architecture:** Both functions currently do `scan_iter(f"auth:key:{key_id}*")` and act on the first result. A single new private helper resolves a `key_id` to *every* record whose **stored** `key_id` field equals the requested value. `revoke_key` then refuses when that set has more than one member; `list_keys` emits all of them and marks the set ambiguous. No storage format changes, no migration, no client changes.

**Tech Stack:** Python 3.11, `redis.asyncio`, `pytest` + `pytest-asyncio` + `fakeredis.aioredis`. Unit tests live in `auth/tests/`; the one route-level test lives in `cortex/tests/` because it needs a minted admin key and the auth middleware (see Task 3).

## Global Constraints

- **`auth/` only.** No client changes, no storage migration, no change to `key_id`'s derivation (`key_hash[:16]`), the `auth:key_index` member format, or the record layout. `2026-07-30-client-enrollment-join-codes-design.md` replaces the representation later; this change makes the *current* one behave correctly.
- **`auth/keys.py` must stay fastapi-free.** `bridge/requirements.txt` ships no fastapi and `auth/asgi.py` depends on this module importing there. `auth/tests/test_keys.py::TestFastapiFree` enforces it — do not import from `auth.api` or `fastapi` in `keys.py`.
- **`validate_key` is not touched.** It looks records up by full hash and is already correct. Its behaviour is the fixed point every test measures against.
- **Redis client access:** `list_keys` and `revoke_key` use the module-global `_redis` set by `init_auth()`. Keep that; do not add an explicit-client seam (the enrollment spec explicitly declines to add one).
- Tests use the existing fixture style: `fakeredis.aioredis.FakeRedis(decode_responses=True)`, then `await keys.init_auth(redis_client=redis, enabled=True)`, and reset with `await keys.init_auth(redis_client=None, enabled=False)`.
- Run tests with: `cd auth && python -m pytest tests/ -v` (Tasks 1, 2, 4) and
  `cd cortex && python -m pytest tests/test_auth_consolidation.py -v` (Task 3).
- **Never edit an existing passing test to accommodate a change.** `revoke_key`'s
  single-match and unknown-id contracts are unchanged by design; if an existing test
  fails, the implementation broke a contract — re-read the test and fix the code.

---

### Task 1: Verified resolution, and `revoke_key` refuses ambiguity

**Files:**
- Modify: `auth/keys.py` (add `AmbiguousKeyIdError` and `_resolve_key_id` near `_KEY_INDEX` at line 65; rewrite `revoke_key` at lines 253-263)
- Test: `auth/tests/test_key_id_resolution.py` (create)

**Interfaces:**
- Produces: `AmbiguousKeyIdError(Exception)` with attributes `key_id: str` and `matches: list[str]` (the full Redis keys). `_resolve_key_id(key_id: str) -> list[tuple[str, dict[str, str]]]` returning `(redis_key, record)` pairs whose stored `key_id` field equals `key_id`; returns `[]` for an unsafe or unknown id. `revoke_key(key_id: str) -> bool` unchanged in signature, but now raises `AmbiguousKeyIdError` when more than one record verifies.
- Consumes: nothing.

- [ ] **Step 1: Write the failing tests**

Create `auth/tests/test_key_id_resolution.py`:

```python
"""Tests for key_id -> record resolution (2026-07-30 hardening spec).

Two credentials sharing a 16-hex key_id prefix cannot be produced by
create_key, so these tests write records into Redis directly. That is the
point: the defect is that resolution GUESSES, and the guess is only
observable when the guess has more than one candidate.
"""
from __future__ import annotations

import json

import fakeredis.aioredis
import pytest
import pytest_asyncio

from auth import keys


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=r, enabled=True)
    yield r
    await keys.init_auth(redis_client=None, enabled=False)
    await r.aclose()


async def _put(redis, key_hash: str, key_id: str, agent_id: str) -> None:
    """Write a credential record directly, bypassing create_key."""
    await redis.hset(
        f"auth:key:{key_hash}",
        mapping={
            "agent_id": agent_id,
            "scopes": json.dumps(["memory:read"]),
            "created_at": "2026-07-30T00:00:00+00:00",
            "key_id": key_id,
        },
    )
    await redis.zadd("auth:key_index", {key_id: 1.0})


SHARED = "a" * 16
HASH_A = SHARED + "1" * 48
HASH_B = SHARED + "2" * 48


class TestResolveKeyId:
    @pytest.mark.asyncio
    async def test_unknown_id_resolves_to_nothing(self, redis):
        assert await keys._resolve_key_id("deadbeefdeadbeef") == []

    @pytest.mark.asyncio
    async def test_single_record_resolves(self, redis):
        await _put(redis, HASH_A, SHARED, "alice")
        found = await keys._resolve_key_id(SHARED)
        assert len(found) == 1
        assert found[0][0] == f"auth:key:{HASH_A}"
        assert found[0][1]["agent_id"] == "alice"

    @pytest.mark.asyncio
    async def test_prefix_match_with_different_stored_id_is_not_a_match(self, redis):
        # Record's hash starts with SHARED, but its stored key_id says otherwise.
        # A prefix match is not an identity.
        await _put(redis, HASH_A, "ffffffffffffffff", "mallory")
        assert await keys._resolve_key_id(SHARED) == []

    @pytest.mark.asyncio
    async def test_collision_resolves_to_both(self, redis):
        await _put(redis, HASH_A, SHARED, "alice")
        await _put(redis, HASH_B, SHARED, "bob")
        found = await keys._resolve_key_id(SHARED)
        assert len(found) == 2

    @pytest.mark.asyncio
    async def test_glob_metacharacters_resolve_to_nothing(self, redis):
        """A key_id arrives from a URL path parameter. '*' must not scan
        the whole keyspace and delete an arbitrary record."""
        await _put(redis, HASH_A, SHARED, "alice")
        for hostile in ("*", "?", "a*", "[a-z]", "a" * 65, "AAAA", "zz"):
            assert await keys._resolve_key_id(hostile) == [], hostile


class TestRevokeKey:
    @pytest.mark.asyncio
    async def test_revokes_single_match(self, redis):
        await _put(redis, HASH_A, SHARED, "alice")
        assert await keys.revoke_key(SHARED) is True
        assert await redis.exists(f"auth:key:{HASH_A}") == 0
        assert await redis.zscore("auth:key_index", SHARED) is None

    @pytest.mark.asyncio
    async def test_unknown_id_returns_false(self, redis):
        assert await keys.revoke_key("deadbeefdeadbeef") is False

    @pytest.mark.asyncio
    async def test_ambiguity_deletes_nothing_and_raises(self, redis):
        await _put(redis, HASH_A, SHARED, "alice")
        await _put(redis, HASH_B, SHARED, "bob")
        with pytest.raises(keys.AmbiguousKeyIdError) as exc:
            await keys.revoke_key(SHARED)
        assert exc.value.key_id == SHARED
        assert len(exc.value.matches) == 2
        # Neither deleted, index intact.
        assert await redis.exists(f"auth:key:{HASH_A}") == 1
        assert await redis.exists(f"auth:key:{HASH_B}") == 1
        assert await redis.zscore("auth:key_index", SHARED) is not None

    @pytest.mark.asyncio
    async def test_ambiguity_leaves_both_credentials_valid(self, redis):
        await _put(redis, HASH_A, SHARED, "alice")
        await _put(redis, HASH_B, SHARED, "bob")
        with pytest.raises(keys.AmbiguousKeyIdError):
            await keys.revoke_key(SHARED)
        # validate_key looks up by FULL hash and must be unaffected.
        assert await keys.validate_key_by_hash(HASH_A) is not None
        assert await keys.validate_key_by_hash(HASH_B) is not None

    @pytest.mark.asyncio
    async def test_glob_metacharacter_id_revokes_nothing(self, redis):
        await _put(redis, HASH_A, SHARED, "alice")
        assert await keys.revoke_key("*") is False
        assert await redis.exists(f"auth:key:{HASH_A}") == 1
```

Add this helper to `auth/keys.py` so the tests can assert on `validate_key`'s
behaviour without knowing a plaintext (the records are written directly, so no
plaintext exists):

```python
async def validate_key_by_hash(key_hash: str, redis_client=None) -> dict[str, Any] | None:
    """validate_key's lookup half, keyed by an already-computed hash.

    Exists so tests and the enrollment design can ask "does this stored record
    still authenticate?" without possessing the plaintext. validate_key itself
    is unchanged and simply hashes first.
    """
    client = redis_client if redis_client is not None else _redis
    if client is None:
        return None
    data = await client.hgetall(f"{_KEY_PREFIX}{key_hash}")
    if not data:
        return None
    expires_at = data.get("expires_at")
    if expires_at:
        try:
            if datetime.now(timezone.utc) > datetime.fromisoformat(expires_at):
                return None
        except (ValueError, TypeError):
            pass
    return {
        "agent_id": data.get("agent_id", "unknown"),
        "scopes": json.loads(data.get("scopes", "[]")),
        "authenticated": True,
        "key_id": data.get("key_id", key_hash[:16]),
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd auth && python -m pytest tests/test_key_id_resolution.py -v`
Expected: FAIL — `AttributeError: module 'auth.keys' has no attribute '_resolve_key_id'` (and `AmbiguousKeyIdError`, `validate_key_by_hash`).

- [ ] **Step 3: Add the exception, the id guard, and the resolver**

In `auth/keys.py`, immediately after the `_KEY_INDEX` constant (line 65), add:

```python
import re

# A key_id reaches us from a URL path parameter and is interpolated into a
# Redis glob. Anything outside lowercase hex would either match nothing or --
# for '*', '?', '[' -- widen the scan to the whole keyspace, where "first
# match wins" becomes "delete an arbitrary credential and report success".
_KEY_ID_RE = re.compile(r"^[0-9a-f]{1,64}$")


class AmbiguousKeyIdError(Exception):
    """More than one stored record claims this key_id.

    Raised instead of acting on a guess. Under server-minted keys this is
    unreachable (it needs a 64-bit prefix collision); once a client supplies
    its own credential hash it becomes a choice, which is why resolution
    verifies rather than assumes.
    """

    def __init__(self, key_id: str, matches: list[str]) -> None:
        super().__init__(
            f"key_id {key_id!r} matches {len(matches)} stored records; refusing to guess"
        )
        self.key_id = key_id
        self.matches = matches


async def _resolve_key_id(key_id: str) -> list[tuple[str, dict[str, Any]]]:
    """Every record whose STORED key_id equals `key_id`.

    A prefix match is not an identity: the glob is only a way to narrow the
    scan, and each candidate is confirmed against its own key_id field.
    """
    if _redis is None or not _KEY_ID_RE.match(key_id):
        return []
    found: list[tuple[str, dict[str, Any]]] = []
    async for redis_key in _redis.scan_iter(f"{_KEY_PREFIX}{key_id}*", count=100):
        data = await _redis.hgetall(redis_key)
        if data and data.get("key_id") == key_id:
            found.append((redis_key, data))
    return found
```

- [ ] **Step 4: Rewrite `revoke_key`**

Replace `revoke_key` (lines 253-263) with:

```python
async def revoke_key(key_id: str) -> bool:
    """Revoke an API key by its short ID.

    Returns True if exactly one record verified and was deleted, False if none
    did. Raises AmbiguousKeyIdError when more than one verified: deleting one
    of two while reporting success is the defect this function used to have.
    """
    if _redis is None:
        return False

    matches = await _resolve_key_id(key_id)
    if not matches:
        return False
    if len(matches) > 1:
        logger.critical(
            "AMBIGUOUS key_id %s matches %d stored records (%s); refusing to revoke",
            key_id, len(matches), ", ".join(k for k, _ in matches),
        )
        raise AmbiguousKeyIdError(key_id, [k for k, _ in matches])

    redis_key, _ = matches[0]
    await _redis.delete(redis_key)
    await _redis.zrem(_KEY_INDEX, key_id)
    return True
```

Add `validate_key_by_hash` from Step 1 next to `validate_key`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd auth && python -m pytest tests/test_key_id_resolution.py -v`
Expected: PASS (10 tests).

- [ ] **Step 6: Run the whole auth suite for regressions**

Run: `cd auth && python -m pytest tests/ -v`
Expected: PASS. `revoke_key`'s single-match and unknown-id contracts are unchanged, so existing tests must not need edits. If one fails, the fix broke a contract — do not edit the existing test to match; re-read it and fix the implementation.

- [ ] **Step 7: Commit**

```bash
git add auth/keys.py auth/tests/test_key_id_resolution.py
git commit -m "fix(auth): revoke_key acted on the first glob match without checking it"
```

---

### Task 2: `list_keys` emits every verified match

**Files:**
- Modify: `auth/keys.py` (rewrite `list_keys`, lines 230-250)
- Test: `auth/tests/test_key_id_resolution.py` (append)

**Interfaces:**
- Consumes: `_resolve_key_id` from Task 1.
- Produces: `list_keys(limit: int = 50) -> list[dict[str, Any]]` where each row gains `ambiguous: bool`. Existing row keys (`key_id`, `agent_id`, `scopes`, `created_at`, `expires_at`) are unchanged, so `auth/api.py`'s response shape stays backward-compatible.

- [ ] **Step 1: Write the failing tests**

Append to `auth/tests/test_key_id_resolution.py`:

```python
class TestListKeys:
    @pytest.mark.asyncio
    async def test_lists_single_record(self, redis):
        await _put(redis, HASH_A, SHARED, "alice")
        rows = await keys.list_keys()
        assert len(rows) == 1
        assert rows[0]["agent_id"] == "alice"
        assert rows[0]["ambiguous"] is False

    @pytest.mark.asyncio
    async def test_collision_emits_every_record(self, redis):
        """A listing that hides a live credential is how one goes unnoticed."""
        await _put(redis, HASH_A, SHARED, "alice")
        await _put(redis, HASH_B, SHARED, "bob")
        rows = await keys.list_keys()
        assert len(rows) == 2
        assert {r["agent_id"] for r in rows} == {"alice", "bob"}
        assert all(r["ambiguous"] is True for r in rows)

    @pytest.mark.asyncio
    async def test_index_member_with_no_verified_record_is_skipped(self, redis):
        # Index entry whose record was deleted out from under it.
        await redis.zadd("auth:key_index", {"bbbbbbbbbbbbbbbb": 1.0})
        assert await keys.list_keys() == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd auth && python -m pytest tests/test_key_id_resolution.py::TestListKeys -v`
Expected: FAIL — `KeyError: 'ambiguous'` on the first test, and `test_collision_emits_every_record` returning 1 row instead of 2.

- [ ] **Step 3: Rewrite `list_keys`**

Replace `list_keys` (lines 230-250) with:

```python
async def list_keys(limit: int = 50) -> list[dict[str, Any]]:
    """List all API keys (metadata only, never plaintext).

    Where one index member resolves to more than one record, EVERY record is
    emitted and each is marked ambiguous. Under-reporting is the failure mode
    here: a credential hidden behind another's prefix still authenticates.
    """
    if _redis is None:
        return []

    key_ids = await _redis.zrevrange(_KEY_INDEX, 0, limit - 1)
    rows: list[dict[str, Any]] = []
    for kid in key_ids:
        matches = await _resolve_key_id(kid)
        if len(matches) > 1:
            logger.critical(
                "AMBIGUOUS key_id %s matches %d stored records (%s); listing all",
                kid, len(matches), ", ".join(k for k, _ in matches),
            )
        for _redis_key, data in matches:
            rows.append({
                "key_id": data.get("key_id", kid),
                "agent_id": data.get("agent_id", "unknown"),
                "scopes": json.loads(data.get("scopes", "[]")),
                "created_at": data.get("created_at"),
                "expires_at": data.get("expires_at"),
                "ambiguous": len(matches) > 1,
            })
    return rows
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd auth && python -m pytest tests/test_key_id_resolution.py -v`
Expected: PASS (13 tests — the 10 from Task 1 plus 3 new).

- [ ] **Step 5: Run the whole auth suite**

Run: `cd auth && python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add auth/keys.py auth/tests/test_key_id_resolution.py
git commit -m "fix(auth): a listing that hides a live credential behind a prefix"
```

---

### Task 3: `DELETE /auth/keys/{key_id}` answers 409 on ambiguity

**Files:**
- Modify: `auth/api.py` (import `AmbiguousKeyIdError`; `revoke_api_key`, lines 69-78)
- Test: `cortex/tests/test_auth_consolidation.py` (append) — **not** `auth/tests/`

**Interfaces:**
- Consumes: `AmbiguousKeyIdError` from Task 1.
- Produces: no new symbols. `DELETE /auth/keys/{key_id}` returns 409 with a `detail` naming the id and the count.

**Why the test lives in `cortex/tests/`:** the route is behind
`require_scope("admin")`. With `AUTH_ENABLED=false` that dependency **403s** — the
anonymous scope set is `SCOPES - {"admin", "*"}` and the disabled path now runs a real
membership check rather than passing through. So a bare `FastAPI()` + router app can
never reach the ambiguity branch; the test needs a real admin key and the auth
middleware. `cortex/tests/test_auth_consolidation.py` already has exactly that harness
(`redis` and `auth_env` fixtures, `_mini_cortex(redis)`, `_client(app)`), and
`test_admin_key_can_mint_keys` is the pattern to mirror.

- [ ] **Step 1: Write the failing test**

Append to `cortex/tests/test_auth_consolidation.py`, inside the class that holds
`test_admin_key_can_mint_keys`:

```python
    @pytest.mark.asyncio
    async def test_ambiguous_key_id_is_409_and_deletes_nothing(self, redis, auth_env):
        """A well-formed request against ambiguous server state is a 409, not a
        500 (which reads as the caller's fault) and not a 200 (which is the bug
        this whole change removes: deleting one of two and reporting success)."""
        import json as _json

        shared = "a" * 16
        hash_a, hash_b = shared + "1" * 48, shared + "2" * 48
        for h, who in ((hash_a, "alice"), (hash_b, "bob")):
            await redis.hset(f"auth:key:{h}", mapping={
                "agent_id": who,
                "scopes": _json.dumps(["memory:read"]),
                "created_at": "2026-07-30T00:00:00+00:00",
                "key_id": shared,
            })
        await redis.zadd("auth:key_index", {shared: 1.0})

        async with _client(_mini_cortex(redis)) as c:
            resp = await c.delete(
                f"/auth/keys/{shared}",
                headers={"X-API-Key": auth_env["admin"]},
            )

        assert resp.status_code == 409
        assert shared in resp.json()["detail"]
        assert await redis.exists(f"auth:key:{hash_a}") == 1
        assert await redis.exists(f"auth:key:{hash_b}") == 1
```

- [ ] **Step 1b: Confirm the harness names before running**

Run: `cd cortex && grep -n "def _mini_cortex\|def _client\|def auth_env" tests/test_auth_consolidation.py`
If any name differs, use what the file actually defines. Do not invent a signature.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd cortex && python -m pytest tests/test_auth_consolidation.py -k ambiguous -v`
Expected: FAIL — status 500, because `AmbiguousKeyIdError` propagates unhandled.

- [ ] **Step 3: Handle the exception in the route**

In `auth/api.py`, add `AmbiguousKeyIdError` to the existing `from auth.keys import (...)` block, then replace `revoke_api_key`:

```python
    @router.delete("/keys/{key_id}")
    async def revoke_api_key(
        key_id: str,
        identity: dict = Depends(require_scope("admin")),
    ) -> RevokeKeyResponse:
        """Revoke an API key by its short ID."""
        try:
            success = await revoke_key(key_id)
        except AmbiguousKeyIdError as exc:
            # 409, not 500: the request is well-formed and the server state is
            # the problem. Nothing was deleted.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Key {key_id} matches {len(exc.matches)} stored records. "
                    "Nothing was revoked — resolve the ambiguity in Redis before retrying."
                ),
            ) from exc
        if not success:
            raise HTTPException(status_code=404, detail=f"Key {key_id} not found")
        return RevokeKeyResponse(status="revoked", key_id=key_id)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd cortex && python -m pytest tests/test_auth_consolidation.py -k ambiguous -v`
Expected: PASS. Then the whole file, since it shares fixtures:
`cd cortex && python -m pytest tests/test_auth_consolidation.py -v`

- [ ] **Step 5: Confirm `auth/keys.py` is still fastapi-free**

Run: `cd auth && python -m pytest tests/test_keys.py::TestFastapiFree -v`
Expected: PASS. The exception is defined in `keys.py` and imported *by* `api.py`, never the reverse.

- [ ] **Step 6: Commit**

```bash
git add auth/api.py cortex/tests/test_auth_consolidation.py
git commit -m "feat(auth): an ambiguous revoke is a 409, not a silent success"
```

---

### Task 4: The subsystem invariant guard

**Files:**
- Test: `auth/tests/test_key_id_resolution.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: no new symbols. This is the test that fails on the pre-patch code and encodes why the change exists.

- [ ] **Step 1: Write the invariant test**

Append to `auth/tests/test_key_id_resolution.py`:

```python
class TestSubsystemInvariant:
    """No reachable sequence of revoke_key calls may leave a credential that
    validate_key accepts but list_keys does not show.

    This is the property the whole patch exists to establish. On the pre-patch
    code it fails: revoke_key deletes one of two colliding records, zrem's the
    single shared index member, and the survivor becomes invisible to
    list_keys forever while still authenticating.
    """

    @pytest.mark.asyncio
    async def test_no_revoke_sequence_orphans_a_live_credential(self, redis):
        await _put(redis, HASH_A, SHARED, "alice")
        await _put(redis, HASH_B, SHARED, "bob")
        await _put(redis, "c" * 64, "cccccccccccccccc", "carol")

        # Attempt every revocation an operator could reach from a listing.
        for kid in {r["key_id"] for r in await keys.list_keys()}:
            try:
                await keys.revoke_key(kid)
            except keys.AmbiguousKeyIdError:
                pass  # refusing is the correct outcome, not a failure

        listed = {r["key_id"] for r in await keys.list_keys()}
        for key_hash in (HASH_A, HASH_B, "c" * 64):
            still_valid = await keys.validate_key_by_hash(key_hash) is not None
            if still_valid:
                stored_id = (await redis.hgetall(f"auth:key:{key_hash}"))["key_id"]
                assert stored_id in listed, (
                    f"{key_hash[:16]} authenticates but is not listed — "
                    "it can never be revoked by key_id"
                )
```

- [ ] **Step 2: Run the invariant test**

Run: `cd auth && python -m pytest tests/test_key_id_resolution.py::TestSubsystemInvariant -v`
Expected: PASS.

- [ ] **Step 3: Prove the test is load-bearing**

Temporarily restore the old first-match behaviour in `revoke_key` — replace its
body with the original:

```python
    async for full_key in _redis.scan_iter(f"{_KEY_PREFIX}{key_id}*", count=10):
        await _redis.delete(full_key)
        await _redis.zrem(_KEY_INDEX, key_id)
        return True
    return False
```

Run: `cd auth && python -m pytest tests/test_key_id_resolution.py::TestSubsystemInvariant -v`
Expected: **FAIL** with "authenticates but is not listed". A guard test that
passes against the bug it describes is worthless — this step is how you know it
isn't. Then revert the temporary change (`git checkout auth/keys.py`) and re-run
to confirm PASS.

- [ ] **Step 4: Run the full auth suite one last time**

Run: `cd auth && python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add auth/tests/test_key_id_resolution.py
git commit -m "test(auth): pin the invariant a prefix collision used to break"
```

---

## Self-review

**Spec coverage.** §2's `revoke_key` rules → Task 1 (exactly-one deletes; zero returns False; differing stored id ignored; ≥2 refuses). §2's `list_keys` rules → Task 2 (all matches emitted, ambiguity marked). §2's `CRITICAL` logging → Tasks 1 and 2. §2.1's "no representation change" → held: no task touches `key_id` derivation, the index member, or the record layout. §3's five test bullets → Tasks 1, 2 and 4. §4's 409 on the REST route → Task 3. §4's `deploy/firekeep-admin keys revoke` is **not** in scope here — that subcommand is created by the enrollment spec and inherits this behaviour when it lands.

**Beyond the spec, deliberately.** `_KEY_ID_RE` is not in the spec. `key_id` arrives from a URL path parameter and is interpolated into a Redis glob, so `DELETE /auth/keys/*` currently scans the whole keyspace, deletes the first record found, and returns `True`. It is the same defect class the spec is about — resolution acting on an unverified match — and fixing it separately would mean touching these two functions twice.

**One addition to the public surface:** `validate_key_by_hash`. Records written directly (which is the only way to construct a collision) have no plaintext, so no test could otherwise ask "does this still authenticate?" — and that question is the invariant in Task 4. It is `validate_key`'s existing lookup half, factored out; `validate_key` is unchanged.

**A defect found in this plan during its own self-review, kept as a note because the
next reader will hit the same wall.** Task 3's test was first written against a bare
`FastAPI()` app with the auth router mounted and no key. That can never reach the
ambiguity branch: `require_scope("admin")` **403s** under `AUTH_ENABLED=false`, because
the anonymous scope set is `SCOPES - {"admin", "*"}` and the disabled path performs a
real membership check rather than passing through. The test needs a minted admin key and
the auth middleware, which is why it lives in `cortex/tests/`.

**Type consistency.** `_resolve_key_id` returns `list[tuple[str, dict[str, Any]]]` in Task 1 and is unpacked as `(redis_key, data)` in Tasks 1 and 2. `AmbiguousKeyIdError.matches` is `list[str]` (Redis keys) in Task 1 and read as `len(exc.matches)` in Task 3.
