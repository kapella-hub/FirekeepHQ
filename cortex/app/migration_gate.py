"""MIGRATION_FREEZE write gate (identity-v2 D6).

During the freeze-copy-cutover migration window
(docs/superpowers/specs/2026-08-27-memory-identity-v2-design.md D6 step 1)
every write path that could add or mutate a Qdrant/Neo4j record must refuse
rather than race the migration's shadow copy and verify passes.
``MIGRATION_FREEZE=true`` flips every gated route to 503 with a retry hint;
read/recall routes are unaffected -- the design keeps them serving until the
collection flip (D6 step 4).

A single FastAPI dependency, attached via
``dependencies=[Depends(require_not_frozen)]`` on every gated route, mirrors
``auth.middleware.require_scope``'s shape: a small callable other routers
import rather than re-implementing the check. ``settings`` is itself a
sub-dependency (not a bare ``get_settings()`` call) so tests can flip the
flag with ``app.dependency_overrides[get_settings]`` instead of mutating the
process-wide ``lru_cache`` singleton.

Corpus's write routes (``POST /corpus/ingest``, ``DELETE /corpus/sources/{name}``,
``DELETE /corpus/dex-sources/{id}``) are gated separately (``corpus/api.py``'s
``is_migration_frozen`` module hook): ``corpus/`` is a shared module used
outside Cortex and takes no dependency on ``app.config``, the same reason
its ``ingest_document``/``get_corpus_sources``/``delete_corpus_source``
hooks are plain module-level callables rather than FastAPI dependencies.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException

from app.config import Settings, get_settings

MIGRATION_FREEZE_DETAIL = "memory store migration in progress; retry shortly"


def require_not_frozen(settings: Settings = Depends(get_settings)) -> None:
    """FastAPI dependency: 503s the request while MIGRATION_FREEZE is set."""
    if settings.MIGRATION_FREEZE:
        raise HTTPException(status_code=503, detail=MIGRATION_FREEZE_DETAIL)
