"""Celery tasks for skill synthesis — triggered + periodic catch-all."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import redis.asyncio

from app.config import get_settings
from app.db.vector import VectorClient
from app.knowledge.classifier import classify_document
from app.knowledge.status import set_ingest_status
from app.skills.reconcile import reconcile_source_skills
from app.skills.scorer import compute_skill_score
from app.skills.synthesizer import SkillSynthesizer
from app.workers.sleep_cycle import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.skill_synthesis.synthesize_skill_for_session")
def synthesize_skill_for_session(session_id: str, skill_worthy: bool = False) -> dict[str, Any]:
    """Celery task: score session and synthesize skill if triggered."""
    try:
        return asyncio.run(_run_synthesis(session_id, skill_worthy))
    except Exception:
        logger.exception("Unhandled error in synthesize_skill_for_session(%s)", session_id)
        return {"status": "error", "session_id": session_id}


@celery_app.task(name="app.workers.skill_synthesis.draft_skill_from_doc")
def draft_skill_from_doc(
    source_name: str,
    procedure_title: str,
    doc_content: str,
    project: str | None = None,
    namespace: str = "default",
) -> dict[str, Any]:
    """Celery task: draft a skill from a single procedure extracted out of an ingested document."""
    try:
        return asyncio.run(
            _run_doc_synthesis(source_name, procedure_title, doc_content, project, namespace)
        )
    except Exception as e:
        logger.exception(
            "Unhandled error in draft_skill_from_doc(%s :: %s)", source_name, procedure_title
        )
        return {
            "status": "draft_failed",
            "source_doc": source_name,
            "procedure_title": procedure_title,
            "error": str(e),
        }


@celery_app.task(
    name="app.workers.skill_synthesis.classify_and_draft_from_doc",
    acks_late=True,
    reject_on_worker_lost=True,
)
def classify_and_draft_from_doc(
    source_name: str,
    content: str,
    source_type: str,
    project: str | None = None,
    namespace: str = "default",
) -> dict[str, Any]:
    """Celery task: classify an ingested doc, then fan out per-procedure draft tasks.

    acks_late + reject_on_worker_lost so a worker crash redelivers instead of
    stranding the source at 'classifying'. Never raises; holds no redis client
    (all redis work is inside _run_classify_and_draft's own client)."""
    try:
        return asyncio.run(
            _run_classify_and_draft(source_name, content, source_type, project, namespace)
        )
    except Exception as e:
        logger.exception("Unhandled error in classify_and_draft_from_doc(%s)", source_name)
        return {"status": "error", "source_doc": source_name, "error": str(e)}


@celery_app.task(name="app.workers.skill_synthesis.run_url_ingest")
def run_url_ingest(url: str, depth: int, max_pages: int) -> dict[str, Any]:
    """Celery task: crawl a URL (bounded depth/pages, SSRF-guarded) and ingest
    each fetched page through the knowledge pipeline. Never raises out of the
    task; per-page ingest failures are logged and skipped rather than aborting
    the rest of the crawl."""
    try:
        return asyncio.run(_run_url_ingest_impl(url, depth, max_pages))
    except Exception as e:
        logger.exception("Unhandled error in run_url_ingest(%s)", url)
        return {"status": "error", "error": str(e)}


@celery_app.task(name="app.workers.skill_synthesis.skill_synthesis_pass")
def skill_synthesis_pass() -> dict[str, Any]:
    """Memory Agent Pass 9 catch-all: score sessions from last 24h not yet evaluated."""
    settings = get_settings()
    if not settings.SKILL_SYNTHESIS_ENABLED:
        return {"status": "disabled"}
    try:
        return asyncio.run(_run_pass())
    except Exception:
        logger.exception("Unhandled error in skill_synthesis_pass")
        return {"status": "error", "synthesized": 0}


async def _run_synthesis(session_id: str, skill_worthy: bool) -> dict[str, Any]:
    """Core synthesis logic — score session, synthesize skill if triggered."""
    settings = get_settings()
    try:
        score = await compute_skill_score(session_id, skill_worthy=skill_worthy)
        if not score.triggered:
            logger.info("Skill synthesis skipped for session %s (score %.3f)", session_id, score.total)
            return {"status": "skipped", "session_id": session_id, "score": score.total}

        synth = SkillSynthesizer(settings)
        result = await synth.synthesize(session_id, score)
        logger.info(
            "Skill synthesized for session %s: %s (score %.3f)",
            session_id, result.get("trigger", ""), score.total,
        )
        return result
    except Exception:
        logger.exception("Skill synthesis failed for session %s", session_id)
        return {"status": "error", "session_id": session_id}


async def _run_doc_synthesis(
    source_name: str,
    procedure_title: str,
    doc_content: str,
    project: str | None,
    namespace: str,
) -> dict[str, Any]:
    """Core doc-drafting logic — build synthesizer, run synthesize_from_document."""
    settings = get_settings()
    try:
        synth = SkillSynthesizer(settings)
        result = await synth.synthesize_from_document(
            source_name=source_name,
            procedure_title=procedure_title,
            doc_content=doc_content,
            project=project,
            namespace=namespace,
        )
        logger.info(
            "Skill drafted from document %s :: %s: %s",
            source_name, procedure_title, result.get("status", ""),
        )
        return result
    except Exception as e:
        logger.exception("Doc skill drafting failed for %s :: %s", source_name, procedure_title)
        return {
            "status": "draft_failed",
            "source_doc": source_name,
            "procedure_title": procedure_title,
            "error": str(e),
        }


async def _run_classify_and_draft(
    source_name: str,
    content: str,
    source_type: str,
    project: str | None,
    namespace: str,
) -> dict[str, Any]:
    """Classify then enqueue per-title drafts, recording ingest status. Owns its
    own redis client (try/finally aclose), mirroring _run_pass."""
    settings = get_settings()
    r = redis.asyncio.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await set_ingest_status(source_name, "classifying", redis_client=r)
        classification = await classify_document(content, settings=settings)

        if not classification.get("ok", False):
            # Generation backend absent (embed-only deploy) → clean 'corpus_only'
            # (doc searchable, classification deferred), not an alarming 'failed'.
            # Auto-reverts to normal classification once a generation model is up.
            if classification.get("unavailable"):
                await set_ingest_status(
                    source_name, "corpus_only", disposition="reference", skills_queued=0,
                    note=classification.get("note", ""), redis_client=r,
                )
                return {"status": "corpus_only", "source_doc": source_name}
            await set_ingest_status(
                source_name, "failed", disposition="reference", skills_queued=0,
                note=classification.get("note", ""), redis_client=r,
            )
            return {"status": "classify_failed", "source_doc": source_name}

        titles = classification.get("procedure_titles") or []
        for title in titles:
            draft_skill_from_doc.delay(
                source_name, title, content, project=project, namespace=namespace,
            )
        await set_ingest_status(
            source_name, "classified",
            disposition=classification.get("primary_type", "reference"),
            skills_queued=len(titles), note=classification.get("note", ""),
            redis_client=r,
        )
        # Safe draft-skill reconciliation — own guarded block: a Qdrant hiccup
        # must NOT flip the (already-successful) 'classified' status to failed.
        vector = VectorClient(settings)
        try:
            await reconcile_source_skills(source_name, set(titles), vector)
        except Exception:
            logger.exception("Skill reconciliation failed for %s (ingest unaffected)", source_name)
        finally:
            try:
                await vector.close()
            except Exception:
                logger.debug("vector close failed during reconcile (ingest unaffected)")
        return {"status": "classified", "source_doc": source_name, "skills_queued": len(titles)}
    except Exception:
        logger.exception("classify_and_draft failed for %s", source_name)
        try:
            await set_ingest_status(
                source_name, "failed", disposition="reference", skills_queued=0,
                note="classify/draft task error", redis_client=r,
            )
        except Exception:
            logger.exception("terminal failed-status write also failed for %s", source_name)
        return {"status": "error", "source_doc": source_name}
    finally:
        await r.aclose()


async def _run_url_ingest_impl(url: str, depth: int, max_pages: int) -> dict[str, Any]:
    """Crawl `url` (SSRF-guarded, bounded to `depth`/`max_pages`) and ingest each
    fetched page through the knowledge pipeline. Owns its own redis + vector
    clients (mirrors _run_classify_and_draft). A single bad page is logged and
    skipped rather than aborting the rest of the crawl."""
    from urllib.parse import urlparse

    from app.knowledge.crawler import crawl
    from app.knowledge.ingest_core import ingest_knowledge_document

    settings = get_settings()
    r = redis.asyncio.from_url(settings.REDIS_URL, decode_responses=True)
    vector = VectorClient(settings)
    try:
        pages = await crawl(
            url,
            depth=depth,
            max_pages=max_pages,
            timeout=settings.KNOWLEDGE_CRAWL_TIMEOUT_SECONDS,
            max_bytes=settings.KNOWLEDGE_CRAWL_MAX_PAGE_BYTES,
        )

        ingested = 0
        for page in pages:
            try:
                hostname = urlparse(page.url).hostname
                label = (page.title or page.url)[:120]
                source_name = f"Web:{hostname}:{label}"
                await ingest_knowledge_document(
                    page.markdown, source_name, "web", vector=vector, redis=r,
                )
                ingested += 1
            except Exception:
                logger.exception(
                    "run_url_ingest: failed to ingest page %s (continuing)", page.url
                )

        return {
            "status": "done",
            "url": url,
            "pages_ingested": ingested,
            "pages_fetched": len(pages),
        }
    finally:
        try:
            await vector.close()
        except Exception:
            logger.debug("vector close failed during run_url_ingest (ingest unaffected)")
        await r.aclose()


async def _run_pass() -> dict[str, Any]:
    """Scan Bridge for sessions completed in last 24h not yet evaluated."""
    import httpx
    import redis.asyncio

    settings = get_settings()
    synthesized = 0

    r = redis.asyncio.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.BRIDGE_URL}/sessions",
                params={"status": "completed", "limit": 100},
            )
            if resp.status_code != 200:
                return {"status": "bridge_unavailable", "synthesized": 0}
            sessions = resp.json().get("sessions", [])

        for session in sessions:
            sid = session.get("session_id", "")
            if not sid:
                continue
            already = await r.get(f"nc:skill:evaluated:{sid}")
            if already:
                continue
            result = await _run_synthesis(sid, skill_worthy=False)
            if result.get("status") == "ok":
                synthesized += 1
    except Exception:
        logger.exception("skill_synthesis_pass failed")
    finally:
        await r.aclose()

    return {"status": "completed", "synthesized": synthesized}
