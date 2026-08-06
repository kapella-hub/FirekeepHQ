"""Shared knowledge-ingestion core (SP3): the corpus-sync + queue-classify body
used by both POST /knowledge/ingest and the scheduled collectors."""
from __future__ import annotations

from app.knowledge.status import set_ingest_status
from app.workers.skill_synthesis import classify_and_draft_from_doc
from corpus.pipeline import ingest_document as corpus_ingest_document


async def ingest_knowledge_document(
    content: str, source_name: str, source_type: str, *,
    vector, redis, project: str | None = None, namespace: str = "default",
    workspace_id: str | None = None, member_id: str | None = None,
) -> None:
    """Corpus-ingest synchronously (doc searchable now), mark status 'queued',
    enqueue the async classify+draft task. RAISES on corpus-ingest failure
    (caller maps it: REST → 500, collector → per-page error). Ordering
    invariant: corpus first, then status, then enqueue.

    ``workspace_id``/``member_id`` are the ingesting principal's tenancy. They
    reach BOTH halves: the corpus chunks written here, and — via the Celery
    kwargs — the draft skills the classifier fans out. Both are searched
    through ``memory_recall``'s workspace filter, so a null on either half is
    content that exists and cannot be found. The collector path has no HTTP
    principal and passes the deployment workspace explicitly at its call site.
    """
    await corpus_ingest_document(
        content=content, source_name=source_name, source_type=source_type,
        vector_client=vector, redis_client=redis,
        workspace_id=workspace_id, member_id=member_id,
    )
    await set_ingest_status(source_name, "queued", redis_client=redis)
    classify_and_draft_from_doc.delay(
        source_name, content, source_type, project=project, namespace=namespace,
        workspace_id=workspace_id, member_id=member_id,
    )
