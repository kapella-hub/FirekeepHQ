# CortexBridge Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build CortexBridge — a shadow context MCP server that solves LLM mid-task amnesia by maintaining a live working state that agents can restore after context compression or session restarts.

**Architecture:** A standalone Python project at `/opt/CortexBridge` with a FastMCP server exposing 7 tools. Redis DB 3 stores all session state. On session completion, distills to FirekeepCortex via HTTP. No LLM dependency.

**Tech Stack:** Python 3.11, FastAPI, FastMCP, Redis (async), httpx, pytest, Docker

**Spec:** `/opt/FirekeepCortex/docs/superpowers/specs/2026-03-14-cortexbridge-design.md`

---

## File Structure

| File | Responsibility |
|------|----------------|
| `/opt/CortexBridge/app/__init__.py` | Package marker |
| `/opt/CortexBridge/app/config.py` | Pydantic BaseSettings, env var loading |
| `/opt/CortexBridge/app/redis_client.py` | Async Redis connection management |
| `/opt/CortexBridge/app/session.py` | Session CRUD, state transitions, Redis transactions |
| `/opt/CortexBridge/app/shadow.py` | Shadow context assembly (Markdown generation) |
| `/opt/CortexBridge/app/distiller.py` | FirekeepCortex distillation (httpx calls) |
| `/opt/CortexBridge/app/mcp_server.py` | MCP tool definitions, entry point |
| `/opt/CortexBridge/tests/__init__.py` | Test package marker |
| `/opt/CortexBridge/tests/conftest.py` | Shared fixtures (mock Redis, etc.) |
| `/opt/CortexBridge/tests/test_config.py` | Config tests |
| `/opt/CortexBridge/tests/test_session.py` | Session CRUD + state transition tests |
| `/opt/CortexBridge/tests/test_shadow.py` | Shadow assembly tests |
| `/opt/CortexBridge/tests/test_distiller.py` | Distillation tests |
| `/opt/CortexBridge/tests/test_mcp_tools.py` | MCP tool integration tests |
| `/opt/CortexBridge/requirements.txt` | Dependencies |
| `/opt/CortexBridge/Dockerfile` | Container image |
| `/opt/CortexBridge/docker-compose.yml` | Standalone compose with Redis network |
| `/opt/CortexBridge/.env.example` | Example environment variables |

---

## Chunk 1: Project Scaffold + Config + Redis Client

### Task 1: Project Scaffold

**Files:**
- Create: `/opt/CortexBridge/requirements.txt`
- Create: `/opt/CortexBridge/app/__init__.py`
- Create: `/opt/CortexBridge/app/config.py`
- Create: `/opt/CortexBridge/tests/__init__.py`
- Create: `/opt/CortexBridge/tests/test_config.py`

- [ ] **Step 1: Create project directory and requirements.txt**

```bash
mkdir -p /opt/CortexBridge/app /opt/CortexBridge/tests
```

`/opt/CortexBridge/requirements.txt`:
```
fastapi>=0.115,<1
uvicorn[standard]>=0.34,<1
pydantic-settings>=2.7,<3
redis[hiredis]>=5.2,<6
httpx>=0.28,<1
fastmcp>=3.1,<4

# Dev / Test
pytest>=8.0,<9
pytest-asyncio>=0.24,<1
```

- [ ] **Step 2: Create config.py**

`/opt/CortexBridge/app/__init__.py`:
```python
"""CortexBridge — Shadow Context Manager for AI agents."""
```

`/opt/CortexBridge/app/config.py`:
```python
"""Configuration for CortexBridge — loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    REDIS_URL: str = "redis://localhost:6379/3"
    MCP_HOST: str = "0.0.0.0"
    MCP_PORT: int = 8070
    FIREKEEP_API_URL: str = "http://localhost:8100"
    FIREKEEP_API_KEY: str = ""
    FIREKEEP_NAMESPACE: str = "cortexbridge"
    SESSION_TTL_DAYS: int = 7
    MAX_SESSIONS: int = 100
    DEFAULT_AGENT_ID: str = "default"

    # Component size limits
    PLAN_MAX_BYTES: int = 10240  # 10 KB
    DECISIONS_MAX: int = 50
    FILES_MAX: int = 100
    PROGRESS_MAX: int = 50
    SCRATCH_MAX: int = 50

    model_config = {"env_prefix": "CB_", "env_file": ".env", "extra": "ignore"}


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
```

- [ ] **Step 3: Write config tests**

`/opt/CortexBridge/tests/__init__.py`:
```python
```

`/opt/CortexBridge/tests/test_config.py`:
```python
"""Tests for CortexBridge configuration."""

from app.config import Settings


class TestSettings:
    def test_defaults(self):
        s = Settings()
        assert s.REDIS_URL == "redis://localhost:6379/3"
        assert s.MCP_PORT == 8070
        assert s.SESSION_TTL_DAYS == 7
        assert s.MAX_SESSIONS == 100
        assert s.DEFAULT_AGENT_ID == "default"
        assert s.PLAN_MAX_BYTES == 10240
        assert s.DECISIONS_MAX == 50
        assert s.FILES_MAX == 100

    def test_env_prefix(self):
        import os
        os.environ["CB_MCP_PORT"] = "9999"
        try:
            s = Settings()
            assert s.MCP_PORT == 9999
        finally:
            del os.environ["CB_MCP_PORT"]
```

- [ ] **Step 4: Install dependencies and run tests**

```bash
cd /opt/CortexBridge
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest tests/test_config.py -v
```

Expected: ALL PASS

- [ ] **Step 5: Initialize git and commit**

```bash
cd /opt/CortexBridge
git init
git add -A
git commit -m "init: project scaffold with config and tests"
```

### Task 2: Redis Client

**Files:**
- Create: `/opt/CortexBridge/app/redis_client.py`

- [ ] **Step 1: Create Redis client module**

`/opt/CortexBridge/app/redis_client.py`:
```python
"""Async Redis connection for CortexBridge."""

from __future__ import annotations

import redis.asyncio as aioredis

from app.config import get_settings

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Return a shared async Redis client, creating on first call."""
    global _redis
    if _redis is None:
        settings = get_settings()
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


async def close_redis() -> None:
    """Close the Redis client."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
```

- [ ] **Step 2: Commit**

```bash
git add app/redis_client.py
git commit -m "feat: add async Redis client module"
```

---

## Chunk 2: Session Manager

### Task 3: Session CRUD and State Transitions

**Files:**
- Create: `/opt/CortexBridge/app/session.py`
- Create: `/opt/CortexBridge/tests/conftest.py`
- Create: `/opt/CortexBridge/tests/test_session.py`

- [ ] **Step 1: Create test fixtures**

`/opt/CortexBridge/tests/conftest.py`:
```python
"""Shared test fixtures for CortexBridge."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_redis():
    """Mock async Redis client."""
    r = AsyncMock()
    r.hset = AsyncMock()
    r.hget = AsyncMock(return_value=None)
    r.hgetall = AsyncMock(return_value={})
    r.set = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.delete = AsyncMock()
    r.exists = AsyncMock(return_value=0)
    r.expire = AsyncMock()
    r.lpush = AsyncMock()
    r.lrange = AsyncMock(return_value=[])
    r.llen = AsyncMock(return_value=0)
    r.ltrim = AsyncMock()
    r.rpop = AsyncMock()
    r.zadd = AsyncMock()
    r.zrangebyscore = AsyncMock(return_value=[])
    r.zrevrangebyscore = AsyncMock(return_value=[])
    r.zcard = AsyncMock(return_value=0)
    r.zrem = AsyncMock()
    r.zrange = AsyncMock(return_value=[])

    # Pipeline support
    mock_pipe = AsyncMock()
    mock_pipe.__aenter__ = AsyncMock(return_value=mock_pipe)
    mock_pipe.__aexit__ = AsyncMock(return_value=False)
    mock_pipe.execute = AsyncMock(return_value=[])
    r.pipeline = MagicMock(return_value=mock_pipe)
    r._pipeline = mock_pipe

    return r
```

- [ ] **Step 2: Write session tests**

`/opt/CortexBridge/tests/test_session.py`:
```python
"""Tests for session management."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.session import SessionManager


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def manager(mock_redis, settings):
    return SessionManager(mock_redis, settings)


class TestStartSession:
    @pytest.mark.asyncio
    async def test_creates_session(self, manager, mock_redis):
        result = await manager.start_session("implement feature X")
        assert "session_id" in result
        assert "created_at" in result
        # Pipeline was used for atomic operation
        mock_redis.pipeline.assert_called()

    @pytest.mark.asyncio
    async def test_pauses_existing_active_session(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="old-session-id")
        result = await manager.start_session("new task")
        assert "session_id" in result


class TestGetActiveSession:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_active(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value=None)
        result = await manager.get_active_session_id()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_session_id(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        result = await manager.get_active_session_id()
        assert result == "sess-123"


class TestUpdateComponent:
    @pytest.mark.asyncio
    async def test_update_plan(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        result = await manager.update("plan", "- [ ] Step 1\n- [ ] Step 2")
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_update_plan_rejects_oversized(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        huge_plan = "x" * 20000
        with pytest.raises(ValueError, match="exceeds"):
            await manager.update("plan", huge_plan)

    @pytest.mark.asyncio
    async def test_update_decision_appends(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        mock_redis.llen = AsyncMock(return_value=5)
        result = await manager.update("decision", "chose approach A")
        assert result["status"] == "ok"
        assert result["component_count"] == 6

    @pytest.mark.asyncio
    async def test_update_file_upserts(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        mock_redis.hlen = AsyncMock(return_value=3)
        result = await manager.update("file", "added function X", key="app/main.py")
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_update_requires_active_session(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="No active session"):
            await manager.update("plan", "test")

    @pytest.mark.asyncio
    async def test_update_file_requires_key(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        with pytest.raises(ValueError, match="key"):
            await manager.update("file", "summary")

    @pytest.mark.asyncio
    async def test_update_scratch_requires_key(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        with pytest.raises(ValueError, match="key"):
            await manager.update("scratch", "value")


class TestCompleteSession:
    @pytest.mark.asyncio
    async def test_marks_completed(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        mock_redis.hgetall = AsyncMock(return_value={"goal": "test", "status": "active", "agent_id": "default"})
        result = await manager.complete_session()
        assert result["status"] == "completed"
        mock_redis.pipeline.assert_called()

    @pytest.mark.asyncio
    async def test_requires_active_session(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="No active session"):
            await manager.complete_session()


class TestAbandonSession:
    @pytest.mark.asyncio
    async def test_marks_abandoned(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        mock_redis.hgetall = AsyncMock(return_value={"goal": "test", "status": "active", "agent_id": "default"})
        result = await manager.abandon_session()
        assert result["status"] == "abandoned"


class TestResumeSession:
    @pytest.mark.asyncio
    async def test_resumes_paused_session(self, manager, mock_redis):
        mock_redis.hgetall = AsyncMock(return_value={
            "goal": "old task", "status": "paused", "agent_id": "default",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "tags": "[]",
        })
        result = await manager.resume_session("sess-old")
        assert result["status"] == "active"

    @pytest.mark.asyncio
    async def test_resume_rejects_completed(self, manager, mock_redis):
        mock_redis.hgetall = AsyncMock(return_value={"status": "completed"})
        with pytest.raises(ValueError, match="Cannot resume"):
            await manager.resume_session("sess-done")


class TestListSessions:
    @pytest.mark.asyncio
    async def test_returns_sessions(self, manager, mock_redis):
        mock_redis.zrevrangebyscore = AsyncMock(return_value=["sess-1", "sess-2"])
        mock_redis.hgetall = AsyncMock(return_value={
            "goal": "task", "status": "active", "agent_id": "default",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "tags": "[]",
        })
        result = await manager.list_sessions()
        assert len(result) == 2
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_session.py -v`
Expected: FAIL (module does not exist)

- [ ] **Step 4: Implement SessionManager**

`/opt/CortexBridge/app/session.py`:
```python
"""Session management — CRUD, state transitions, Redis transactions."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from app.config import Settings


class SessionManager:
    """Manages session lifecycle in Redis with atomic state transitions."""

    def __init__(self, redis: aioredis.Redis, settings: Settings) -> None:
        self._r = redis
        self._s = settings

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _session_key(sid: str) -> str:
        return f"cb:session:{sid}"

    @staticmethod
    def _active_key(agent_id: str) -> str:
        return f"cb:active:{agent_id}"

    @staticmethod
    def _plan_key(sid: str) -> str:
        return f"cb:session:{sid}:plan"

    @staticmethod
    def _decisions_key(sid: str) -> str:
        return f"cb:session:{sid}:decisions"

    @staticmethod
    def _files_key(sid: str) -> str:
        return f"cb:session:{sid}:files"

    @staticmethod
    def _progress_key(sid: str) -> str:
        return f"cb:session:{sid}:progress"

    @staticmethod
    def _scratch_key(sid: str) -> str:
        return f"cb:session:{sid}:scratch"

    INDEX_KEY = "cb:sessions"

    def _all_session_keys(self, sid: str) -> list[str]:
        return [
            self._session_key(sid),
            self._plan_key(sid),
            self._decisions_key(sid),
            self._files_key(sid),
            self._progress_key(sid),
            self._scratch_key(sid),
        ]

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    async def start_session(
        self, goal: str, agent_id: str | None = None, tags: list[str] | None = None
    ) -> dict[str, str]:
        agent_id = agent_id or self._s.DEFAULT_AGENT_ID
        session_id = str(uuid.uuid4())[:12]
        now = datetime.now(timezone.utc).isoformat()

        async with self._r.pipeline(transaction=True) as pipe:
            # Pause current active session if any
            current = await self._r.get(self._active_key(agent_id))
            if current:
                pipe.hset(self._session_key(current), "status", "paused")
                pipe.hset(self._session_key(current), "updated_at", now)

            # Create new session
            pipe.hset(self._session_key(session_id), mapping={
                "goal": goal,
                "status": "active",
                "agent_id": agent_id,
                "created_at": now,
                "updated_at": now,
                "tags": json.dumps(tags or []),
                "outcome": "",
                "distillation": "",
            })
            pipe.set(self._active_key(agent_id), session_id)
            pipe.zadd(self.INDEX_KEY, {session_id: datetime.now(timezone.utc).timestamp()})
            await pipe.execute()

        # Enforce MAX_SESSIONS
        await self._enforce_max_sessions()

        return {"session_id": session_id, "created_at": now}

    # ------------------------------------------------------------------
    # Update components
    # ------------------------------------------------------------------

    async def update(
        self,
        category: str,
        content: str,
        key: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        agent_id = agent_id or self._s.DEFAULT_AGENT_ID
        sid = await self._r.get(self._active_key(agent_id))
        if not sid:
            raise ValueError("No active session")

        now = datetime.now(timezone.utc).isoformat()
        count = 0

        if category == "plan":
            if len(content.encode("utf-8")) > self._s.PLAN_MAX_BYTES:
                raise ValueError(f"Plan exceeds {self._s.PLAN_MAX_BYTES} bytes")
            await self._r.set(self._plan_key(sid), content)
            count = 1

        elif category == "decision":
            entry = json.dumps({"timestamp": now, "content": content})
            await self._r.lpush(self._decisions_key(sid), entry)
            await self._r.ltrim(self._decisions_key(sid), 0, self._s.DECISIONS_MAX - 1)
            count = await self._r.llen(self._decisions_key(sid))

        elif category == "file":
            if not key:
                raise ValueError("'key' (file path) is required for file updates")
            entry = json.dumps({"summary": content, "last_action": now})
            await self._r.hset(self._files_key(sid), key, entry)
            count = await self._r.hlen(self._files_key(sid))
            # Evict oldest if over limit (hash doesn't have ordering, so this is best-effort)
            if count > self._s.FILES_MAX:
                all_fields = await self._r.hkeys(self._files_key(sid))
                to_remove = all_fields[: count - self._s.FILES_MAX]
                if to_remove:
                    await self._r.hdel(self._files_key(sid), *to_remove)
                    count = await self._r.hlen(self._files_key(sid))

        elif category == "progress":
            entry = json.dumps({"timestamp": now, "content": content})
            await self._r.lpush(self._progress_key(sid), entry)
            await self._r.ltrim(self._progress_key(sid), 0, self._s.PROGRESS_MAX - 1)
            count = await self._r.llen(self._progress_key(sid))

        elif category == "scratch":
            if not key:
                raise ValueError("'key' is required for scratch updates")
            await self._r.hset(self._scratch_key(sid), key, content)
            count = await self._r.hlen(self._scratch_key(sid))
            if count > self._s.SCRATCH_MAX:
                all_fields = await self._r.hkeys(self._scratch_key(sid))
                to_remove = all_fields[: count - self._s.SCRATCH_MAX]
                if to_remove:
                    await self._r.hdel(self._scratch_key(sid), *to_remove)
                    count = await self._r.hlen(self._scratch_key(sid))

        else:
            raise ValueError(f"Unknown category: {category}")

        # Update timestamp
        await self._r.hset(self._session_key(sid), "updated_at", now)
        await self._r.zadd(self.INDEX_KEY, {sid: datetime.now(timezone.utc).timestamp()})

        return {"status": "ok", "component_count": count}

    # ------------------------------------------------------------------
    # Get session data (for shadow assembly)
    # ------------------------------------------------------------------

    async def get_session_data(self, session_id: str) -> dict[str, Any] | None:
        meta = await self._r.hgetall(self._session_key(session_id))
        if not meta:
            return None
        plan = await self._r.get(self._plan_key(session_id)) or ""
        decisions_raw = await self._r.lrange(self._decisions_key(session_id), 0, -1)
        files_raw = await self._r.hgetall(self._files_key(session_id))
        progress_raw = await self._r.lrange(self._progress_key(session_id), 0, -1)
        scratch = await self._r.hgetall(self._scratch_key(session_id))

        decisions = [json.loads(d) for d in reversed(decisions_raw)]
        progress = [json.loads(p) for p in reversed(progress_raw)]
        files = {k: json.loads(v) for k, v in files_raw.items()}

        return {
            **meta,
            "tags": json.loads(meta.get("tags", "[]")),
            "plan": plan,
            "decisions": decisions,
            "files": files,
            "progress": progress,
            "scratch": scratch,
        }

    async def get_active_session_id(self, agent_id: str | None = None) -> str | None:
        agent_id = agent_id or self._s.DEFAULT_AGENT_ID
        return await self._r.get(self._active_key(agent_id))

    # ------------------------------------------------------------------
    # Complete
    # ------------------------------------------------------------------

    async def complete_session(
        self, session_id: str | None = None, outcome: str | None = None, agent_id: str | None = None
    ) -> dict[str, str]:
        agent_id = agent_id or self._s.DEFAULT_AGENT_ID
        if session_id is None:
            session_id = await self._r.get(self._active_key(agent_id))
        if not session_id:
            raise ValueError("No active session")

        meta = await self._r.hgetall(self._session_key(session_id))
        if not meta:
            raise ValueError(f"Session {session_id} not found")

        now = datetime.now(timezone.utc).isoformat()
        ttl_seconds = self._s.SESSION_TTL_DAYS * 86400

        async with self._r.pipeline(transaction=True) as pipe:
            pipe.hset(self._session_key(session_id), mapping={
                "status": "completed",
                "updated_at": now,
                "outcome": outcome or "",
                "distillation": "pending",
            })
            # Clear active pointer if this is the active session
            if meta.get("agent_id"):
                current_active = await self._r.get(self._active_key(meta["agent_id"]))
                if current_active == session_id:
                    pipe.delete(self._active_key(meta["agent_id"]))
            # Set TTL on all session keys
            for key in self._all_session_keys(session_id):
                pipe.expire(key, ttl_seconds)
            await pipe.execute()

        return {"status": "completed", "session_id": session_id}

    # ------------------------------------------------------------------
    # Abandon
    # ------------------------------------------------------------------

    async def abandon_session(
        self, session_id: str | None = None, agent_id: str | None = None
    ) -> dict[str, str]:
        agent_id = agent_id or self._s.DEFAULT_AGENT_ID
        if session_id is None:
            session_id = await self._r.get(self._active_key(agent_id))
        if not session_id:
            raise ValueError("No active session")

        meta = await self._r.hgetall(self._session_key(session_id))
        if not meta:
            raise ValueError(f"Session {session_id} not found")

        now = datetime.now(timezone.utc).isoformat()
        ttl_seconds = self._s.SESSION_TTL_DAYS * 86400

        async with self._r.pipeline(transaction=True) as pipe:
            pipe.hset(self._session_key(session_id), mapping={
                "status": "abandoned",
                "updated_at": now,
            })
            if meta.get("agent_id"):
                current_active = await self._r.get(self._active_key(meta["agent_id"]))
                if current_active == session_id:
                    pipe.delete(self._active_key(meta["agent_id"]))
            for key in self._all_session_keys(session_id):
                pipe.expire(key, ttl_seconds)
            await pipe.execute()

        return {"status": "abandoned", "session_id": session_id}

    # ------------------------------------------------------------------
    # Resume
    # ------------------------------------------------------------------

    async def resume_session(self, session_id: str, agent_id: str | None = None) -> dict[str, Any]:
        agent_id = agent_id or self._s.DEFAULT_AGENT_ID
        meta = await self._r.hgetall(self._session_key(session_id))
        if not meta:
            raise ValueError(f"Session {session_id} not found")
        if meta.get("status") in ("completed", "abandoned"):
            raise ValueError(f"Cannot resume {meta['status']} session")

        now = datetime.now(timezone.utc).isoformat()

        async with self._r.pipeline(transaction=True) as pipe:
            # Pause current active
            current = await self._r.get(self._active_key(agent_id))
            if current and current != session_id:
                pipe.hset(self._session_key(current), "status", "paused")
                pipe.hset(self._session_key(current), "updated_at", now)
            # Activate target
            pipe.hset(self._session_key(session_id), mapping={
                "status": "active",
                "agent_id": agent_id,
                "updated_at": now,
            })
            pipe.set(self._active_key(agent_id), session_id)
            # Remove TTL (in case it was set during a previous pause/abandon)
            for key in self._all_session_keys(session_id):
                pipe.persist(key)
            await pipe.execute()

        return {"status": "active", "session_id": session_id}

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    async def list_sessions(
        self, status: str | None = None, agent_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        # Get session IDs from index (newest first)
        all_ids = await self._r.zrevrangebyscore(self.INDEX_KEY, "+inf", "-inf", start=0, num=limit * 2)
        results = []
        for sid in all_ids:
            if len(results) >= limit:
                break
            meta = await self._r.hgetall(self._session_key(sid))
            if not meta:
                # Expired — clean up index entry
                await self._r.zrem(self.INDEX_KEY, sid)
                continue
            if status and meta.get("status") != status:
                continue
            if agent_id and meta.get("agent_id") != agent_id:
                continue
            results.append({
                "session_id": sid,
                "goal": meta.get("goal", ""),
                "status": meta.get("status", ""),
                "created_at": meta.get("created_at", ""),
                "updated_at": meta.get("updated_at", ""),
                "agent_id": meta.get("agent_id", ""),
            })
        return results

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def _enforce_max_sessions(self) -> None:
        count = await self._r.zcard(self.INDEX_KEY)
        if count <= self._s.MAX_SESSIONS:
            return
        # Get oldest sessions
        oldest = await self._r.zrange(self.INDEX_KEY, 0, count - self._s.MAX_SESSIONS - 1)
        for sid in oldest:
            meta = await self._r.hgetall(self._session_key(sid))
            if not meta or meta.get("status") in ("completed", "abandoned"):
                for key in self._all_session_keys(sid):
                    await self._r.delete(key)
                await self._r.zrem(self.INDEX_KEY, sid)

    async def set_distillation_status(self, session_id: str, status: str) -> None:
        await self._r.hset(self._session_key(session_id), "distillation", status)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_session.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add app/session.py tests/conftest.py tests/test_session.py
git commit -m "feat: session manager with CRUD, state transitions, Redis transactions"
```

---

## Chunk 3: Shadow Assembler + Distiller

### Task 4: Shadow Context Assembly

**Files:**
- Create: `/opt/CortexBridge/app/shadow.py`
- Create: `/opt/CortexBridge/tests/test_shadow.py`

- [ ] **Step 1: Write shadow assembly tests**

`/opt/CortexBridge/tests/test_shadow.py`:
```python
"""Tests for shadow context assembly."""

from app.shadow import assemble_shadow


class TestAssembleShadow:
    def test_full_shadow(self):
        data = {
            "goal": "implement feature X",
            "status": "active",
            "created_at": "2026-03-14T02:00:00Z",
            "updated_at": "2026-03-14T02:15:00Z",
            "plan": "- [x] Step 1\n- [ ] Step 2",
            "decisions": [
                {"timestamp": "2026-03-14T02:05:00Z", "content": "chose approach A"},
            ],
            "files": {
                "app/main.py": {"summary": "added endpoint", "last_action": "2026-03-14T02:10:00Z"},
            },
            "progress": [
                {"timestamp": "2026-03-14T02:06:00Z", "content": "step 1 done"},
            ],
            "scratch": {"key1": "value1"},
        }
        result = assemble_shadow(data)
        assert "## Session: implement feature X" in result
        assert "### Plan" in result
        assert "- [x] Step 1" in result
        assert "### Decisions" in result
        assert "chose approach A" in result
        assert "### Files Known" in result
        assert "**app/main.py**" in result
        assert "### Progress" in result
        assert "step 1 done" in result
        assert "### Scratchpad" in result
        assert "key1: value1" in result

    def test_empty_components(self):
        data = {
            "goal": "test",
            "status": "active",
            "created_at": "2026-03-14T00:00:00Z",
            "updated_at": "2026-03-14T00:00:00Z",
            "plan": "",
            "decisions": [],
            "files": {},
            "progress": [],
            "scratch": {},
        }
        result = assemble_shadow(data)
        assert "## Session: test" in result
        assert "No plan set" in result

    def test_section_order(self):
        data = {
            "goal": "test", "status": "active",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "plan": "step", "decisions": [{"timestamp": "t", "content": "d"}],
            "files": {"f": {"summary": "s", "last_action": "t"}},
            "progress": [{"timestamp": "t", "content": "p"}],
            "scratch": {"k": "v"},
        }
        result = assemble_shadow(data)
        plan_pos = result.index("### Plan")
        dec_pos = result.index("### Decisions")
        files_pos = result.index("### Files Known")
        prog_pos = result.index("### Progress")
        scratch_pos = result.index("### Scratchpad")
        assert plan_pos < dec_pos < files_pos < prog_pos < scratch_pos
```

- [ ] **Step 2: Implement shadow assembly**

`/opt/CortexBridge/app/shadow.py`:
```python
"""Shadow context assembly — generates the Markdown document."""

from __future__ import annotations

from typing import Any


def assemble_shadow(data: dict[str, Any]) -> str:
    """Assemble a shadow context Markdown document from session data.

    Section order is fixed: Plan, Decisions, Files Known, Progress, Scratchpad.
    """
    lines: list[str] = []

    # Header
    lines.append(f"## Session: {data.get('goal', 'unknown')}")
    lines.append(
        f"**Status**: {data.get('status', 'unknown')} | "
        f"**Started**: {data.get('created_at', '')} | "
        f"**Updated**: {data.get('updated_at', '')}"
    )
    lines.append("")

    # Plan
    lines.append("### Plan")
    plan = data.get("plan", "")
    lines.append(plan if plan else "*No plan set*")
    lines.append("")

    # Decisions
    lines.append("### Decisions")
    decisions = data.get("decisions", [])
    if decisions:
        for d in decisions:
            ts = d.get("timestamp", "")
            # Extract time portion for compact display
            time_part = ts[11:16] if len(ts) > 16 else ts
            lines.append(f"- [{time_part}] {d.get('content', '')}")
    else:
        lines.append("*No decisions recorded*")
    lines.append("")

    # Files Known
    lines.append("### Files Known")
    files = data.get("files", {})
    if files:
        for path, info in sorted(files.items()):
            summary = info.get("summary", "") if isinstance(info, dict) else str(info)
            lines.append(f"- **{path}** — {summary}")
    else:
        lines.append("*No files tracked*")
    lines.append("")

    # Progress
    lines.append("### Progress")
    progress = data.get("progress", [])
    if progress:
        for p in progress:
            ts = p.get("timestamp", "")
            time_part = ts[11:16] if len(ts) > 16 else ts
            lines.append(f"- [{time_part}] {p.get('content', '')}")
    else:
        lines.append("*No progress logged*")
    lines.append("")

    # Scratchpad
    lines.append("### Scratchpad")
    scratch = data.get("scratch", {})
    if scratch:
        for k, v in sorted(scratch.items()):
            lines.append(f"- {k}: {v}")
    else:
        lines.append("*Empty*")

    return "\n".join(lines)
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_shadow.py -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add app/shadow.py tests/test_shadow.py
git commit -m "feat: shadow context Markdown assembly"
```

### Task 5: Distiller

**Files:**
- Create: `/opt/CortexBridge/app/distiller.py`
- Create: `/opt/CortexBridge/tests/test_distiller.py`

- [ ] **Step 1: Write distiller tests**

`/opt/CortexBridge/tests/test_distiller.py`:
```python
"""Tests for FirekeepCortex distillation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest

from app.config import Settings
from app.distiller import Distiller


@pytest.fixture
def distiller():
    return Distiller(Settings())


class TestBuildPayload:
    def test_builds_action_from_goal_and_plan(self, distiller):
        data = {
            "goal": "fix auth bug",
            "plan": "- [x] Step 1: find bug\n- [x] Step 2: fix it\n- [ ] Step 3: test",
            "decisions": [{"timestamp": "t", "content": "used approach A"}],
            "progress": [{"timestamp": "t", "content": "done"}],
            "tags": ["auth", "bugfix"],
        }
        payload = distiller.build_payload(data, outcome="bug fixed")
        assert "fix auth bug" in payload["action"]
        assert payload["outcome"] == "bug fixed"
        assert "approach A" in payload["resolution"]
        assert "cortexbridge" in payload["tags"]

    def test_uses_last_progress_when_no_outcome(self, distiller):
        data = {
            "goal": "task",
            "plan": "",
            "decisions": [],
            "progress": [{"timestamp": "t", "content": "last thing done"}],
            "tags": [],
        }
        payload = distiller.build_payload(data)
        assert payload["outcome"] == "last thing done"

    def test_fallback_outcome(self, distiller):
        data = {"goal": "task", "plan": "", "decisions": [], "progress": [], "tags": []}
        payload = distiller.build_payload(data)
        assert payload["outcome"] == "Session completed"


class TestDistill:
    @pytest.mark.asyncio
    async def test_successful_distillation(self, distiller):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "stored", "vector_id": "v-123"}
        mock_response.raise_for_status = MagicMock()

        with patch("app.distiller.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_client

            result = await distiller.distill(
                {"goal": "t", "plan": "", "decisions": [], "progress": [], "tags": []},
            )
            assert result["status"] == "success"
            assert result["firekeep_memory_id"] == "v-123"

    @pytest.mark.asyncio
    async def test_failed_distillation(self, distiller):
        with patch("app.distiller.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=httpx.RequestError("down"))
            MockClient.return_value = mock_client

            result = await distiller.distill(
                {"goal": "t", "plan": "", "decisions": [], "progress": [], "tags": []},
            )
            assert result["status"] == "failed"
```

- [ ] **Step 2: Implement distiller**

`/opt/CortexBridge/app/distiller.py`:
```python
"""Distillation — converts session data to FirekeepCortex memories."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class Distiller:
    """Distills completed session data into FirekeepCortex long-term memory."""

    def __init__(self, settings: Settings) -> None:
        self._api_url = settings.FIREKEEP_API_URL
        self._api_key = settings.FIREKEEP_API_KEY
        self._namespace = settings.FIREKEEP_NAMESPACE

    def build_payload(self, data: dict[str, Any], outcome: str | None = None) -> dict[str, Any]:
        """Build the /memory/learn payload from session data."""
        goal = data.get("goal", "Unknown task")
        plan = data.get("plan", "")

        # Extract completed steps from plan
        completed = re.findall(r"- \[x\] (.+)", plan)
        plan_summary = "; ".join(completed[:5]) if completed else "No plan steps recorded"

        action = f"{goal} — {plan_summary}"

        # Outcome: explicit > last progress > fallback
        if not outcome:
            progress = data.get("progress", [])
            if progress:
                outcome = progress[-1].get("content", "Session completed")
            else:
                outcome = "Session completed"

        # Resolution: join top 3 decisions
        decisions = data.get("decisions", [])
        resolution = "; ".join(d.get("content", "") for d in decisions[:3]) or None

        tags = list(data.get("tags", [])) + ["cortexbridge"]

        return {
            "action": action,
            "outcome": outcome,
            "resolution": resolution,
            "tags": tags,
            "domain": "development",
            "namespace": self._namespace,
        }

    async def distill(self, data: dict[str, Any], outcome: str | None = None) -> dict[str, Any]:
        """Send distilled session to FirekeepCortex. Returns status dict."""
        payload = self.build_payload(data, outcome)

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self._api_url}/memory/learn",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                resp_data = resp.json()
                memory_id = resp_data.get("vector_id") or resp_data.get("graph_id")
                logger.info("Distilled session to FirekeepCortex: %s", memory_id)
                return {"status": "success", "firekeep_memory_id": memory_id}
        except Exception as exc:
            logger.error("Distillation failed: %s", exc)
            return {"status": "failed", "error": str(exc)}
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_distiller.py -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add app/distiller.py tests/test_distiller.py
git commit -m "feat: FirekeepCortex distiller for session completion"
```

---

## Chunk 4: MCP Server + Docker

### Task 6: MCP Server

**Files:**
- Create: `/opt/CortexBridge/app/mcp_server.py`
- Create: `/opt/CortexBridge/tests/test_mcp_tools.py`

- [ ] **Step 1: Implement MCP server with all 7 tools**

`/opt/CortexBridge/app/mcp_server.py`:
```python
"""CortexBridge MCP Server — shadow context tools for AI agents."""

from __future__ import annotations

import os

from fastmcp import FastMCP

from app.config import get_settings
from app.distiller import Distiller
from app.redis_client import get_redis, close_redis
from app.session import SessionManager
from app.shadow import assemble_shadow

MCP_HOST = os.getenv("CB_MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("CB_MCP_PORT", "8070"))

mcp = FastMCP("CortexBridge")


async def _get_manager() -> SessionManager:
    r = await get_redis()
    return SessionManager(r, get_settings())


@mcp.tool()
async def ctx_start_session(
    goal: str, agent_id: str = "default", tags: list[str] | None = None
) -> dict:
    """Start a new working session with a goal description.

    Call this when beginning a new task. If you already have an active session,
    it will be automatically paused.

    Args:
        goal: What you are trying to accomplish (e.g., "implement namespace normalization").
        agent_id: Your agent identifier (use different IDs for parallel terminals).
        tags: Optional categorization tags.
    """
    mgr = await _get_manager()
    return await mgr.start_session(goal, agent_id=agent_id, tags=tags)


@mcp.tool()
async def ctx_update(
    category: str, content: str, key: str | None = None, agent_id: str = "default"
) -> dict:
    """Update your working context with new information.

    Call this as you work to record your plan, decisions, file knowledge, and progress.
    This data persists across context compressions and session restarts.

    Args:
        category: What to update — "plan", "decision", "file", "progress", or "scratch".
        content: The content to store. For "plan", send the full current plan (Markdown checklist).
        key: Required for "file" (file path) and "scratch" (entry name). Ignored for others.
        agent_id: Your agent identifier.
    """
    mgr = await _get_manager()
    return await mgr.update(category, content, key=key, agent_id=agent_id)


@mcp.tool()
async def ctx_get_shadow(session_id: str | None = None, agent_id: str = "default") -> dict:
    """Retrieve your full working context as a Markdown document.

    Call this after context compression or when starting a new conversation to restore
    your working state. Returns everything: your plan, decisions, file knowledge,
    progress, and scratchpad.

    Args:
        session_id: Specific session to retrieve (defaults to your active session).
        agent_id: Your agent identifier.
    """
    mgr = await _get_manager()
    if session_id is None:
        session_id = await mgr.get_active_session_id(agent_id)
    if not session_id:
        return {"error": "No active session. Start one with ctx_start_session."}

    data = await mgr.get_session_data(session_id)
    if not data:
        return {"error": f"Session {session_id} not found."}

    shadow = assemble_shadow(data)
    return {
        "session_id": session_id,
        "goal": data.get("goal", ""),
        "status": data.get("status", ""),
        "shadow": shadow,
    }


@mcp.tool()
async def ctx_complete_session(
    session_id: str | None = None, outcome: str | None = None, agent_id: str = "default"
) -> dict:
    """Mark the current session as completed and save learnings to long-term memory.

    Call this when your task is done. The session is distilled into a FirekeepCortex memory
    so future sessions can benefit from what you learned.

    Args:
        session_id: Session to complete (defaults to active session).
        outcome: Summary of what was accomplished.
        agent_id: Your agent identifier.
    """
    mgr = await _get_manager()
    result = await mgr.complete_session(session_id=session_id, outcome=outcome, agent_id=agent_id)
    sid = result["session_id"]

    # Distill to FirekeepCortex
    data = await mgr.get_session_data(sid)
    if data:
        distiller = Distiller(get_settings())
        dist_result = await distiller.distill(data, outcome=outcome)
        await mgr.set_distillation_status(sid, dist_result["status"])
        result["distillation"] = dist_result["status"]
        if dist_result.get("firekeep_memory_id"):
            result["firekeep_memory_id"] = dist_result["firekeep_memory_id"]
    else:
        result["distillation"] = "failed"

    return result


@mcp.tool()
async def ctx_abandon_session(session_id: str | None = None, agent_id: str = "default") -> dict:
    """Abandon a session without saving to long-term memory.

    Use this for sessions started by mistake or no longer relevant.

    Args:
        session_id: Session to abandon (defaults to active session).
        agent_id: Your agent identifier.
    """
    mgr = await _get_manager()
    return await mgr.abandon_session(session_id=session_id, agent_id=agent_id)


@mcp.tool()
async def ctx_list_sessions(
    status: str | None = None, agent_id: str | None = None, limit: int = 10
) -> dict:
    """List recent sessions.

    Args:
        status: Filter by status — "active", "paused", "completed", "abandoned".
        agent_id: Filter by agent ID.
        limit: Maximum number of sessions to return.
    """
    mgr = await _get_manager()
    sessions = await mgr.list_sessions(status=status, agent_id=agent_id, limit=limit)
    return {"sessions": sessions}


@mcp.tool()
async def ctx_resume_session(session_id: str, agent_id: str = "default") -> dict:
    """Resume a paused session and get its working context.

    Args:
        session_id: The session ID to resume.
        agent_id: Your agent identifier.
    """
    mgr = await _get_manager()
    await mgr.resume_session(session_id, agent_id=agent_id)

    data = await mgr.get_session_data(session_id)
    if not data:
        return {"error": f"Session {session_id} not found after resume."}

    shadow = assemble_shadow(data)
    return {
        "session_id": session_id,
        "goal": data.get("goal", ""),
        "status": "active",
        "shadow": shadow,
    }


if __name__ == "__main__":
    mcp.run(transport="http", host=MCP_HOST, port=MCP_PORT, stateless_http=True)
```

- [ ] **Step 2: Write MCP tool tests**

`/opt/CortexBridge/tests/test_mcp_tools.py`:
```python
"""Tests for MCP tool wiring — verify tools call through to session manager."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestMCPTools:
    @pytest.mark.asyncio
    async def test_ctx_start_session(self):
        from app.mcp_server import ctx_start_session
        with patch("app.mcp_server._get_manager") as mock_get:
            mgr = AsyncMock()
            mgr.start_session = AsyncMock(return_value={"session_id": "abc", "created_at": "now"})
            mock_get.return_value = mgr
            result = await ctx_start_session("test goal")
            assert result["session_id"] == "abc"
            mgr.start_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_ctx_update(self):
        from app.mcp_server import ctx_update
        with patch("app.mcp_server._get_manager") as mock_get:
            mgr = AsyncMock()
            mgr.update = AsyncMock(return_value={"status": "ok", "component_count": 1})
            mock_get.return_value = mgr
            result = await ctx_update("plan", "- [ ] Step 1")
            assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_ctx_get_shadow_no_session(self):
        from app.mcp_server import ctx_get_shadow
        with patch("app.mcp_server._get_manager") as mock_get:
            mgr = AsyncMock()
            mgr.get_active_session_id = AsyncMock(return_value=None)
            mock_get.return_value = mgr
            result = await ctx_get_shadow()
            assert "error" in result

    @pytest.mark.asyncio
    async def test_ctx_abandon(self):
        from app.mcp_server import ctx_abandon_session
        with patch("app.mcp_server._get_manager") as mock_get:
            mgr = AsyncMock()
            mgr.abandon_session = AsyncMock(return_value={"status": "abandoned"})
            mock_get.return_value = mgr
            result = await ctx_abandon_session()
            assert result["status"] == "abandoned"

    @pytest.mark.asyncio
    async def test_ctx_list_sessions(self):
        from app.mcp_server import ctx_list_sessions
        with patch("app.mcp_server._get_manager") as mock_get:
            mgr = AsyncMock()
            mgr.list_sessions = AsyncMock(return_value=[])
            mock_get.return_value = mgr
            result = await ctx_list_sessions()
            assert result["sessions"] == []
```

- [ ] **Step 3: Run all tests**

Run: `pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add app/mcp_server.py tests/test_mcp_tools.py
git commit -m "feat: MCP server with 7 context management tools"
```

### Task 7: Docker + Deployment

**Files:**
- Create: `/opt/CortexBridge/Dockerfile`
- Create: `/opt/CortexBridge/docker-compose.yml`
- Create: `/opt/CortexBridge/.env.example`

- [ ] **Step 1: Create Dockerfile**

`/opt/CortexBridge/Dockerfile`:
```dockerfile
FROM python:3.11-slim

RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --create-home appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8070

CMD ["python", "-m", "app.mcp_server"]
```

- [ ] **Step 2: Create docker-compose.yml**

`/opt/CortexBridge/docker-compose.yml`:
```yaml
services:
  cortexbridge:
    build: .
    container_name: cortexbridge
    ports:
      - "8070:8070"
    environment:
      - CB_REDIS_URL=redis://redis:6379/3
      - CB_MCP_HOST=0.0.0.0
      - CB_MCP_PORT=8070
      - CB_FIREKEEP_API_URL=http://firekeepcortex-api-1:8000
    networks:
      - firekeepcortex_default
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8070/mcp')\" 2>/dev/null || bash -c 'echo > /dev/tcp/localhost/8070'"]
      interval: 30s
      timeout: 5s
      retries: 3

networks:
  firekeepcortex_default:
    external: true
```

- [ ] **Step 3: Create .env.example**

`/opt/CortexBridge/.env.example`:
```bash
# CortexBridge Configuration
# All variables are prefixed with CB_

# Redis (DB 3 to avoid FirekeepCortex collision)
CB_REDIS_URL=redis://localhost:6379/3

# MCP Server
CB_MCP_HOST=0.0.0.0
CB_MCP_PORT=8070

# FirekeepCortex Integration (for distillation)
CB_FIREKEEP_API_URL=http://localhost:8100
CB_FIREKEEP_API_KEY=
CB_FIREKEEP_NAMESPACE=cortexbridge

# Session Management
CB_SESSION_TTL_DAYS=7
CB_MAX_SESSIONS=100
CB_DEFAULT_AGENT_ID=default
```

- [ ] **Step 4: Build and start**

```bash
cd /opt/CortexBridge
docker compose up --build -d
```

- [ ] **Step 5: Verify it's running**

```bash
docker ps | grep cortexbridge
# Should show container running on port 8070
```

- [ ] **Step 6: Commit**

```bash
git add Dockerfile docker-compose.yml .env.example
git commit -m "feat: Docker deployment with FirekeepCortex network integration"
```

### Task 8: Integration Test

- [ ] **Step 1: End-to-end test via MCP**

Use curl or a quick Python script to test the full flow:

```bash
# Start session
curl -s -X POST http://localhost:8070/mcp -H "Content-Type: application/json" -d '...'

# Or use Python:
python3 -c "
import asyncio
from fastmcp import Client

async def test():
    async with Client('http://localhost:8070/mcp') as c:
        # Start session
        r = await c.call_tool('ctx_start_session', {'goal': 'test integration'})
        print('start:', r)

        # Update plan
        r = await c.call_tool('ctx_update', {'category': 'plan', 'content': '- [ ] Step 1\n- [ ] Step 2'})
        print('update plan:', r)

        # Update decision
        r = await c.call_tool('ctx_update', {'category': 'decision', 'content': 'chose approach B'})
        print('update decision:', r)

        # Get shadow
        r = await c.call_tool('ctx_get_shadow', {})
        print('shadow:', r)

        # Complete
        r = await c.call_tool('ctx_complete_session', {'outcome': 'integration test passed'})
        print('complete:', r)

        # List
        r = await c.call_tool('ctx_list_sessions', {})
        print('list:', r)

asyncio.run(test())
"
```

- [ ] **Step 2: Final commit**

```bash
git add -A
git commit -m "chore: integration test and final polish"
```

---

## Post-Implementation

### Claude Code MCP Config

Add to `~/.claude/claude_desktop_config.json` or equivalent:

```json
{
  "mcpServers": {
    "cortexbridge": {
      "url": "http://YOUR_VPS_IP:8070/mcp"
    }
  }
}
```

### Verify Tools Appear

Start Claude Code and run `/mcp` — you should see 7 CortexBridge tools alongside the FirekeepCortex tools.
