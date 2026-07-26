"""Tests for the Confluence adapter (SP3 Task 7)."""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock
import pytest
from app.collectors.confluence import ConfluenceAdapter, _build_cql


def test_build_cql_spaces_and_label():
    assert _build_cql(["OPS", "NOC"], "runbook") == \
        'type=page AND space in ("OPS","NOC") AND label="runbook"'
    assert _build_cql(["OPS"], "") == 'type=page AND space in ("OPS")'


def _resp(json_body):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=json_body)
    return r


@pytest.mark.asyncio
async def test_discover_changed_paginates_and_filters(monkeypatch):
    settings = MagicMock(CONFLUENCE_BASE_URL="https://wiki.x", CONFLUENCE_SPACE_KEYS="OPS",
                         CONFLUENCE_LABEL="")
    adapter = ConfluenceAdapter(settings, "PAT")
    page1 = {"results": [{"id": "1", "title": "A", "version": {"number": 2},
                          "space": {"key": "OPS"}}],
             "_links": {"next": "/rest/api/content/search?cursor=2", "base": "https://wiki.x"}}
    page2 = {"results": [{"id": "2", "title": "B", "version": {"number": 5},
                          "space": {"key": "OPS"}}], "_links": {}}
    calls = []
    async def fake_get(url, **kw):
        calls.append(url)
        return _resp(page1 if len(calls) == 1 else page2)
    adapter._client.get = AsyncMock(side_effect=fake_get)

    async def seen(pid): return 2 if pid == "1" else 0   # page 1 unchanged, page 2 new
    items = await adapter.discover_changed(seen)
    assert [i["stable_id"] for i in items] == ["2"]       # only changed
    assert items[0]["meta"] == {"space_key": "OPS", "title": "B"}
    assert "expand=version,space" in calls[0] or "expand=version%2Cspace" in calls[0]
    assert len(calls) == 2                                 # followed _links.next
    assert adapter.last_total_seen == 2


@pytest.mark.asyncio
async def test_fetch_content_body_to_markdown():
    settings = MagicMock(CONFLUENCE_BASE_URL="https://wiki.x")
    adapter = ConfluenceAdapter(settings, "PAT")
    body = {"body": {"storage": {"value": "<h1>Restart</h1><p>Run <code>svc restart</code></p>"}}}
    adapter._client.get = AsyncMock(return_value=_resp(body))
    md, source_name, source_type = await adapter.fetch_content(
        {"stable_id": "2", "version": 5, "label": "B", "meta": {"space_key": "OPS", "title": "B"}})
    assert source_name == "Confluence:OPS:B" and source_type == "wiki"
    assert "Restart" in md and "svc restart" in md
