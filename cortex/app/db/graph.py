"""Neo4j graph database client for FirekeepCortex.

Handles action log persistence, knowledge graph writes from the Sleep Cycle,
and graph traversal queries for the RAG engine.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

import neo4j
from neo4j import AsyncGraphDatabase

from app.exceptions import GraphConnectionError

if TYPE_CHECKING:
    from app.config import Settings
    from app.models import ActionLog

logger = logging.getLogger(__name__)

# Stopwords filtered out during keyword extraction for graph queries.
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "must", "of", "in",
    "to", "for", "with", "on", "at", "from", "by", "as", "into", "about",
    "and", "but", "or", "nor", "not", "so", "yet", "both", "either",
    "neither", "each", "every", "all", "any", "few", "more", "most",
    "other", "some", "such", "no", "only", "same", "than", "too", "very",
    "just", "because", "if", "when", "while", "where", "how", "what",
    "which", "who", "whom", "this", "that", "these", "those", "it", "its",
})

# Domain-specific stopwords: common in engineering contexts but low
# discriminative power for graph lookups.
_DOMAIN_STOPWORDS = frozenset({
    "error", "bug", "fix", "issue", "problem", "code", "file",
    "change", "make", "need", "want", "like", "just", "also",
})

# Synonym map for query expansion.
_SYNONYMS: dict[str, str] = {
    "db": "database",
    "database": "db",
    "auth": "authentication",
    "authentication": "auth",
    "config": "configuration",
    "configuration": "config",
    "env": "environment",
    "environment": "env",
    "deps": "dependencies",
    "dependencies": "deps",
    "perf": "performance",
    "performance": "perf",
    "mem": "memory",
    "memory": "mem",
    "conn": "connection",
    "connection": "conn",
    "err": "error",
    "req": "request",
    "request": "req",
    "res": "response",
    "response": "res",
}

# Valid node labels used in fulltext index and label filtering.
_GRAPH_LABELS = frozenset({"Domain", "Concept", "Action", "Outcome", "Resolution"})


class Neo4jClient:
    """Async Neo4j client for the FirekeepCortex knowledge graph."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._uri = settings.NEO4J_URI
        self._user = settings.NEO4J_USER
        self._password = settings.NEO4J_PASSWORD
        self._driver: neo4j.AsyncDriver | None = None

    async def connect(self) -> None:
        """Create the async Neo4j driver, retrying while the server comes up.

        This is called unconditionally from the FastAPI lifespan, so raising
        here aborts startup and exits the container. On 2026-07-26 that happened
        in production: during a rolling restart cortex-api came up before Neo4j
        had DNS, the first attempt raised "Cannot resolve address neo4j:7687",
        and the API stayed down until a human intervened. One transient blip,
        permanent outage.

        Bounded retry with exponential backoff fixes that in both directions —
        it rides out a dependency that is merely slow, and it still fails loudly
        against one that is genuinely misconfigured rather than hanging a deploy.
        Set NEO4J_CONNECT_ATTEMPTS=1 to restore the old single-shot behaviour.
        """
        attempts = max(1, int(getattr(self._settings, "NEO4J_CONNECT_ATTEMPTS", 1)))
        backoff = float(getattr(self._settings, "NEO4J_CONNECT_BACKOFF_SECONDS", 0.0))
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            driver = None
            try:
                driver = AsyncGraphDatabase.driver(
                    self._uri,
                    auth=(self._user, self._password),
                    max_connection_pool_size=self._settings.NEO4J_POOL_SIZE,
                )
                await driver.verify_connectivity()
            except Exception as exc:  # noqa: BLE001 — any failure is retryable here
                last_exc = exc
                # Never keep a driver whose connectivity check failed: leaving it
                # on self._driver would hand later calls a half-open handle.
                if driver is not None:
                    try:
                        await driver.close()
                    except Exception:  # noqa: BLE001 — closing a dead driver is best-effort
                        pass
                if attempt < attempts:
                    delay = backoff * (2 ** (attempt - 1))
                    logger.warning(
                        "Neo4j connect attempt %d/%d failed (%s); retrying in %.2fs",
                        attempt, attempts, exc, delay,
                    )
                    await asyncio.sleep(delay)
                continue

            self._driver = driver
            if attempt > 1:
                logger.info(
                    "Connected to Neo4j at %s after %d attempts", self._uri, attempt
                )
            else:
                logger.info("Connected to Neo4j at %s", self._uri)
            return

        raise GraphConnectionError(
            f"Failed to connect to Neo4j at {self._uri} after {attempts} attempt(s): {last_exc}"
        ) from last_exc

    async def close(self) -> None:
        """Close the Neo4j driver."""
        if self._driver is not None:
            await self._driver.close()
            self._driver = None
            logger.info("Neo4j driver closed")

    def _ensure_driver(self) -> neo4j.AsyncDriver:
        if self._driver is None:
            raise GraphConnectionError("Neo4j driver is not connected")
        return self._driver

    async def ping(self) -> None:
        """Verify connectivity to Neo4j. Raises on failure."""
        driver = self._ensure_driver()
        await driver.verify_connectivity()

    # ------------------------------------------------------------------
    # Generic query helpers (used by versioning, etc.)
    # ------------------------------------------------------------------

    async def _execute_read(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Execute a read query and return results as a list of dicts."""
        driver = self._ensure_driver()
        async with driver.session() as session:
            result = await session.run(query, params)
            records = await result.data()
            return records

    async def _execute_write(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Execute a write query in an explicit transaction and return results."""
        driver = self._ensure_driver()
        async with driver.session() as session:
            async with session.begin_transaction() as tx:
                result = await tx.run(query, params)
                records = await result.data()
                await tx.commit()
                return records

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _content_hash(self, text: str) -> str:
        """Return a stable short hash of SHA-256, length from settings."""
        length = self._settings.CONTENT_HASH_LENGTH
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]

    @staticmethod
    def _sanitize_label(label: str) -> str:
        """Sanitize a label to alphanumeric + underscore only."""
        safe = "".join(c for c in label if c.isalnum() or c == "_")
        return safe if safe else "Entity"

    @staticmethod
    def _canonicalize(name: str) -> str:
        """Canonicalize an entity name for consistent MERGE matching.

        Lowercase, strip whitespace, collapse multiple spaces, replace
        hyphens with underscores.
        """
        name = name.lower().strip()
        name = re.sub(r"\s+", " ", name)
        name = name.replace("-", "_")
        return name

    @staticmethod
    def _extract_keywords(text: str, max_keywords: int = 7) -> list[str]:
        """Extract meaningful keywords from text for graph queries.

        Uses inverse-frequency weighting (word length + rarity heuristic)
        and bigram extraction for compound terms. Filters general and
        domain-specific stopwords.
        """
        tokens = re.split(r"[^a-zA-Z0-9]+", text.lower())
        # Filter tokens
        filtered: list[str] = []
        seen: set[str] = set()
        for tok in tokens:
            if len(tok) < 3 or tok in _STOPWORDS or tok in _DOMAIN_STOPWORDS or tok in seen:
                continue
            seen.add(tok)
            filtered.append(tok)

        # Count occurrences for inverse-frequency weighting
        counts = Counter(tokens)

        # Score by specificity: longer words and rarer words score higher.
        # inverse frequency proxy: 1 / count. length bonus: len(word).
        def _specificity(word: str) -> float:
            freq = counts.get(word, 1)
            return len(word) + (1.0 / freq)

        # Build bigrams from consecutive non-stopword tokens
        bigrams: list[str] = []
        non_stop = list(dict.fromkeys(
            t for t in tokens
            if len(t) >= 3 and t not in _STOPWORDS and t not in _DOMAIN_STOPWORDS
        ))
        for i in range(len(non_stop) - 1):
            bigram = f"{non_stop[i]} {non_stop[i + 1]}"
            if bigram not in seen:
                seen.add(bigram)
                bigrams.append(bigram)

        # Combine unigrams + bigrams, score all
        candidates = filtered + bigrams

        # Bigrams get a bonus (sum of component lengths + bonus)
        def _score(candidate: str) -> float:
            if " " in candidate:
                parts = candidate.split()
                return sum(_specificity(p) for p in parts) + 2.0  # bigram bonus
            return _specificity(candidate)

        candidates.sort(key=_score, reverse=True)
        return candidates[:max_keywords]

    @staticmethod
    def _expand_keywords(keywords: list[str]) -> list[str]:
        """Expand keywords with synonyms from the synonym map.

        For each keyword, if a synonym exists in _SYNONYMS, include both
        the original and the synonym.
        """
        expanded: list[str] = []
        seen: set[str] = set()
        for kw in keywords:
            # Handle bigrams: expand each word in the bigram
            if " " in kw:
                if kw not in seen:
                    seen.add(kw)
                    expanded.append(kw)
                continue
            if kw not in seen:
                seen.add(kw)
                expanded.append(kw)
            synonym = _SYNONYMS.get(kw)
            if synonym and synonym not in seen:
                seen.add(synonym)
                expanded.append(synonym)
        return expanded

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    async def ensure_indexes(self) -> None:
        """Create Neo4j indexes if they do not already exist.

        Should be called once during application startup.
        """
        index_statements = [
            "CREATE INDEX IF NOT EXISTS FOR (n:Action) ON (n.id)",
            "CREATE INDEX IF NOT EXISTS FOR (n:Outcome) ON (n.id)",
            "CREATE INDEX IF NOT EXISTS FOR (n:Resolution) ON (n.id)",
            "CREATE INDEX IF NOT EXISTS FOR (n:Domain) ON (n.name)",
            "CREATE INDEX IF NOT EXISTS FOR (n:Concept) ON (n.name)",
            # Fulltext index across all major node types for relevance-scored search.
            (
                "CREATE FULLTEXT INDEX node_fulltext IF NOT EXISTS "
                "FOR (n:Domain|Concept|Action|Outcome|Resolution) "
                "ON EACH [n.name, n.description]"
            ),
            # Note: Corpus entities use dynamic labels (System, Product, Process, etc.)
            # so a universal property index isn't possible in Neo4j 5.x.
            # Query engine uses property-based MATCH with WHERE n.corpus = true.
        ]
        driver = self._ensure_driver()
        try:
            async with driver.session() as session:
                for stmt in index_statements:
                    await session.run(stmt)
            logger.info("Neo4j indexes ensured")
        except Exception as exc:
            raise GraphConnectionError(
                f"Failed to ensure Neo4j indexes: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def merge_action_log(
        self,
        log: ActionLog,
        namespace: str = "default",
        memory_id: str | None = None,
        *,
        workspace_id: str | None = None,
        member_id: str | None = None,
    ) -> str:
        """Persist an action log as a Domain->Action->Outcome->Resolution chain.

        Creates Concept nodes for each tag and links them to the Action.
        Creates a Namespace node and links Domain to it via CONTAINS.
        Returns the element ID of the Action node.

        When ``memory_id`` is supplied (the Qdrant point written by the same
        ``/memory/learn`` call), it is appended to each chain node's
        ``memory_ids`` list. That back-link is what lets recall verify a graph
        row against the authoritative vector lifecycle instead of guessing:
        without it an archived memory keeps resurfacing through the graph leg.
        The append is idempotent — these nodes are MERGEd by content hash, so
        the same chain is rewritten on every repeat of a known action.

        All writes are wrapped in a single explicit transaction so that
        a failure partway through does not leave partial data.
        """
        domain = self._canonicalize(log.domain)
        tags = [self._canonicalize(t) for t in log.tags]
        action_id = self._content_hash(log.action)
        outcome_id = self._content_hash(log.outcome)

        query = """
        MERGE (ns:Namespace {name: $namespace})
        MERGE (d:Domain {name: $domain})
        MERGE (ns)-[:CONTAINS]->(d)
        MERGE (a:Action {id: $action_id})
        SET a.description = $action,
            a.workspace_id = $workspace_id,
            a.member_id = $member_id
        MERGE (o:Outcome {id: $outcome_id})
        SET o.description = $outcome
        MERGE (a)-[:RELATES_TO]->(d)
        MERGE (a)-[:CAUSED]->(o)
        WITH a, o, d
        FOREACH (_ IN CASE WHEN $memory_id IS NOT NULL
                            AND NOT ($memory_id IN coalesce(a.memory_ids, []))
                       THEN [1] ELSE [] END |
            SET a.memory_ids = coalesce(a.memory_ids, []) + [$memory_id]
        )
        FOREACH (_ IN CASE WHEN $memory_id IS NOT NULL
                            AND NOT ($memory_id IN coalesce(o.memory_ids, []))
                       THEN [1] ELSE [] END |
            SET o.memory_ids = coalesce(o.memory_ids, []) + [$memory_id]
        )
        FOREACH (_ IN CASE WHEN $resolution_id IS NOT NULL THEN [1] ELSE [] END |
            MERGE (r:Resolution {id: $resolution_id})
            SET r.description = $resolution
            MERGE (o)-[:RESOLVED_BY]->(r)
            MERGE (r)-[:UTILIZES]->(d)
            FOREACH (__ IN CASE WHEN $memory_id IS NOT NULL
                                 AND NOT ($memory_id IN coalesce(r.memory_ids, []))
                            THEN [1] ELSE [] END |
                SET r.memory_ids = coalesce(r.memory_ids, []) + [$memory_id]
            )
        )
        FOREACH (tag IN $tags |
            MERGE (c:Concept {name: tag})
            MERGE (a)-[:RELATES_TO]->(c)
        )
        RETURN elementId(a) AS id
        """

        resolution_id = (
            self._content_hash(log.resolution) if log.resolution else None
        )

        driver = self._ensure_driver()
        try:
            async with driver.session() as session:
                tx = await session.begin_transaction()
                try:
                    result = await tx.run(
                        query,
                        namespace=namespace,
                        domain=domain,
                        action=log.action,
                        action_id=action_id,
                        outcome=log.outcome,
                        outcome_id=outcome_id,
                        resolution=log.resolution,
                        resolution_id=resolution_id,
                        tags=tags,
                        memory_id=memory_id,
                        workspace_id=workspace_id,
                        member_id=member_id,
                    )
                    record = await result.single()
                    if record is None:
                        raise GraphConnectionError(
                            "merge_action_log returned no result"
                        )
                    action_element_id = record["id"]
                    await tx.commit()
                except BaseException:
                    await tx.rollback()
                    raise
                return action_element_id
        except GraphConnectionError:
            raise
        except Exception as exc:
            raise GraphConnectionError(
                f"Failed to merge action log: {exc}"
            ) from exc

    async def merge_knowledge_nodes(
        self,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
    ) -> int:
        """Bulk-write knowledge nodes and edges from Sleep Cycle LLM output.

        Uses UNWIND with dynamic labels and relationship types.
        Does NOT rely on APOC procedures.

        Each node dict must have: id, label, properties.
        Each edge dict must have: source, target, type.
        Edges may optionally include source_label and target_label for
        label-aware matching (improves performance). Falls back to
        label-less match if labels are not provided.

        Entity names are canonicalized before MERGE to reduce duplicates.

        All writes are wrapped in a single explicit transaction.
        Returns total count of created/merged items.
        """
        driver = self._ensure_driver()
        count = 0

        try:
            async with driver.session() as session:
                tx = await session.begin_transaction()
                try:
                    # --- Merge nodes with dynamic labels ---
                    if nodes:
                        label_groups: dict[str, list[dict[str, Any]]] = {}
                        for node in nodes:
                            label = node.get("label", "Entity")
                            # Canonicalize name in properties if present
                            props = dict(node.get("properties", {}))
                            if "name" in props:
                                props["name"] = self._canonicalize(props["name"])
                            canonicalized_node = {
                                "id": node["id"],
                                "label": label,
                                "properties": props,
                            }
                            label_groups.setdefault(label, []).append(canonicalized_node)

                        for label, group in label_groups.items():
                            safe_label = self._sanitize_label(label)

                            merge_query = (
                                "UNWIND $nodes AS node "
                                f"MERGE (n:{safe_label} {{id: node.id}}) "
                                "SET n += node.properties "
                                "RETURN count(n) AS cnt"
                            )
                            result = await tx.run(merge_query, nodes=group)
                            record = await result.single()
                            if record:
                                count += record["cnt"]

                    # --- Merge edges with dynamic relationship types ---
                    if edges:
                        rel_groups: dict[str, list[dict[str, Any]]] = {}
                        for edge in edges:
                            rel_type = edge.get("type", "RELATED_TO")
                            rel_groups.setdefault(rel_type, []).append(edge)

                        for rel_type, group in rel_groups.items():
                            safe_type = self._sanitize_label(rel_type)
                            if safe_type == "Entity":
                                safe_type = "RELATED_TO"

                            # Check if edges in this group have label info
                            sample = group[0]
                            has_labels = "source_label" in sample and "target_label" in sample

                            if has_labels:
                                # Group edges by (source_label, target_label) for
                                # label-aware matching
                                label_pair_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
                                labelless: list[dict[str, Any]] = []
                                for edge in group:
                                    sl = edge.get("source_label")
                                    tl = edge.get("target_label")
                                    if sl and tl:
                                        safe_sl = self._sanitize_label(sl)
                                        safe_tl = self._sanitize_label(tl)
                                        label_pair_groups.setdefault(
                                            (safe_sl, safe_tl), []
                                        ).append(edge)
                                    else:
                                        labelless.append(edge)

                                for (safe_sl, safe_tl), lp_group in label_pair_groups.items():
                                    edge_query = (
                                        "UNWIND $edges AS edge "
                                        f"MATCH (src:{safe_sl} {{id: edge.source}}) "
                                        f"MATCH (tgt:{safe_tl} {{id: edge.target}}) "
                                        f"MERGE (src)-[r:{safe_type}]->(tgt) "
                                        "RETURN count(r) AS cnt"
                                    )
                                    result = await tx.run(edge_query, edges=lp_group)
                                    record = await result.single()
                                    if record:
                                        count += record["cnt"]

                                # Fall back to label-less for edges without labels
                                if labelless:
                                    edge_query = (
                                        "UNWIND $edges AS edge "
                                        "MATCH (src {id: edge.source}) "
                                        "MATCH (tgt {id: edge.target}) "
                                        f"MERGE (src)-[r:{safe_type}]->(tgt) "
                                        "RETURN count(r) AS cnt"
                                    )
                                    result = await tx.run(edge_query, edges=labelless)
                                    record = await result.single()
                                    if record:
                                        count += record["cnt"]
                            else:
                                # No label info — use original label-less match
                                edge_query = (
                                    "UNWIND $edges AS edge "
                                    "MATCH (src {id: edge.source}) "
                                    "MATCH (tgt {id: edge.target}) "
                                    f"MERGE (src)-[r:{safe_type}]->(tgt) "
                                    "RETURN count(r) AS cnt"
                                )
                                result = await tx.run(edge_query, edges=group)
                                record = await result.single()
                                if record:
                                    count += record["cnt"]

                    await tx.commit()
                except BaseException:
                    await tx.rollback()
                    raise

        except GraphConnectionError:
            raise
        except Exception as exc:
            raise GraphConnectionError(
                f"Failed to merge knowledge nodes: {exc}"
            ) from exc

        return count

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def query_related(
        self, concept: str, limit: int = 10, namespace: str = "default"
    ) -> list[dict[str, Any]]:
        """Find nodes related to a concept via graph traversal (up to 3 hops).

        Tries fulltext index search first for relevance-scored results.
        Falls back to CONTAINS-based keyword matching if the fulltext
        index is not available.

        When namespace is not "default", results are filtered to nodes
        reachable from the given Namespace node via CONTAINS relationships.
        """
        keywords = self._extract_keywords(concept)
        if not keywords:
            return []

        # Expand keywords with synonyms
        expanded = self._expand_keywords(keywords)

        # Cap results to prevent explosion
        effective_limit = min(limit, 50)

        # Try fulltext search first
        result = await self._query_related_fulltext(expanded, effective_limit, namespace)
        if result is not None:
            return result

        # Fallback to CONTAINS-based search
        return await self._query_related_contains(expanded, effective_limit, namespace)

    async def _query_related_fulltext(
        self, keywords: list[str], limit: int, namespace: str = "default"
    ) -> list[dict[str, Any]] | None:
        """Try fulltext index search. Returns None if index doesn't exist."""
        # Build Lucene query string: OR-join all keywords
        # Escape special Lucene characters in keywords
        def _escape_lucene(term: str) -> str:
            special = r'+-&|!(){}[]^"~*?:\/'
            return "".join(f"\\{c}" if c in special else c for c in term)

        parts = []
        for kw in keywords:
            escaped = _escape_lucene(kw)
            if " " in kw:
                parts.append(f'"{escaped}"')
            else:
                parts.append(escaped)
        search_terms = " OR ".join(parts)

        # Build namespace filter clause
        ns_filter = ""
        params: dict[str, Any] = {
            "search_terms": search_terms,
            "limit": limit,
        }
        if namespace != "default":
            ns_filter = (
                "AND EXISTS((result)<-[:CONTAINS*..3]-(:Namespace {name: $namespace}))"
            )
            params["namespace"] = namespace

        query = f"""
        CALL db.index.fulltext.queryNodes('node_fulltext', $search_terms)
        YIELD node, score
        WITH node AS start, score
        WHERE start:Domain OR start:Concept OR start:Action
              OR start:Outcome OR start:Resolution
        OPTIONAL MATCH (start)-[r*1..3]-(related)
        WHERE related:Domain OR related:Concept OR related:Action
              OR related:Outcome OR related:Resolution
        WITH DISTINCT
            COALESCE(related, start) AS result,
            score,
            CASE WHEN related IS NULL THEN 0 ELSE size(r) END AS distance
        WHERE true {ns_filter}
        RETURN result.name AS name,
               result.description AS description,
               labels(result)[0] AS label,
               coalesce(result.memory_ids, []) AS memory_ids,
               distance,
               score
        ORDER BY score DESC, distance ASC
        LIMIT $limit
        """

        driver = self._ensure_driver()
        try:
            async with driver.session() as session:
                result = await session.run(query, **params)
                return [dict(record) async for record in result]
        except neo4j.exceptions.ClientError:
            # Fulltext index may not exist — fall back gracefully
            logger.debug(
                "Fulltext index not available, falling back to CONTAINS search"
            )
            return None

    async def query_related_multihop(
        self,
        concept: str,
        limit: int = 10,
        namespace: str = "default",
        max_hops: int = 3,
        decay_per_hop: float = 0.5,
    ) -> list[dict[str, Any]]:
        """Find nodes related to a concept via multi-hop graph traversal with distance-weighted scoring.

        Uses fulltext search to find starting nodes, then traverses up to
        ``max_hops`` edges. Score decays exponentially per hop:
        hop 0 = 1.0x, hop 1 = decay^1, hop 2 = decay^2, etc.

        Falls back to ``query_related`` on failure.
        """
        keywords = self._extract_keywords(concept)
        if not keywords:
            return []

        expanded = self._expand_keywords(keywords)
        effective_limit = min(limit, 50)

        try:
            result = await self._query_related_multihop_fulltext(
                expanded, effective_limit, namespace, max_hops, decay_per_hop,
            )
            if result is not None:
                return result
        except Exception:
            logger.exception("Multi-hop fulltext query failed, falling back to query_related")

        # Fallback to existing single method
        return await self.query_related(concept, limit=limit, namespace=namespace)

    async def _query_related_multihop_fulltext(
        self,
        keywords: list[str],
        limit: int,
        namespace: str,
        max_hops: int,
        decay_per_hop: float,
    ) -> list[dict[str, Any]] | None:
        """Multi-hop fulltext search with distance-weighted score decay.

        Returns None if the fulltext index doesn't exist (same pattern as
        ``_query_related_fulltext``).
        """
        # Build Lucene query string
        def _escape_lucene(term: str) -> str:
            special = r'+-&|!(){}[]^"~*?:\/'
            return "".join(f"\\{c}" if c in special else c for c in term)

        parts = []
        for kw in keywords:
            escaped = _escape_lucene(kw)
            if " " in kw:
                parts.append(f'"{escaped}"')
            else:
                parts.append(escaped)
        search_terms = " OR ".join(parts)

        # Namespace filter
        ns_filter = ""
        params: dict[str, Any] = {
            "search_terms": search_terms,
            "limit": limit,
            "start_limit": 20,
            "decay": decay_per_hop,
        }
        if namespace != "default":
            ns_filter = (
                "AND EXISTS((result)<-[:CONTAINS*..3]-(:Namespace {name: $namespace}))"
            )
            params["namespace"] = namespace

        # Build CASE WHEN for distance-based score decay
        # hop 0 (direct match) = 1.0, hop N = decay^N
        case_clauses = []
        for hop in range(max_hops + 1):
            decay_value = decay_per_hop ** hop
            case_clauses.append(f"WHEN {hop} THEN {decay_value}")
        case_expr = "CASE toInteger(distance) " + " ".join(case_clauses) + f" ELSE {decay_per_hop ** max_hops} END"

        query = f"""
        CALL db.index.fulltext.queryNodes('node_fulltext', $search_terms)
        YIELD node, score
        WITH node AS start, score
        WHERE start:Domain OR start:Concept OR start:Action
              OR start:Outcome OR start:Resolution
        WITH start, score
        ORDER BY score DESC
        LIMIT $start_limit
        OPTIONAL MATCH (start)-[r*1..{max_hops}]-(related)
        WHERE related:Domain OR related:Concept OR related:Action
              OR related:Outcome OR related:Resolution
        WITH DISTINCT
            COALESCE(related, start) AS result,
            score,
            CASE WHEN related IS NULL THEN 0 ELSE size(r) END AS distance
        WHERE true {ns_filter}
        WITH result, score, distance,
             score * {case_expr} AS weighted_score
        RETURN result.name AS name,
               result.description AS description,
               labels(result)[0] AS label,
               coalesce(result.memory_ids, []) AS memory_ids,
               distance,
               weighted_score AS score
        ORDER BY weighted_score DESC, distance ASC
        LIMIT $limit
        """

        driver = self._ensure_driver()
        try:
            async with driver.session() as session:
                result = await session.run(query, **params)
                return [dict(record) async for record in result]
        except neo4j.exceptions.ClientError:
            logger.debug(
                "Fulltext index not available for multi-hop, falling back"
            )
            return None

    async def _query_related_contains(
        self, keywords: list[str], limit: int, namespace: str = "default"
    ) -> list[dict[str, Any]]:
        """Fallback CONTAINS-based keyword search with label filtering."""
        # Build OR-chained CONTAINS conditions for each keyword.
        conditions: list[str] = []
        params: dict[str, Any] = {"limit": limit}
        for i, kw in enumerate(keywords):
            pname = f"kw{i}"
            params[pname] = kw
            conditions.append(
                f"(toLower(start.name) CONTAINS ${pname} "
                f"OR toLower(start.description) CONTAINS ${pname})"
            )
        where_clause = " OR ".join(conditions)

        # Build namespace filter clause
        ns_filter_start = ""
        ns_filter_related = ""
        if namespace != "default":
            ns_filter_start = (
                "AND EXISTS((start)<-[:CONTAINS*..3]-(:Namespace {name: $namespace}))"
            )
            ns_filter_related = (
                "AND EXISTS((related)<-[:CONTAINS*..3]-(:Namespace {name: $namespace}))"
            )
            params["namespace"] = namespace

        # Label-bounded traversal
        query = f"""
        MATCH (start)-[r*1..3]-(related)
        WHERE (start:Domain OR start:Concept OR start:Action
               OR start:Outcome OR start:Resolution)
          AND ({where_clause})
          AND (related:Domain OR related:Concept OR related:Action
               OR related:Outcome OR related:Resolution)
          {ns_filter_start}
          {ns_filter_related}
        RETURN DISTINCT related.name AS name,
               related.description AS description,
               labels(related)[0] AS label,
               coalesce(related.memory_ids, []) AS memory_ids,
               size(r) AS distance
        ORDER BY distance ASC
        LIMIT $limit
        """
        driver = self._ensure_driver()
        try:
            async with driver.session() as session:
                result = await session.run(query, **params)
                return [dict(record) async for record in result]
        except GraphConnectionError:
            raise
        except Exception as exc:
            raise GraphConnectionError(
                f"Failed to query related concepts: {exc}"
            ) from exc

    async def get_graph_snapshot(self, concept: str | None = None, limit: int = 50) -> dict:
        """Return nodes and edges for visualization.

        If concept is provided, get subgraph around it (2 hops).
        Otherwise get a sample of nodes with their edges.
        Returns {"nodes": [...], "edges": [...]}.
        """
        driver = self._ensure_driver()
        effective_limit = min(limit, 200)

        try:
            async with driver.session() as session:
                if concept:
                    canon = self._canonicalize(concept)
                    query = """
                    MATCH (center)
                    WHERE (center:Domain OR center:Concept OR center:Action
                           OR center:Outcome OR center:Resolution)
                      AND toLower(center.name) CONTAINS $concept
                    WITH center LIMIT 1
                    OPTIONAL MATCH path = (center)-[*1..2]-(neighbor)
                    WHERE neighbor:Domain OR neighbor:Concept OR neighbor:Action
                          OR neighbor:Outcome OR neighbor:Resolution
                    WITH collect(DISTINCT center) + collect(DISTINCT neighbor) AS allNodes,
                         collect(DISTINCT path) AS paths
                    UNWIND allNodes AS n
                    WITH collect(DISTINCT n) AS nodes, paths
                    UNWIND paths AS p
                    UNWIND relationships(p) AS rel
                    WITH nodes,
                         collect(DISTINCT {
                             source: elementId(startNode(rel)),
                             target: elementId(endNode(rel)),
                             type: type(rel)
                         }) AS edges
                    UNWIND nodes AS n
                    RETURN collect(DISTINCT {
                        id: elementId(n),
                        label: labels(n)[0],
                        name: COALESCE(n.name, n.description, n.id, 'unnamed')
                    }) AS nodes, edges
                    """
                    result = await session.run(query, concept=canon)
                else:
                    query = """
                    MATCH (n)
                    WHERE n:Domain OR n:Concept OR n:Action
                          OR n:Outcome OR n:Resolution
                    WITH n LIMIT $limit
                    OPTIONAL MATCH (n)-[r]-(m)
                    WHERE m:Domain OR m:Concept OR m:Action
                          OR m:Outcome OR m:Resolution
                    WITH collect(DISTINCT {
                        id: elementId(n),
                        label: labels(n)[0],
                        name: COALESCE(n.name, n.description, n.id, 'unnamed')
                    }) + collect(DISTINCT {
                        id: elementId(m),
                        label: labels(m)[0],
                        name: COALESCE(m.name, m.description, m.id, 'unnamed')
                    }) AS allNodes,
                    collect(DISTINCT {
                        source: elementId(startNode(r)),
                        target: elementId(endNode(r)),
                        type: type(r)
                    }) AS edges
                    UNWIND allNodes AS node
                    RETURN collect(DISTINCT node) AS nodes, edges
                    """
                    result = await session.run(query, limit=effective_limit)

                record = await result.single()
                if record is None:
                    return {"nodes": [], "edges": []}

                nodes = [dict(n) for n in record["nodes"]] if record["nodes"] else []
                edges = [dict(e) for e in record["edges"]] if record["edges"] else []
                # Filter out null-source/target edges
                edges = [e for e in edges if e.get("source") and e.get("target")]
                return {"nodes": nodes, "edges": edges}
        except Exception as exc:
            logger.error("Failed to get graph snapshot: %s", exc)
            return {"nodes": [], "edges": []}

    async def get_node_edge_counts(self) -> dict:
        """Return {"nodes": int, "edges": int}."""
        driver = self._ensure_driver()
        try:
            async with driver.session() as session:
                node_result = await session.run(
                    "MATCH (n) WHERE n:Domain OR n:Concept OR n:Action "
                    "OR n:Outcome OR n:Resolution RETURN count(n) AS cnt"
                )
                node_record = await node_result.single()
                node_count = node_record["cnt"] if node_record else 0

                edge_result = await session.run(
                    "MATCH ()-[r]->() RETURN count(r) AS cnt"
                )
                edge_record = await edge_result.single()
                edge_count = edge_record["cnt"] if edge_record else 0

                return {"nodes": node_count, "edges": edge_count}
        except Exception as exc:
            logger.error("Failed to get node/edge counts: %s", exc)
            return {"nodes": 0, "edges": 0}

    async def get_stats(self) -> dict:
        """Return graph statistics: node_count, edge_count, domains, top_tags."""
        driver = self._ensure_driver()
        try:
            async with driver.session() as session:
                node_result = await session.run(
                    "MATCH (n) WHERE n:Domain OR n:Concept OR n:Action "
                    "OR n:Outcome OR n:Resolution RETURN count(n) AS cnt"
                )
                node_record = await node_result.single()
                node_count = node_record["cnt"] if node_record else 0

                edge_result = await session.run(
                    "MATCH ()-[r]->() RETURN count(r) AS cnt"
                )
                edge_record = await edge_result.single()
                edge_count = edge_record["cnt"] if edge_record else 0

                domain_result = await session.run(
                    "MATCH (d:Domain) RETURN d.name AS domain"
                )
                domains = [record["domain"] async for record in domain_result if record["domain"]]

                tag_result = await session.run(
                    "MATCH (c:Concept)<-[:RELATES_TO]-() "
                    "RETURN c.name AS tag, count(*) AS cnt "
                    "ORDER BY cnt DESC LIMIT 20"
                )
                top_tags = [
                    {"tag": record["tag"], "count": record["cnt"]}
                    async for record in tag_result
                    if record["tag"]
                ]

                return {
                    "node_count": node_count,
                    "edge_count": edge_count,
                    "domains": domains,
                    "top_tags": top_tags,
                }
        except Exception as exc:
            logger.error("Failed to get graph stats: %s", exc)
            return {"node_count": 0, "edge_count": 0, "domains": [], "top_tags": []}

    async def export_graph(self) -> dict:
        """Export all nodes and edges.

        Returns {"nodes": [...], "edges": [...]}.
        """
        driver = self._ensure_driver()
        try:
            async with driver.session() as session:
                node_result = await session.run(
                    "MATCH (n) WHERE n:Domain OR n:Concept OR n:Action "
                    "OR n:Outcome OR n:Resolution "
                    "RETURN elementId(n) AS id, labels(n)[0] AS label, "
                    "properties(n) AS properties"
                )
                nodes = [
                    {
                        "id": record["id"],
                        "label": record["label"],
                        "properties": dict(record["properties"]) if record["properties"] else {},
                    }
                    async for record in node_result
                ]

                edge_result = await session.run(
                    "MATCH (n)-[r]->(m) "
                    "WHERE (n:Domain OR n:Concept OR n:Action OR n:Outcome OR n:Resolution) "
                    "AND (m:Domain OR m:Concept OR m:Action OR m:Outcome OR m:Resolution) "
                    "RETURN elementId(n) AS source, elementId(m) AS target, type(r) AS type"
                )
                edges = [
                    {
                        "source": record["source"],
                        "target": record["target"],
                        "type": record["type"],
                    }
                    async for record in edge_result
                ]

                return {"nodes": nodes, "edges": edges}
        except Exception as exc:
            logger.error("Failed to export graph: %s", exc)
            return {"nodes": [], "edges": []}

    async def create_supersession(self, newer_id: str, older_id: str, reason: str, detected: str = "auto") -> None:
        """Create a SUPERSEDES edge between two nodes.

        newer_id is a Neo4j element ID. older_id can be either a Neo4j
        element ID or a Qdrant vector UUID (matched via MemoryRef nodes).
        """
        driver = self._ensure_driver()
        async with driver.session() as session:
            await session.run(
                "MATCH (newer) WHERE elementId(newer) = $newer_id "
                "OPTIONAL MATCH (older_by_eid) WHERE elementId(older_by_eid) = $older_id "
                "OPTIONAL MATCH (older_by_ref:MemoryRef {vector_id: $older_id}) "
                "WITH newer, COALESCE(older_by_eid, older_by_ref) AS older "
                "WHERE older IS NOT NULL "
                "MERGE (newer)-[:SUPERSEDES {reason: $reason, detected: $detected, timestamp: datetime()}]->(older)",
                newer_id=newer_id, older_id=older_id, reason=reason, detected=detected,
            )

    async def get_supersession_history(self, node_id: str) -> dict:
        """Get the supersession chain for a node — what it supersedes and what supersedes it."""
        driver = self._ensure_driver()
        async with driver.session() as session:
            # What this node supersedes (outgoing SUPERSEDES edges)
            result = await session.run(
                "MATCH (n)-[r:SUPERSEDES]->(older) WHERE elementId(n) = $node_id "
                "RETURN elementId(older) AS id, older.action AS text, r.reason AS reason, r.detected AS detected",
                node_id=node_id,
            )
            supersedes = [dict(record) async for record in result]

            # What supersedes this node (incoming SUPERSEDES edges)
            result2 = await session.run(
                "MATCH (newer)-[r:SUPERSEDES]->(n) WHERE elementId(n) = $node_id "
                "RETURN elementId(newer) AS id, newer.action AS text, r.reason AS reason, r.detected AS detected",
                node_id=node_id,
            )
            superseded_by_list = [dict(record) async for record in result2]

            return {
                "supersedes": supersedes,
                "superseded_by": superseded_by_list[0] if superseded_by_list else None,
            }

    async def get_backlinks(self, vector_id: str) -> list[dict]:
        """Get all backlinked memories for a given vector ID."""
        driver = self._ensure_driver()
        async with driver.session() as session:
            result = await session.run(
                "MATCH (ref:MemoryRef {vector_id: $vector_id})-[:BACKLINK]->(target) "
                "RETURN elementId(target) AS graph_id, "
                "       target.description AS text, "
                "       labels(target)[0] AS label "
                "UNION "
                "MATCH (source)-[:BACKLINK]->(ref:MemoryRef {vector_id: $vector_id}) "
                "RETURN elementId(source) AS graph_id, "
                "       source.description AS text, "
                "       labels(source)[0] AS label",
                vector_id=vector_id,
            )
            return [dict(record) async for record in result]

    # ------------------------------------------------------------------
    # User profile operations
    # ------------------------------------------------------------------

    async def ensure_person_node(self, name: str = "default") -> str:
        """MERGE a Person node and return its elementId."""
        driver = self._ensure_driver()
        canon = self._canonicalize(name)
        try:
            async with driver.session() as session:
                result = await session.run(
                    "MERGE (p:Person {name: $name}) "
                    "ON CREATE SET p.created_at = datetime() "
                    "SET p.updated_at = datetime() "
                    "RETURN elementId(p) AS id",
                    name=canon,
                )
                record = await result.single()
                if record is None:
                    raise GraphConnectionError("ensure_person_node returned no result")
                return record["id"]
        except GraphConnectionError:
            raise
        except Exception as exc:
            raise GraphConnectionError(
                f"Failed to ensure person node: {exc}"
            ) from exc

    async def upsert_skill(
        self, person: str, skill_name: str, level: str, evidence_count: int
    ) -> None:
        """MERGE a Skill node and HAS_EXPERTISE edge from Person."""
        driver = self._ensure_driver()
        canon_person = self._canonicalize(person)
        canon_skill = self._canonicalize(skill_name)
        try:
            async with driver.session() as session:
                await session.run(
                    "MERGE (p:Person {name: $person}) "
                    "MERGE (s:Skill {name: $skill}) "
                    "MERGE (p)-[r:HAS_EXPERTISE]->(s) "
                    "SET s.level = $level, "
                    "    s.evidence_count = $evidence_count, "
                    "    s.updated_at = datetime() "
                    "ON CREATE SET s.created_at = datetime()",
                    person=canon_person,
                    skill=canon_skill,
                    level=level,
                    evidence_count=evidence_count,
                )
        except Exception as exc:
            raise GraphConnectionError(
                f"Failed to upsert skill: {exc}"
            ) from exc

    async def upsert_preference(
        self, person: str, domain: str, description: str, evidence_count: int
    ) -> None:
        """MERGE a Preference node and PREFERS edge from Person."""
        driver = self._ensure_driver()
        canon_person = self._canonicalize(person)
        canon_domain = self._canonicalize(domain)
        try:
            async with driver.session() as session:
                await session.run(
                    "MERGE (p:Person {name: $person}) "
                    "MERGE (pref:Preference {domain: $domain}) "
                    "MERGE (p)-[r:PREFERS]->(pref) "
                    "SET pref.description = $description, "
                    "    pref.evidence_count = $evidence_count, "
                    "    pref.updated_at = datetime() "
                    "ON CREATE SET pref.created_at = datetime()",
                    person=canon_person,
                    domain=canon_domain,
                    description=description,
                    evidence_count=evidence_count,
                )
        except Exception as exc:
            raise GraphConnectionError(
                f"Failed to upsert preference: {exc}"
            ) from exc

    async def upsert_goal(
        self, person: str, description: str, status: str = "active"
    ) -> None:
        """MERGE a Goal node and WORKING_ON edge from Person."""
        driver = self._ensure_driver()
        canon_person = self._canonicalize(person)
        try:
            async with driver.session() as session:
                await session.run(
                    "MERGE (p:Person {name: $person}) "
                    "MERGE (g:Goal {description: $description}) "
                    "MERGE (p)-[r:WORKING_ON]->(g) "
                    "SET g.status = $status, "
                    "    g.updated_at = datetime() "
                    "ON CREATE SET g.created_at = datetime()",
                    person=canon_person,
                    description=description,
                    status=status,
                )
        except Exception as exc:
            raise GraphConnectionError(
                f"Failed to upsert goal: {exc}"
            ) from exc

    async def get_domains(self) -> list[str]:
        """Return a list of all domain names in the graph."""
        driver = self._ensure_driver()
        try:
            async with driver.session() as session:
                result = await session.run(
                    "MATCH (d:Domain) RETURN d.name AS name ORDER BY name"
                )
                return [record["name"] async for record in result if record["name"]]
        except Exception as exc:
            logger.error("Failed to get domains: %s", exc)
            return []

    async def query_resolutions(
        self, error_pattern: str, limit: int = 5, namespace: str = "default"
    ) -> list[dict[str, Any]]:
        """Find resolutions for outcomes matching the given error pattern."""
        params: dict[str, Any] = {
            "error_pattern": error_pattern,
            "limit": limit,
        }

        ns_filter = ""
        if namespace != "default":
            ns_filter = (
                "AND EXISTS((r)<-[:CONTAINS*..3]-(:Namespace {name: $namespace}))"
            )
            params["namespace"] = namespace

        query = f"""
        MATCH (o:Outcome)-[:RESOLVED_BY]->(r:Resolution)
        WHERE toLower(o.description) CONTAINS toLower($error_pattern)
        {ns_filter}
        RETURN r.description AS resolution,
               o.description AS error,
               elementId(r) AS id,
               coalesce(r.memory_ids, []) + coalesce(o.memory_ids, []) AS memory_ids
        LIMIT $limit
        """
        driver = self._ensure_driver()
        try:
            async with driver.session() as session:
                result = await session.run(query, **params)
                return [dict(record) async for record in result]
        except GraphConnectionError:
            raise
        except Exception as exc:
            raise GraphConnectionError(
                f"Failed to query resolutions: {exc}"
            ) from exc
