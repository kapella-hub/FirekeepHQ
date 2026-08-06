"""Proactive recall — fetches relevant memories from FirekeepCortex during updates."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


async def fetch_relevant_memories(
    context: str,
    api_url: str,
    api_key: str | None = None,
    namespace: str | None = None,
    top_k: int = 3,
    min_score: float = 0.35,
    timeout: float = 30.0,
) -> list[dict]:
    """Query FirekeepCortex for memories relevant to the given context.

    Returns a list of {"content": str, "score": float} dicts for matches
    above min_score.  Returns an empty list on ANY error — this is a
    non-blocking enhancement and must never break the update path.
    """
    if not context or len(context.strip()) < 10:
        return []

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    payload = {
        "task": context[:500],
        "top_k": top_k,
        # Hot path: raw list, no LLM synthesis (SP0 C6, defect #11). The 30s
        # default timeout matches the server's own backend budget.
        "format": "raw",
    }
    # Omitted unless the caller names one. `namespace` on Cortex is a CATEGORY,
    # and sending the literal "default" would scope proactive recall to that one
    # category — hiding every memory an agent filed under "infrastructure",
    # "engineering" and the rest, which on the live store is 146 memories, 129 of
    # them active. Omitting the key searches all of them. This is what
    # `FIREKEEP_NAMESPACE`'s "unified with Cortex's default namespace so
    # distillates and proactive recall see the same memories" comment was
    # reaching for; the writes still name their namespace, only the READ is
    # unscoped.
    if namespace is not None:
        payload["namespace"] = namespace

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{api_url}/memory/recall",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        sources = data.get("sources", [])
        if data.get("degraded"):
            logger.warning(
                "Proactive recall degraded (vector search unavailable) — skipping injection"
            )
            return []
        results: list[dict] = []
        for s in sources:
            raw = s.get("metadata", {}).get("raw_score")
            if raw is None:
                # Graph-only bare entry — no cosine score to rank honestly.
                continue
            if raw >= min_score:
                results.append({"content": s["content"], "score": raw})
        return results
    except Exception as exc:
        logger.debug("Proactive recall failed (non-fatal): %s", exc)
        return []
