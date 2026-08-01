"""Ticket persistence and the one-EVAL redemption boundary."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

import fakeredis.aioredis
import pytest

from app.enroll.store import (
    KEY_INDEX,
    TICKET_INDEX,
    TICKET_PREFIX,
    EnrollmentSettings,
    EnrollmentStore,
    ticket_id,
)
from app.enroll.mint import encode_join_code


@pytest.mark.asyncio
async def test_issue_writes_tombstone_and_inventory_atomically():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = EnrollmentStore(redis, EnrollmentSettings(ticket_ttl_hours=24, tombstone_days=7))
    try:
        ticket, tid, record = await store.issue(
            agent_label="bob",
            transport="tunnel",
            kind="ports",
            host="127.0.0.1",
            ssh_target="bob@server",
            now=datetime(2026, 7, 31, tzinfo=timezone.utc),
        )
        assert ticket_id(ticket) == tid
        assert await redis.hgetall(f"{TICKET_PREFIX}{tid}") == record
        assert 6 * 86400 < await redis.ttl(f"{TICKET_PREFIX}{tid}") <= 7 * 86400
        assert await redis.zscore(TICKET_INDEX, tid) is not None
        assert not record.get("ticket")
    finally:
        await redis.aclose()


class EvalCaptureRedis:
    def __init__(self):
        self.eval_calls = []

    async def hgetall(self, key):
        return {}

    async def eval(self, *args):
        self.eval_calls.append(args)
        return ["unknown"]


@pytest.mark.asyncio
async def test_consume_uses_exactly_one_eval_and_no_auth_write_helper():
    redis = EvalCaptureRedis()
    store = EnrollmentStore(redis)
    raw_ticket = bytes(range(32))
    import base64
    ticket = base64.urlsafe_b64encode(raw_ticket).decode().rstrip("=")

    outcome, fields, snapshot = await store.consume(
        ticket=ticket,
        credential_hash="a" * 64,
        device_nonce="b" * 16,
        now=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )
    assert (outcome, fields, snapshot) == ("unknown", [], None)
    assert len(redis.eval_calls) == 1
    call = redis.eval_calls[0]
    assert call[1] == 5
    assert call[4] == "auth:key:" + "a" * 64
    assert call[6] == KEY_INDEX


def test_ticket_id_rejects_noncanonical_or_wrong_width_secrets():
    for bad in ("", "abc", "A" * 42, "!" * 43, "A" * 44):
        with pytest.raises(ValueError):
            ticket_id(bad)


def test_server_code_is_accepted_by_the_shipped_client_decoder():
    client_root = str(Path(__file__).resolve().parents[2] / "client")
    if client_root not in sys.path:
        sys.path.insert(0, client_root)
    from firekeep_client.joincode import decode_join_code

    import base64
    ticket = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
    encoded = encode_join_code({
        "v": 1, "t": "tunnel", "k": "ports", "h": "127.0.0.1",
        "s": "root@203.0.113.9", "x": "20260731T120000Z", "q": ticket,
    })
    decoded = decode_join_code(encoded)
    assert decoded.ticket == ticket
    assert decoded.ssh_target == "root@203.0.113.9"
