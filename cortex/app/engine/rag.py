"""RAG (Retrieval-Augmented Generation) engine for FirekeepCortex.

Performs concurrent dual-retrieval from the knowledge graph (Neo4j) and
semantic memory (Qdrant), merges and scores results, and produces a
Markdown context block suitable for LLM system prompt injection.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.db.graph import Neo4jClient
from app.db.vector import VectorClient
from app.models import ContextQuery, MemorySource, RecallResponse

logger = logging.getLogger(__name__)

# Regex pattern for detecting error-related queries.
_ERROR_PATTERN = re.compile(
    r"\b(error|fail|bug|crash|exception|broken|wrong|issue)\b",
    re.IGNORECASE,
)

# Minimum Jaccard similarity to consider two text entries as matching.
_JACCARD_MATCH_THRESHOLD = 0.3

# Composite score below which a graph hit is traversal noise rather than
# knowledge. A node reached at distance 10 with no token overlap scores
# 0.6 * (1/10) = 0.06 purely because it was reachable — it then occupies a
# top_k slot and, worse, props up the recall's own confidence band. The floor
# sits above that and below a plain distance-3 hit (0.2), so nothing a
# MULTIHOP_MAX_HOPS traversal can legitimately return is affected.
_GRAPH_MIN_SCORE = 0.1

# Lifecycle statuses in descending order of how usable the memory is. A graph
# row may back onto several vector memories; it is admitted on the best of
# them, so one archived sibling cannot suppress live knowledge.
_LIFECYCLE_PRECEDENCE = ("active", "superseded", "deprecated", "archived")


def _tokenize(text: str) -> set[str]:
    """Split text into lowercase word tokens."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Compute Jaccard similarity between two token sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# Attribution values that carry no information. "unknown" is what `upsert`
# stores when no X-Agent-Id header reached /memory/learn; the legacy sentinel
# tags the ~3.9K records written before the field existed. Rendering either on
# every line trains a reader to skip the suffix, which costs the lines that do
# name someone.
_UNATTRIBUTED = {"unknown", "legacy-pre-team-continuity"}

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _provenance_suffix(metadata: Any) -> str:
    """Render "who wrote this, and when" for one recall line.

    An agent reading a memory cannot otherwise tell a teammate's hard-won note
    from a CI bot's noise — or from its own output written minutes ago, which is
    the case that makes recall look like it is working when it is not.

    Only the date is kept, not the clock: staleness is what a reader acts on, and
    a per-line timestamp is pure cost against `token_budget`. session_id is
    deliberately NOT rendered — 32 hex chars an LLM cannot use — but it does reach
    `sources[].metadata`, where an auditor can join on it.
    """
    if not isinstance(metadata, dict):
        return ""

    agent = str(metadata.get("agent_id") or "").strip()
    if agent.lower() in _UNATTRIBUTED:
        agent = ""

    stamp = str(metadata.get("timestamp") or "")[:10]
    date = stamp if _ISO_DATE.fullmatch(stamp) else ""

    parts = [p for p in (agent, date) if p]
    return f" — {', '.join(parts)}" if parts else ""


def _memory_ids_of(row: Any) -> list[str]:
    """Read the Qdrant memory IDs a graph row is linked to.

    Neo4j projects the property as an empty list when absent, but rows also
    arrive from older queries and from tests that predate the back-link, so
    a missing or malformed value degrades to "unlinked" rather than raising.
    """
    if not isinstance(row, dict):
        return []
    raw = row.get("memory_ids")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(m) for m in raw if m]


def _min_max_normalize(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize entry scores to [0, 1] via min-max normalization.

    If the set has a single item, its score is capped at 1.0.
    If all scores are identical, they are all set to 1.0.
    """
    if not entries:
        return entries

    scores = [e["score"] for e in entries]
    min_s = min(scores)
    max_s = max(scores)

    if len(entries) == 1:
        entries[0]["score"] = min(entries[0]["score"], 1.0)
        return entries

    if max_s == min_s:
        for e in entries:
            e["score"] = 1.0
        return entries

    for e in entries:
        e["score"] = (e["score"] - min_s) / (max_s - min_s)

    return entries


def estimate_tokens(text: str) -> int:
    """Rough token count: 4 chars ≈ 1 token."""
    return len(text) // 4


def trim_to_budget(entries: list[dict], budget: int) -> list[dict]:
    """Trim lowest-ranked entries to fit within token budget. Always keeps ≥2."""
    if len(entries) <= 2:
        return entries
    kept = []
    total = 0
    for entry in entries:  # entries are already sorted by score desc
        tokens = estimate_tokens(entry.get("content", entry.get("text", "")))
        if total + tokens <= budget or len(kept) < 2:
            kept.append(entry)
            total += tokens
        if total >= budget and len(kept) >= 2:
            break
    return kept


async def synthesize_memories(
    task: str,
    entries: list[dict],
    llm_base_url: str,
    llm_model: str,
    llm_api_key: str = "",
) -> str | None:
    """Call LLM to synthesize entries into a task-focused paragraph. Returns None on failure."""
    memories_text = "\n\n".join(
        f"[{i+1}] {e.get('content', e.get('text', ''))}"
        for i, e in enumerate(entries)
    )
    system_prompt = (
        "Synthesize the following memories into a focused, concise paragraph "
        "(≤200 words) relevant to the task. Preserve specific facts, file paths, "
        "and names. Do not add information not present in the memories."
    )
    payload = {
        "model": llm_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Task: {task}\n\nMemories:\n{memories_text}"},
        ],
        "temperature": 0.1,
    }
    headers = {"Content-Type": "application/json"}
    if llm_api_key:
        headers["Authorization"] = f"Bearer {llm_api_key}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{llm_base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


class RAGEngine:
    """Dual-retrieval cognitive engine combining graph and vector search."""

    STATUS_MULTIPLIERS = {
        "active": 1.0,
        "superseded": 0.5,
        "deprecated": 0.1,
        "archived": 0.0,
    }

    # Rank for an archived memory the caller explicitly asked to see. Low
    # enough that a live result always outranks it, non-zero so it is not
    # confused with the ordinary "archived is gone" path.
    ARCHIVED_INCLUDED_MULTIPLIER = 0.1

    def __init__(
        self,
        graph: Neo4jClient,
        vector: VectorClient,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._graph = graph
        self._vector = vector
        self._settings = settings or get_settings()
        self._http_client = http_client

    async def recall(
        self, query: ContextQuery, *, workspace_id: str | None = None
    ) -> RecallResponse:
        """Retrieve, merge, score, and format memory context for an LLM.

        Steps:
            1. Concurrent dual-retrieval from Qdrant and Neo4j.
            2. Optionally query resolutions for error-related tasks.
            2b. Verify linked graph rows against the vector lifecycle.
            3. Apply memory decay based on age.
            4. Normalize scores to [0, 1] per source via min-max.
            5. Fuzzy-match entries across stores; boost cross-referenced items.
            6. Deduplicate, sort by score descending, take top_k (capped and
               backfilled via `_take_top_k`).
            7. Optionally re-rank via LLM.
            8. Format as structured Markdown.
        """
        include_archived = bool(getattr(query, "include_archived", False))

        vector_results, graph_results, vector_degraded = await self._dual_retrieve(
            query, workspace_id=workspace_id
        )

        vector_entries = self._normalize_vector(vector_results)
        graph_entries = self._format_graph_entries(graph_results, query.task)

        # Wire query_resolutions for error-related queries.
        if _ERROR_PATTERN.search(query.task):
            resolution_entries = await self._fetch_resolutions(query.task)
            graph_entries.extend(resolution_entries)

        # Qdrant owns lifecycle state; a graph row that names a vector memory
        # is only as recallable as that memory is. Done before scoring so a
        # dropped row cannot influence min-max normalization.
        graph_entries = await self._verify_graph_lifecycle(
            graph_entries, include_archived=include_archived
        )

        # Apply memory decay BEFORE min-max normalization so an aged entry
        # cannot be re-pinned to 1.0 by rescaling, and undated graph entries
        # gain no relative advantage (SP0 C5, defect #13).
        self._apply_decay(vector_entries)
        self._apply_decay(graph_entries)

        # Min-max normalize each source independently.
        vector_entries = _min_max_normalize(vector_entries)
        graph_entries = _min_max_normalize(graph_entries)

        merged = self._merge_and_boost(vector_entries, graph_entries)

        # Apply lifecycle scoring (status + confidence multipliers).
        merged = self._apply_lifecycle_scoring(
            merged, include_archived=include_archived
        )

        # Sort descending by score, take top_k (or more for re-ranking).
        merged.sort(key=lambda e: e["score"], reverse=True)

        # Re-ranking pass (gated behind config).
        if self._settings.RERANK_ENABLED:
            candidate_count = query.top_k * self._settings.RERANK_CANDIDATES_MULTIPLIER
            candidates = merged[:candidate_count]
            merged = await self._rerank(query.task, candidates)
            merged.sort(key=lambda e: e["score"], reverse=True)

        merged = self._take_top_k(merged, query.top_k)

        # Token budget trimming
        final_entries = trim_to_budget(merged, budget=query.token_budget)

        # LLM synthesis (gated behind format and RECALL_SYNTHESIS_ENABLED)
        synthesis_text: str | None = None
        response_format = query.format
        if response_format == "synthesized" and self._settings.RECALL_SYNTHESIS_ENABLED:
            synthesis_text = await synthesize_memories(
                task=query.task,
                entries=final_entries,
                llm_base_url=self._settings.LLM_BASE_URL,
                llm_model=self._settings.LLM_MODEL,
                llm_api_key=getattr(self._settings, "LLM_API_KEY", ""),
            )
            if synthesis_text is None:
                response_format = "raw"  # fallback to raw on LLM failure

        # Build context block
        if synthesis_text:
            sources_md = self._format_markdown(final_entries, query.task, len(final_entries))
            context_block = f"{synthesis_text}\n\n## Sources\n\n{sources_md}"
        else:
            context_block = self._format_markdown(final_entries, query.task, len(final_entries))

        tokens_used = sum(
            estimate_tokens(e.get("content", e.get("text", "")))
            for e in final_entries
        )

        sources = [
            MemorySource(
                store=entry["store"],
                content=entry["content"],
                score=round(entry["score"], 4),
                metadata=entry.get("metadata", {}),
            )
            for entry in final_entries
        ]
        aggregate_score = (
            round(max(s.score for s in sources), 4)
            if sources
            else 0.0
        )

        return RecallResponse(
            context_block=context_block,
            sources=sources,
            score=aggregate_score,
            tokens_used=tokens_used,
            token_budget=query.token_budget,
            format=response_format,
            degraded=vector_degraded,
        )

    # ------------------------------------------------------------------
    # Streaming recall
    # ------------------------------------------------------------------

    async def recall_streaming(
        self, query: ContextQuery, *, workspace_id: str | None = None
    ) -> AsyncGenerator[dict, None]:
        """Yield recall results progressively.

        Yields:
            {"type": "source", "data": MemorySource-like dict}
            {"type": "context", "data": {"context_block": str, "score": float}}
            {"type": "done", "data": {"request_id": str, "total_sources": int}}
        """
        sources: list[dict[str, Any]] = []

        # Fire both searches concurrently, yield results as each completes
        async def _vector_search() -> tuple[str, list[dict[str, Any]]]:
            try:
                results = await self._search_vector(query, workspace_id=workspace_id)
                return "vector", results
            except Exception:
                logger.exception("Vector search failed in streaming recall")
                return "vector", []

        async def _graph_search() -> tuple[str, list[dict[str, Any]]]:
            try:
                results = await self._graph.query_related(
                    query.task, limit=query.top_k,
                    namespace=getattr(query, "namespace", "default"),
                )
            except Exception:
                logger.exception("Graph query failed in streaming recall")
                return "graph", []
            # Same lifecycle gate as the non-streaming path: an archived
            # memory must not resurface through the graph leg just because
            # this caller asked for SSE. (The streaming path still applies no
            # lifecycle/OWM score multipliers — a pre-existing divergence.)
            return "graph", await self._filter_graph_rows(
                results, include_archived=bool(getattr(query, "include_archived", False))
            )

        vector_task = asyncio.create_task(_vector_search())
        graph_task = asyncio.create_task(_graph_search())

        for coro in asyncio.as_completed([vector_task, graph_task]):
            store_name, results = await coro

            if store_name == "vector":
                for r in results:
                    text = r.get("text", "")
                    if not text:
                        continue
                    source = {
                        "store": "vector",
                        "content": text,
                        "score": round(float(r.get("score", 0.0)), 4),
                        "metadata": r.get("metadata", {}),
                    }
                    sources.append(source)
                    yield {"type": "source", "data": source}
            else:
                for r in results:
                    name = r.get("name") or ""
                    description = r.get("description") or ""
                    # A bare node name is not memory content — it must not
                    # compete with real memories in the stream (mirrors
                    # _format_graph_entries' guard, SP0 C5, defect #13).
                    if not description:
                        continue
                    content = description
                    source = {
                        "store": "graph",
                        "content": content,
                        "score": round(1.0 / max(float(r.get("distance", 1) or 1), 1), 4),
                        "metadata": {
                            "name": name,
                            "label": r.get("label", "Entity"),
                        },
                    }
                    sources.append(source)
                    yield {"type": "source", "data": source}

        # Build context block from all sources
        context_block = self._format_markdown(sources, query.task, query.top_k)
        score = max((s["score"] for s in sources), default=0.0)

        yield {
            "type": "context",
            "data": {"context_block": context_block, "score": round(score, 4)},
        }
        yield {
            "type": "done",
            "data": {
                "request_id": str(uuid.uuid4()),
                "total_sources": len(sources),
            },
        }

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    async def _search_vector(
        self, query: ContextQuery, *, workspace_id: str | None = None
    ) -> list[dict[str, Any]]:
        """The one workspace-filtered vector path for regular and SSE recall."""
        return await self._vector.search(
            query.task,
            top_k=query.top_k,
            filter_tags=query.tags or None,
            namespace=getattr(query, "namespace", "default"),
            include_archived=getattr(query, "include_archived", False),
            project=query.project,
            workspace_id=workspace_id,
            score_threshold=self._settings.RECALL_SCORE_FLOOR,
        )

    async def _dual_retrieve(
        self, query: ContextQuery, *, workspace_id: str | None = None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        """Run vector and graph queries concurrently.

        Returns (vector_results, graph_results, vector_degraded).
        The vector search is retried once (read path is latency-bounded —
        contrast the write path's 3-attempt durability retry). If it still
        fails, recall proceeds graph-only and vector_degraded=True so the
        caller can say so instead of silently returning noise (SP0 C4).
        """

        async def _safe_vector() -> tuple[list[dict[str, Any]], bool]:
            attempts = 2  # initial + one retry
            for attempt in range(1, attempts + 1):
                try:
                    results = await self._search_vector(
                        query, workspace_id=workspace_id
                    )
                    return results, False
                except Exception:
                    if attempt < attempts:
                        logger.warning(
                            "Vector search failed (attempt %d/%d), retrying",
                            attempt, attempts,
                        )
                    else:
                        logger.error(
                            "Vector search failed after %d attempts — recall degraded to graph-only",
                            attempts, exc_info=True,
                        )
            return [], True

        async def _safe_graph() -> list[dict[str, Any]]:
            try:
                if self._settings.MULTIHOP_ENABLED:
                    return await self._graph.query_related_multihop(
                        query.task,
                        limit=query.top_k * 3,
                        namespace=getattr(query, "namespace", "default"),
                        max_hops=self._settings.MULTIHOP_MAX_HOPS,
                        decay_per_hop=self._settings.MULTIHOP_DECAY_PER_HOP,
                    )
                return await self._graph.query_related(
                    query.task, limit=query.top_k,
                    namespace=getattr(query, "namespace", "default"),
                )
            except Exception:
                logger.exception("Graph query failed")
                return []

        (vector_results, vector_degraded), graph_results = await asyncio.gather(
            _safe_vector(), _safe_graph()
        )
        return vector_results, graph_results, vector_degraded

    # ------------------------------------------------------------------
    # Resolution retrieval
    # ------------------------------------------------------------------

    async def _fetch_resolutions(self, task: str) -> list[dict[str, Any]]:
        """Query resolution nodes for error-related tasks.

        Extracts keywords from the task and queries each against the graph's
        query_resolutions method. Results are formatted as graph entries with
        a 1.2x bonus score.
        """
        from app.db.graph import Neo4jClient as _NC

        keywords = _NC._extract_keywords(task)
        if not keywords:
            return []

        entries: list[dict[str, Any]] = []
        for kw in keywords:
            try:
                results = await self._graph.query_resolutions(kw, limit=3)
                for r in results:
                    resolution = r.get("resolution") or ""
                    error = r.get("error") or ""
                    if not resolution:
                        continue
                    content = f"Resolution: {resolution}"
                    if error:
                        content = f"[Error: {error}] {content}"
                    metadata: dict[str, Any] = {
                        "name": "resolution",
                        "label": "Resolution",
                        "source_type": "resolution",
                    }
                    memory_ids = _memory_ids_of(r)
                    if memory_ids:
                        metadata["memory_ids"] = memory_ids
                    entries.append(
                        {
                            "content": content,
                            "score": 1.2,  # bonus score (will be normalized)
                            "store": "graph",
                            "metadata": metadata,
                        }
                    )
            except Exception:
                logger.exception("query_resolutions failed for keyword '%s'", kw)

        # Deduplicate by content.
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for e in entries:
            if e["content"] not in seen:
                seen.add(e["content"])
                unique.append(e)
        return unique

    # ------------------------------------------------------------------
    # Score normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_vector(
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Convert raw vector search results into scored entries.

        Vector scores are already cosine similarity in [0, 1].
        """
        entries: list[dict[str, Any]] = []
        for r in results:
            text = r.get("text", "")
            if not text:
                continue
            metadata = dict(r.get("metadata", {}))
            # Raw cosine survives min-max normalization so downstream
            # consumers (Bridge proactive recall) can floor on the real
            # scale (SP0 C4, defect #16).
            metadata["raw_score"] = float(r.get("score", 0.0))
            entries.append(
                {
                    "content": text,
                    "score": float(r.get("score", 0.0)),
                    "store": "vector",
                    "metadata": metadata,
                }
            )
        return entries

    def _format_graph_entries(
        self,
        results: list[dict[str, Any]],
        query: str,
    ) -> list[dict[str, Any]]:
        """Convert raw graph traversal results into scored entries.

        Uses a composite score: weight * text_sim + (1 - weight) * (1/distance)
        where text_sim is Jaccard token overlap between query and node text.
        """
        weight = self._settings.GRAPH_RELEVANCE_WEIGHT
        query_tokens = _tokenize(query)

        entries: list[dict[str, Any]] = []
        for r in results:
            name = r.get("name") or ""
            description = r.get("description") or ""
            label = r.get("label") or "Entity"
            distance = r.get("distance")

            # A bare node name is not memory content — it must not compete
            # with real memories for top_k slots (SP0 C5, defect #13).
            if not description:
                continue
            content = description

            # Distance component: 1/distance, default 0.5 if no distance.
            dist_score = 1.0 / max(int(distance), 1) if distance else 0.5

            # Text similarity component: Jaccard on query vs name+description.
            node_text = f"{name} {description}".strip()
            node_tokens = _tokenize(node_text)
            text_sim = _jaccard_similarity(query_tokens, node_tokens)

            score = weight * text_sim + (1 - weight) * dist_score

            # Reachable is not relevant: a far, non-overlapping node scores
            # near zero and is dropped rather than padding the result set.
            if score < _GRAPH_MIN_SCORE:
                continue

            metadata: dict[str, Any] = {
                "name": name,
                "label": label,
                "distance": distance,
                # The real relevance, preserved before _min_max_normalize
                # rescales `score` into a within-set RANK. Vector entries
                # already did this (_normalize_vector); graph ones did not,
                # so a graph hit had no honest number to display.
                "raw_score": round(float(score), 4),
            }
            memory_ids = _memory_ids_of(r)
            if memory_ids:
                metadata["memory_ids"] = memory_ids

            entries.append(
                {
                    "content": content,
                    "score": score,
                    "store": "graph",
                    "metadata": metadata,
                }
            )
        return entries

    # ------------------------------------------------------------------
    # Lifecycle verification of graph results
    # ------------------------------------------------------------------

    async def _resolve_lifecycle(
        self, memory_ids: list[str]
    ) -> dict[str, dict[str, Any]] | None:
        """Fetch vector lifecycle state for graph-linked memory IDs.

        Returns the id→state map, or None when the vector store could not
        answer. None is a distinct outcome from an empty map: an empty map
        means those memories are genuinely gone (fail closed), whereas an
        unreachable store must not silently erase the graph leg — which is
        the one leg that still works when Qdrant is down.
        """
        if not memory_ids:
            return {}
        try:
            states = await self._vector.get_lifecycle_states(memory_ids)
        except Exception:
            logger.warning(
                "Lifecycle verification unavailable — graph results returned unverified",
                exc_info=True,
            )
            return None
        return states if isinstance(states, dict) else None

    @staticmethod
    def _lifecycle_verdict(
        states: dict[str, dict[str, Any]] | None,
        memory_ids: list[str],
        include_archived: bool,
    ) -> tuple[bool, str | None]:
        """Decide one graph row: ``(admit, verified_status)``.

        A ``None`` status means "not verified" — either the row is unlinked
        (sleep-cycle and legacy knowledge that never had a vector record, and
        which must stay recallable) or the lookup was unavailable. A linked
        row whose memories have all vanished is refused: Qdrant is
        authoritative, and a dangling link is not evidence of a live memory.
        """
        if not memory_ids or states is None:
            return True, None

        resolved = [states[m] for m in memory_ids if m in states]
        if not resolved:
            return False, None

        status = min(
            (str(s.get("status") or "active") for s in resolved),
            key=lambda s: _LIFECYCLE_PRECEDENCE.index(s)
            if s in _LIFECYCLE_PRECEDENCE
            else 0,
        )
        if status == "archived" and not include_archived:
            return False, status
        return True, status

    async def _verify_graph_lifecycle(
        self, entries: list[dict[str, Any]], include_archived: bool = False
    ) -> list[dict[str, Any]]:
        """Drop graph entries whose backing vector memory forbids recall.

        Survivors are annotated with ``lifecycle_verified`` so a caller can
        tell a checked row from graph-owned knowledge, rather than having a
        vector lifecycle invented for it.
        """
        ids: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            for mid in (entry.get("metadata") or {}).get("memory_ids") or []:
                if mid not in seen:
                    seen.add(mid)
                    ids.append(mid)

        states = await self._resolve_lifecycle(ids)

        kept: list[dict[str, Any]] = []
        for entry in entries:
            metadata = entry.setdefault("metadata", {})
            admit, status = self._lifecycle_verdict(
                states, metadata.get("memory_ids") or [], include_archived
            )
            if not admit:
                continue
            metadata["lifecycle_verified"] = status is not None
            if status is not None:
                metadata["status"] = status
            kept.append(entry)
        return kept

    async def _filter_graph_rows(
        self, rows: list[dict[str, Any]], include_archived: bool = False
    ) -> list[dict[str, Any]]:
        """Lifecycle gate for raw graph rows (the streaming path)."""
        ids: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for mid in _memory_ids_of(row):
                if mid not in seen:
                    seen.add(mid)
                    ids.append(mid)

        states = await self._resolve_lifecycle(ids)
        return [
            row
            for row in rows
            if self._lifecycle_verdict(
                states, _memory_ids_of(row), include_archived
            )[0]
        ]

    # ------------------------------------------------------------------
    # Merge, boost, and deduplicate
    # ------------------------------------------------------------------

    def _merge_and_boost(
        self,
        vector_entries: list[dict[str, Any]],
        graph_entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge entries from both stores, boosting cross-referenced items.

        For each pair of (vector_entry, graph_entry) whose content matches
        (via substring check or Jaccard similarity), the higher-scoring entry
        is kept with its score multiplied by the boost factor and its store
        set to ``"both"``.  The other entry is consumed (removed).
        """
        boost_factor = self._settings.BOOST_FACTOR

        # Track which graph entries have been consumed by a cross-ref match.
        graph_consumed: set[int] = set()
        merged: list[dict[str, Any]] = []

        for v_entry in vector_entries:
            matched = False
            for g_idx, g_entry in enumerate(graph_entries):
                if g_idx in graph_consumed:
                    continue
                if self._is_fuzzy_match(v_entry["content"], g_entry["content"]):
                    # Cross-referenced: combine the best score with boost.
                    best_score = max(v_entry["score"], g_entry["score"])
                    boosted_score = min(best_score * boost_factor, 1.0)

                    merged.append(
                        {
                            "content": v_entry["content"],
                            "score": boosted_score,
                            "store": "both",
                            "metadata": {
                                **v_entry.get("metadata", {}),
                                "graph_name": g_entry.get("metadata", {}).get(
                                    "name", ""
                                ),
                                "graph_label": g_entry.get("metadata", {}).get(
                                    "label", ""
                                ),
                            },
                        }
                    )
                    graph_consumed.add(g_idx)
                    matched = True
                    break

            if not matched:
                merged.append(v_entry)

        # Add remaining (un-consumed) graph entries.
        for g_idx, g_entry in enumerate(graph_entries):
            if g_idx not in graph_consumed:
                merged.append(g_entry)

        return merged

    @staticmethod
    def _take_top_k(entries: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        """Take up to top_k entries, capping graph-only entries at max(1, top_k // 2).

        Graph entries lack timestamps and full memory content, so an uncapped
        merge lets them crowd real memories out of top_k (defect #13). If
        vector/both entries cannot fill top_k, skipped graph entries backfill
        the remainder. Input must already be sorted by score descending.
        """
        graph_cap = max(1, top_k // 2)
        selected: list[dict[str, Any]] = []
        skipped_graph: list[dict[str, Any]] = []
        graph_count = 0
        for entry in entries:
            if len(selected) >= top_k:
                break
            if entry["store"] == "graph":
                if graph_count >= graph_cap:
                    skipped_graph.append(entry)
                    continue
                graph_count += 1
            selected.append(entry)
        for entry in skipped_graph:
            if len(selected) >= top_k:
                break
            selected.append(entry)
        selected.sort(key=lambda e: e["score"], reverse=True)
        return selected

    @staticmethod
    def _is_fuzzy_match(a: str, b: str) -> bool:
        """Return True if the two strings are sufficiently similar.

        Primary: case-insensitive substring check (either direction).
        Fallback: Jaccard similarity on word token sets, threshold 0.3.
        """
        if not a or not b:
            return False
        a_lower = a.lower()
        b_lower = b.lower()

        # Primary: substring check (graph content in vector content or vice versa).
        if b_lower in a_lower or a_lower in b_lower:
            return True

        # Fallback: Jaccard token similarity.
        a_tokens = _tokenize(a)
        b_tokens = _tokenize(b)
        return _jaccard_similarity(a_tokens, b_tokens) >= _JACCARD_MATCH_THRESHOLD

    # ------------------------------------------------------------------
    # Memory decay
    # ------------------------------------------------------------------

    _DECAY_HALF_LIFE_MAP = {
        "reference": "DECAY_REFERENCE_DAYS",
        "procedural": "DECAY_PROCEDURAL_DAYS",
        "episodic": "DECAY_EPISODIC_DAYS",
        "transient": "DECAY_TRANSIENT_DAYS",
    }

    def _apply_decay(self, entries: list[dict[str, Any]]) -> None:
        """Apply exponential memory decay based on entry age and memory type.

        Formula: decayed_score = score * 2^(-age_days / half_life)
        Each memory type has its own half-life (configured in Settings).
        If half_life is 0 (e.g. reference type), decay is skipped.
        If entry has no timestamp, score is unchanged.
        """
        now = datetime.now(timezone.utc)
        for entry in entries:
            metadata = entry.get("metadata", {})

            # Determine per-type half-life, falling back to global default.
            memory_type = metadata.get("memory_type", "episodic")
            config_attr = self._DECAY_HALF_LIFE_MAP.get(memory_type)
            if config_attr:
                half_life = getattr(self._settings, config_attr)
            else:
                half_life = self._settings.MEMORY_DECAY_HALF_LIFE_DAYS

            if half_life <= 0:
                continue

            timestamp_str = metadata.get("timestamp")
            if not timestamp_str:
                continue
            try:
                ts = datetime.fromisoformat(str(timestamp_str))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age_days = (now - ts).total_seconds() / 86400.0
                if age_days > 0:
                    decay = 2.0 ** (-age_days / half_life)
                    entry["score"] *= decay
            except (ValueError, TypeError):
                # Unparseable timestamp — skip decay for this entry.
                continue

    # ------------------------------------------------------------------
    # Lifecycle scoring
    # ------------------------------------------------------------------

    def _apply_lifecycle_scoring(
        self,
        items: list[dict[str, Any]],
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """Apply lifecycle status and confidence multipliers to scored items.

        Each item's score is adjusted by:
          score * status_multiplier * confidence_factor

        Where confidence_factor = (1 + confirmed_count) / (1 + contradicted_count)

        Items with status="archived" are removed entirely (score=0), unless
        the caller explicitly asked for them — a recovery/audit read, where
        dropping the very rows that were requested would be a silent lie.
        Those are ranked at ARCHIVED_INCLUDED_MULTIPLIER so they sort below
        anything live rather than being reinstated at full strength.
        Items are annotated with lifecycle metadata for the context block.
        """
        result = []
        for item in items:
            metadata = item.get("metadata", {})
            status = metadata.get("status", "active")

            # Status multiplier
            multiplier = self.STATUS_MULTIPLIERS.get(status, 1.0)
            if multiplier == 0.0:
                if not include_archived:
                    continue  # Skip archived
                multiplier = self.ARCHIVED_INCLUDED_MULTIPLIER

            # Confidence factor
            confirmed = metadata.get("confirmed_count", 0)
            contradicted = metadata.get("contradicted_count", 0)
            confidence = (1 + confirmed) / (1 + contradicted)

            # OWM (app/owm.py): outcome-weighted efficacy multiplier. Neutral
            # 0.5 — and every memory never scored (field absent) — is exactly
            # 1.0, so pre-OWM ranking is preserved bit-identically until real
            # evidence accumulates. Clamped to [1-W, 1+W].
            owm_mult = 1.0
            eff = metadata.get("owm_efficacy")
            if self._settings.OWM_ENABLED and isinstance(eff, (int, float)):
                w = self._settings.OWM_WEIGHT
                owm_mult = 1.0 + w * 2.0 * (float(eff) - 0.5)
                owm_mult = min(max(owm_mult, 1.0 - w), 1.0 + w)

            item["score"] = item["score"] * multiplier * confidence * owm_mult

            # Annotate for context block
            if status != "active":
                item["_lifecycle_status"] = status
            if metadata.get("superseded_by"):
                item["_superseded_by"] = metadata["superseded_by"]

            result.append(item)

        return result

    # ------------------------------------------------------------------
    # Re-ranking via LLM
    # ------------------------------------------------------------------

    async def _rerank(
        self,
        task: str,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Re-rank candidates by asking the LLM to score relevance.

        Calls the configured LLM endpoint with a relevance scoring prompt.
        If the call fails for any candidate, the original score is kept.
        """
        if not candidates:
            return candidates

        base_url = self._settings.LLM_BASE_URL
        model = self._settings.LLM_MODEL
        api_key = self._settings.LLM_API_KEY

        url = f"{base_url}/chat/completions"
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        client = self._http_client or httpx.AsyncClient(timeout=15.0)
        try:
            tasks = [
                self._rerank_single(client, url, headers, model, task, c)
                for c in candidates
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if not self._http_client:
                await client.aclose()

        reranked: list[dict[str, Any]] = []
        for candidate, result in zip(candidates, results):
            if isinstance(result, Exception):
                logger.warning("Re-rank failed for entry: %s", result)
            elif result is not None:
                candidate["score"] = result
            # else: unparseable response — keep original score
            reranked.append(candidate)

        return reranked

    @staticmethod
    def _parse_rerank_score(text: str) -> float | None:
        """Extract a [0, 1] score from LLM rerank response text.

        Returns None if no valid score can be parsed.
        """
        match = re.search(r"\b(0(?:\.\d+)?|1(?:\.0+)?)\b", text)
        if match:
            return float(match.group(1))
        try:
            value = float(text)
            if 0.0 <= value <= 1.0:
                return value
        except (ValueError, TypeError):
            pass
        return None

    @staticmethod
    async def _rerank_single(
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        model: str,
        task: str,
        candidate: dict[str, Any],
    ) -> float | None:
        """Ask the LLM to score a single candidate's relevance to the task.

        Returns None if the response cannot be parsed as a valid score.
        """
        prompt = (
            "Rate the relevance of this memory to the task on a scale of 0-1. "
            "Respond with ONLY a decimal number between 0 and 1.\n\n"
            f"Task: {task}\n"
            f"Memory: {candidate['content']}"
        )
        response = await client.post(
            url,
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
                "max_tokens": 100,
            },
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
        msg = data["choices"][0]["message"]
        text = (msg.get("content") or "").strip()
        # Fallback: some models (e.g. qwen3) put output in a reasoning field
        if not text:
            text = (msg.get("reasoning") or "").strip()
        return RAGEngine._parse_rerank_score(text)

    # ------------------------------------------------------------------
    # Markdown formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _format_markdown(
        entries: list[dict[str, Any]],
        task: str = "",
        top_k: int = 5,
    ) -> str:
        """Build a structured Markdown block for LLM system prompt injection.

        Output format:
            ## Memory Recall ({n} results, confidence: {high/medium/low})
            1. [{score}] ({source}) {content}
            ...
            > Query: "{task}" | Top {top_k} results
        """
        if not entries:
            return "## Memory Recall (0 results, confidence: low)\n\nNo relevant memories found."

        # Confidence comes from the best REAL relevance, not from `score`.
        #
        # `score` has been through _min_max_normalize, which sets the best entry
        # in the set to exactly 1.0 by construction. Reading the band off it made
        # `confidence: high` unconditional — a recall whose weakest result showed
        # [0%] still announced high confidence, because 0% means "lowest of these
        # three", not "irrelevant". A band that cannot come out low is not a band.
        #
        # raw_score is the pre-normalization value: cosine for vector entries,
        # the weighted jaccard/distance blend for graph ones. Entries without it
        # (resolution bonuses, which carry a sentinel 1.2) are skipped rather than
        # counted, so they cannot prop the band up.
        real = [
            e["metadata"]["raw_score"]
            for e in entries
            if isinstance(e.get("metadata"), dict) and e["metadata"].get("raw_score") is not None
        ]
        max_score = max(real) if real else max(e["score"] for e in entries)
        if max_score > 0.7:
            confidence = "high"
        elif max_score >= 0.4:
            confidence = "medium"
        else:
            confidence = "low"

        n = len(entries)
        lines: list[str] = [
            f"## Memory Recall ({n} result{'s' if n != 1 else ''}, confidence: {confidence})",
            "",
        ]

        # Entries are already sorted by score descending.
        for i, entry in enumerate(entries, 1):
            store = entry["store"]
            content = entry["content"]
            # Show real relevance where we have it; fall back to the normalized
            # rank only for entries that never had a raw score.
            md = entry.get("metadata") or {}
            raw = md.get("raw_score") if isinstance(md, dict) else None
            score = float(raw) if raw is not None else entry["score"]

            # Map internal store names to display labels.
            source_label = store
            if store == "both":
                source_label = "graph+vector"

            # Lifecycle status label.
            status_label = ""
            lifecycle_status = entry.get("_lifecycle_status")
            if lifecycle_status:
                status_label = f" [{lifecycle_status.upper()}]"
            superseded_by = entry.get("_superseded_by")
            if superseded_by:
                status_label += f" (superseded by {superseded_by})"

            provenance = _provenance_suffix(md)

            lines.append(
                f"{i}. [{score:.0%}] ({source_label}) {content}{status_label}{provenance}"
            )

        lines.append("")
        lines.append(f'> Query: "{task}" | Top {top_k}')

        return "\n".join(lines)
