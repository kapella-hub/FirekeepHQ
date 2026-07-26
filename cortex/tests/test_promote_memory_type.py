"""Tests for the SP0 memory_type promotion migration (spec B2)."""

from __future__ import annotations

import importlib.util
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "scripts", "promote_memory_type.py",
)
_spec = importlib.util.spec_from_file_location("promote_memory_type", _SCRIPT_PATH)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


def _point(pid: str, payload: dict) -> MagicMock:
    p = MagicMock()
    p.id = pid
    p.payload = payload
    return p


class TestPromoteMemoryType:
    @pytest.mark.asyncio
    async def test_promotes_nested_memory_type(self):
        client = AsyncMock()
        client.scroll = AsyncMock(side_effect=[(
            [
                _point("a", {"metadata": {"memory_type": "reference"}}),
                _point("b", {"metadata": {"memory_type": "reference"}}),
                _point("c", {"metadata": {"memory_type": "episodic"}}),
            ],
            None,
        )])

        result = await mig.promote_memory_type(client, "firekeep_memory")

        assert result["promoted"] == 3
        # One set_payload per distinct value (batched by value)
        assert client.set_payload.await_count == 2
        calls = {
            c.kwargs["payload"]["memory_type"]: set(c.kwargs["points"])
            for c in client.set_payload.await_args_list
        }
        assert calls["reference"] == {"a", "b"}
        assert calls["episodic"] == {"c"}

    @pytest.mark.asyncio
    async def test_idempotent_skips_already_promoted_points(self):
        client = AsyncMock()
        client.scroll = AsyncMock(side_effect=[(
            [
                # Already top-level: must not be touched again
                _point("a", {"memory_type": "reference",
                             "metadata": {"memory_type": "reference"}}),
                # No memory_type anywhere: left alone (GC half-life fallback)
                _point("b", {"metadata": {}}),
            ],
            None,
        )])

        result = await mig.promote_memory_type(client, "firekeep_memory")

        assert result["promoted"] == 0
        assert result["skipped"] == 2
        client.set_payload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dry_run_writes_nothing(self):
        client = AsyncMock()
        client.scroll = AsyncMock(side_effect=[(
            [_point("a", {"metadata": {"memory_type": "procedural"}})],
            None,
        )])

        result = await mig.promote_memory_type(client, "firekeep_memory", dry_run=True)

        assert result["promoted"] == 1
        assert result["dry_run"] is True
        client.set_payload.assert_not_awaited()
