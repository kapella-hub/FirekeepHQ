"""CollectorEngine — source-agnostic orchestration for scheduled collectors (SP3).

Enabled-gate first (no clients if disabled) → SETNX lock (serializes runs, which
is what makes the per-run Vault module-global safe) → in-run client build/close →
PAT resolution, env-first (a truthy pat_env_value skips Vault entirely) with
Vault bootstrap + PAT fetch as fallback (engine-owned fail-fast) → adapter build
→ change detection → ingest → run record → collection.sync emit. Never raises."""
from __future__ import annotations

import logging
from functools import partial

import redis.asyncio

from app.collectors.state import CollectorState
from app.db.vector import VectorClient
from app.knowledge.ingest_core import ingest_knowledge_document
from replay.emitter import emit, init_emitter

logger = logging.getLogger(__name__)


async def _bootstrap_vault_and_pat(pat_vault_key: str) -> str | None:
    """Init the Vault module in-worker (its init only runs in the API lifespan),
    then fetch the PAT. Returns the token, or None on any failure (uninitialized
    RuntimeError, absent key, or empty value). Closes its own DB7 client."""
    from vault.config import get_vault_settings
    from vault.store import init_vault, retrieve_secret
    vs = get_vault_settings()
    vr = redis.asyncio.from_url(vs.REDIS_URL, decode_responses=True)
    try:
        init_vault(vr, vs.KEY)
        secret = await retrieve_secret(pat_vault_key)   # raises RuntimeError if uninit
        if not secret or not secret.get("value"):
            return None
        return secret["value"]
    except Exception:
        logger.exception("Vault bootstrap / PAT fetch failed")
        return None
    finally:
        await vr.aclose()


class CollectorEngine:
    async def run(self, adapter_factory, *, name: str, enabled: bool, pat_vault_key: str,
                   pat_env_value: str | None = None, settings) -> dict:
        if not enabled:
            return {"status": "disabled"}

        lock_key = f"collector:lock:{name}"
        r = None
        try:
            r = redis.asyncio.from_url(settings.REDIS_URL, decode_responses=True)
            got = await r.set(lock_key, "1", nx=True, ex=settings.COLLECTOR_LOCK_TTL_SECONDS)
        except Exception:
            logger.exception("Collector %s: lock acquisition failed", name)
            if r is not None:
                try:
                    await r.aclose()
                except Exception:
                    logger.debug("redis close failed after lock acquisition error")
            return {"status": "error", "health": "error"}

        if not got:
            try:
                await r.aclose()
            except Exception:
                logger.debug("redis close failed after lock contention")
            return {"status": "locked"}

        vector = VectorClient(settings)
        adapter = None
        seen_i = ingested = skipped = errors = 0
        health = "ok"
        try:
            # Env-first PAT resolution (SP3): a truthy pat_env_value (K8s Secret /
            # .env CONFLUENCE_PAT) short-circuits Vault entirely — Vault is never
            # touched in that case. Empty/None falls back to the existing Vault
            # bootstrap path, unchanged.
            pat = pat_env_value or await _bootstrap_vault_and_pat(pat_vault_key)
            if not pat:
                # Engine-owned fail-fast: no PAT → no ingest. Set the run vars and
                # let the single record_run in finally persist health="error" once
                # (an in-branch record_run would be overwritten by finally's call).
                errors = 1
                health = "error"
                return {"status": "error", "health": "error"}

            adapter = adapter_factory(pat)
            seen_cb = partial(CollectorState.seen_version, name)
            changed = await adapter.discover_changed(lambda pid: seen_cb(pid, r))
            for item in changed:
                seen_i += 1
                try:
                    md, source_name, source_type = await adapter.fetch_content(item)
                    await ingest_knowledge_document(md, source_name, source_type, vector=vector, redis=r)
                    await CollectorState.record_version(name, item["stable_id"], item["version"], r)
                    ingested += 1
                except Exception:
                    logger.exception("Collector %s: page %s failed", name, item.get("stable_id"))
                    errors += 1
            # discover_changed returns only CHANGED items; total-seen lives on the
            # adapter (last_total_seen). Derive pages_skipped from it.
            total = getattr(adapter, "last_total_seen", seen_i)
            skipped = max(0, total - ingested - errors)
            seen_i = total
            if errors:
                health = "degraded"
        except Exception:
            logger.exception("Collector %s run failed", name)
            health = "error"
            errors += 1
        finally:
            try:
                await CollectorState.record_run(name, seen=seen_i, ingested=ingested,
                                                skipped=skipped, errors=errors, health=health, redis=r)
            except Exception:
                logger.exception("record_run failed for %s", name)
            try:
                # Re-init EVERY run (not once-per-process): each Celery task runs
                # in a fresh asyncio.run() loop on a --pool=solo worker, so a
                # cached emitter Redis client's pool would bind to a closed loop
                # after the first run and every later emit would die (the D7
                # anti-pattern the vault bootstrap above already avoids by
                # rebuilding per-run). Mirrors agent_gateway_sweep.py, which
                # calls init_emitter() unconditionally on every task invocation.
                await init_emitter()
                await emit("collection.sync", session_id=f"collector:{name}", agent_id="collector",
                           payload={"seen": seen_i, "ingested": ingested, "skipped": skipped,
                                    "errors": errors, "health": health})
            except Exception:
                logger.debug("collection.sync emit failed (non-critical)")
            if adapter is not None:
                try:
                    await adapter.aclose()
                except Exception:
                    logger.debug("adapter aclose failed")
            try:
                await vector.close()
            except Exception:
                logger.debug("vector close failed")
            try:
                await r.delete(lock_key)
                await r.aclose()
            except Exception:
                logger.debug("lock release / redis close failed")
        return {"status": health, "health": health, "seen": seen_i,
                "ingested": ingested, "skipped": skipped, "errors": errors}
