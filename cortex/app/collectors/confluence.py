"""Confluence Server/Data-Center adapter (SP3). Two-phase fetch: metadata CQL
search (paginate via _links.next, expand version+space) then per-changed-page
body. Storage-XHTML -> markdown via markdownify (guarded import)."""
from __future__ import annotations

import logging
import urllib.parse

import httpx

from app.collectors.base import SourceItem

logger = logging.getLogger(__name__)

try:
    from markdownify import markdownify as _markdownify
    _MD_OK = True
except Exception:                    # missing dep on a non-rebuilt image
    _MD_OK = False


def _to_markdown(xhtml: str) -> str:
    if not xhtml:
        return ""
    if not _MD_OK:
        raise RuntimeError("markdownify not installed — rebuild the image")
    return _markdownify(xhtml, strip=["img"]).strip()


def _q(v: str) -> str:
    return '"' + v.replace('"', '\\"') + '"'


def _build_cql(space_keys: list[str], label: str) -> str:
    spaces = ",".join(_q(k.strip()) for k in space_keys if k.strip())
    cql = f'type=page AND space in ({spaces})'
    if label.strip():
        cql += f' AND label={_q(label.strip())}'
    return cql


class ConfluenceAdapter:
    name = "confluence"

    def __init__(self, settings, pat: str):
        self._settings = settings
        self._base = settings.CONFLUENCE_BASE_URL.rstrip("/")
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {pat}",
                     "Accept": "application/json"},
            timeout=30.0,
        )
        self.last_total_seen = 0

    async def discover_changed(self, seen) -> list[SourceItem]:
        keys = [k for k in self._settings.CONFLUENCE_SPACE_KEYS.split(",") if k.strip()]
        cql = _build_cql(keys, self._settings.CONFLUENCE_LABEL)
        url = (f"{self._base}/rest/api/content/search?"
               f"cql={urllib.parse.quote(cql)}&expand=version,space&limit=200")
        total = 0
        changed: list[SourceItem] = []
        while url:
            resp = await self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
            for r in data.get("results", []):
                total += 1
                pid = str(r["id"])
                version = int(r.get("version", {}).get("number", 0))
                if version > await seen(pid):
                    changed.append(SourceItem(
                        stable_id=pid, version=version, label=r.get("title", ""),
                        meta={"space_key": r.get("space", {}).get("key", ""),
                              "title": r.get("title", "")}))
            nxt = (data.get("_links") or {}).get("next")
            if nxt:
                base = (data.get("_links") or {}).get("base", self._base)
                url = nxt if nxt.startswith("http") else base.rstrip("/") + nxt
            else:
                url = None
        self.last_total_seen = total
        return changed

    async def fetch_content(self, item: SourceItem):
        url = f"{self._base}/rest/api/content/{item['stable_id']}?expand=body.storage,version"
        resp = await self._client.get(url)
        resp.raise_for_status()
        xhtml = resp.json().get("body", {}).get("storage", {}).get("value", "")
        md = _to_markdown(xhtml)
        sk = item["meta"].get("space_key", "")
        title = item["meta"].get("title", "")
        return (md, f"Confluence:{sk}:{title}", "wiki")

    async def aclose(self) -> None:
        await self._client.aclose()


def build_confluence_adapter(settings, pat: str) -> ConfluenceAdapter:
    return ConfluenceAdapter(settings, pat)


# Deliberately imported HERE, after the adapter definitions: the celery task
# below needs celery_app at module scope, and importing the worker module any
# earlier would run its import side effects before this module's public surface
# exists. Placement is load-bearing — hence the per-line noqa, not a reorder.
import asyncio  # noqa: E402
from typing import Any  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.collectors.engine import CollectorEngine  # noqa: E402
from app.workers.sleep_cycle import celery_app  # noqa: E402


@celery_app.task(name="app.collectors.confluence.run_confluence_collector")
def run_confluence_collector() -> dict[str, Any]:
    """Scheduled Confluence sync. Inert unless COLLECTORS_ENABLED and
    CONFLUENCE_COLLECTOR_ENABLED. No-op (rather than firing a malformed CQL
    query) if no space keys are configured. Never raises."""
    s = get_settings()
    if not s.CONFLUENCE_SPACE_KEYS.strip():
        return {"status": "disabled", "reason": "no space keys"}
    try:
        return asyncio.run(CollectorEngine().run(
            lambda pat: build_confluence_adapter(s, pat),
            name="confluence",
            enabled=bool(s.COLLECTORS_ENABLED and s.CONFLUENCE_COLLECTOR_ENABLED),
            pat_vault_key=s.CONFLUENCE_PAT_VAULT_KEY,
            pat_env_value=(s.CONFLUENCE_PAT.strip() or None),  # strip: K8s secret mounts often carry a trailing newline
            settings=s,
        ))
    except Exception as e:
        logger.exception("run_confluence_collector crashed")
        return {"status": "error", "error": str(e)}
