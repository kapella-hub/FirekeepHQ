"""Pattern Engine REST API — FastAPI router mounted on Cortex."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from auth.middleware import require_scope

from app.config import get_settings
from app.patterns.analyzer import analyze_patterns
from app.patterns.models import PatternCard
from app.patterns.store import (
    get_patterns, get_relevant_patterns, store_patterns,
    record_tip_shown, compute_tip_effectiveness, promote_all_patterns,
)
from app.skills import internal_key_headers  # Cortex-internal helper (same package)

logger = logging.getLogger(__name__)


async def _build_briefing_map(http_client, bridge_url: str, internal_key: str | None) -> dict[str, str]:
    """Map Bridge-stored briefing_id -> session_id (SP1b §11 reconciliation).

    Bridge caps list_sessions at 200; request the cap (dev-scale is well under
    it). Sessions without a briefing_id are skipped. Best-effort: any Bridge
    error -> {} so effectiveness degrades to the session_id-keyed join rather
    than 500ing.
    """
    try:
        resp = await http_client.get(
            f"{bridge_url}/sessions",
            headers=internal_key_headers(internal_key),
            params={"limit": 200},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.warning("briefing_id map fetch from Bridge failed; effectiveness runs without reconciliation")
        return {}
    mapping: dict[str, str] = {}
    for sess in data.get("sessions", []):
        bid = sess.get("briefing_id")
        sid = sess.get("session_id")
        if bid and sid:
            mapping[bid] = sid
    return mapping


async def _effectiveness_with_reconciliation(r, http_client, settings) -> list[dict[str, Any]]:
    """Build the briefing_id->session_id map from Bridge, then compute effectiveness."""
    briefing_map = await _build_briefing_map(
        http_client, settings.BRIDGE_URL, settings.FIREKEEP_INTERNAL_KEY,
    )
    return await compute_tip_effectiveness(r, briefing_to_session=briefing_map)


def create_patterns_router(get_replay_redis) -> APIRouter:
    """Create the patterns API router.

    Args:
        get_replay_redis: FastAPI dependency returning async Redis for DB 6.
    """
    router = APIRouter(prefix="/patterns", tags=["patterns"])

    @router.get("/")
    async def list_patterns(
        r=Depends(get_replay_redis),
        identity: dict = Depends(require_scope("eval:read")),
        limit: int = Query(default=50, ge=1, le=200),
        stage: str = Query(default="", description="Filter by stage (candidate, observed, trial, validated, stale, quarantined)"),
        category: str = Query(default="", description="Filter by category (procedural, risk, behavioral)"),
    ) -> dict[str, Any]:
        """List all discovered patterns, sorted by confidence."""
        patterns = await get_patterns(r, limit=limit)
        if stage:
            patterns = [p for p in patterns if p.stage == stage]
        if category:
            patterns = [p for p in patterns if p.category == category]
        return {
            "patterns": [p.model_dump(mode="json") for p in patterns],
            "count": len(patterns),
        }

    @router.get("/relevant")
    async def relevant_patterns(
        r=Depends(get_replay_redis),
        identity: dict = Depends(require_scope("eval:read")),
        goal: str = Query(default="", description="Current task goal"),
        files: str = Query(default="", description="Comma-separated file paths"),
        limit: int = Query(default=5, ge=1, le=50),
        exclude_agent: str = Query(default="", description="Exclude patterns from this agent (for cross-agent learning)"),
    ) -> dict[str, Any]:
        """Get patterns relevant to a goal and/or file list.

        When `exclude_agent` is set, only returns patterns discovered from
        other agents' sessions — enabling cross-agent learning.
        """
        file_list = [f.strip() for f in files.split(",") if f.strip()] if files else []
        patterns = await get_relevant_patterns(
            r, goal=goal, files=file_list, limit=limit, exclude_agent=exclude_agent,
        )
        return {
            "patterns": [p.model_dump(mode="json") for p in patterns],
            "count": len(patterns),
            "goal": goal,
            "files": file_list,
            "exclude_agent": exclude_agent or None,
        }

    @router.post("/analyze")
    async def trigger_analysis(
        r=Depends(get_replay_redis),
        identity: dict = Depends(require_scope("admin")),
        min_sessions: int = Query(default=5, ge=1, le=100),
    ) -> dict[str, Any]:
        """Manually trigger pattern analysis."""
        patterns = await analyze_patterns(r, min_sessions=min_sessions)
        if patterns:
            stored = await store_patterns(r, patterns)
            # Run lifecycle promotion after storing new patterns
            await promote_all_patterns(r)
            return {
                "patterns": [p.model_dump(mode="json") for p in patterns],
                "count": len(patterns),
                "stored": stored,
            }
        return {"patterns": [], "count": 0, "stored": 0, "message": "Not enough sessions or no patterns found"}

    @router.post("/tip-shown")
    async def tip_shown(
        r=Depends(get_replay_redis),
        identity: dict = Depends(require_scope("eval:read")),
        session_id: str = Query(..., description="Session that received the tips"),
        pattern_ids: str = Query(..., description="Comma-separated pattern IDs shown"),
        group: str = Query(default="treatment", description="A/B test group: 'treatment' (tips shown) or 'control' (tips withheld)"),
    ) -> dict[str, Any]:
        """Record that patterns were shown in a briefing.

        Called by the briefing hook after injecting strategy tips.
        This enables the feedback loop: we track which sessions received
        which tips, then compare outcomes to measure tip effectiveness.

        The `group` parameter supports A/B testing: 'treatment' means tips
        were actually displayed, 'control' means they were withheld.
        """
        ids = [p.strip() for p in pattern_ids.split(",") if p.strip()]
        recorded = await record_tip_shown(r, session_id, ids, group=group)
        return {"recorded": recorded, "session_id": session_id, "pattern_ids": ids, "group": group}

    @router.get("/effectiveness")
    async def tip_effectiveness(
        request: Request,
        r=Depends(get_replay_redis),
        identity: dict = Depends(require_scope("eval:read")),
    ) -> dict[str, Any]:
        """Get measured effectiveness of strategy tips.

        Shows which patterns actually improve outcomes when shown in briefings
        vs sessions that didn't receive them.

        SP1b §11: resolves briefing_id->session_id via Bridge so tips shown
        through GET /briefing join to session outcomes.
        """
        results = await _effectiveness_with_reconciliation(
            r, request.app.state.http_client, get_settings(),
        )
        return {"patterns": results, "count": len(results)}

    @router.post("/{pattern_id}/quarantine")
    async def quarantine_pattern_endpoint(
        pattern_id: str,
        r=Depends(get_replay_redis),
        identity: dict = Depends(require_scope("admin")),
        reason: str = Query(default="Manual review"),
    ) -> dict[str, Any]:
        """Immediately quarantine a pattern -- removes from all briefings."""
        from app.patterns.lifecycle import quarantine_pattern as do_quarantine
        from app.patterns.store import _PATTERN_PREFIX, _DEFAULT_TTL

        key = f"{_PATTERN_PREFIX}{pattern_id}"
        raw = await r.get(key)
        if not raw:
            raise HTTPException(status_code=404, detail=f"Pattern {pattern_id} not found")

        pattern = PatternCard.model_validate_json(raw)
        updated = do_quarantine(pattern, reason)
        await r.set(key, updated.model_dump_json(), ex=_DEFAULT_TTL)

        return {
            "id": pattern_id,
            "stage": updated.stage,
            "quarantine_reason": updated.quarantine_reason,
            "quarantined_at": updated.quarantined_at.isoformat() if updated.quarantined_at else None,
        }

    @router.post("/{pattern_id}/unquarantine")
    async def unquarantine_pattern_endpoint(
        pattern_id: str,
        r=Depends(get_replay_redis),
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        """Lift quarantine -- returns pattern to candidate stage for re-evaluation."""
        from app.patterns.lifecycle import unquarantine_pattern as do_unquarantine
        from app.patterns.store import _PATTERN_PREFIX, _DEFAULT_TTL

        key = f"{_PATTERN_PREFIX}{pattern_id}"
        raw = await r.get(key)
        if not raw:
            raise HTTPException(status_code=404, detail=f"Pattern {pattern_id} not found")

        pattern = PatternCard.model_validate_json(raw)
        if pattern.stage != "quarantined":
            raise HTTPException(status_code=400, detail=f"Pattern {pattern_id} is not quarantined (stage={pattern.stage})")

        updated = do_unquarantine(pattern)
        await r.set(key, updated.model_dump_json(), ex=_DEFAULT_TTL)

        return {
            "id": pattern_id,
            "stage": updated.stage,
        }

    # ------------------------------------------------------------------
    # Dataset + Experiment endpoints (PATTERN_EXPERIMENTS_ENABLED only)
    # ------------------------------------------------------------------
    from app.config import get_settings as _get_settings
    if _get_settings().PATTERN_EXPERIMENTS_ENABLED:
        from app.patterns.models import Dataset, Experiment
        from app.patterns.store import (
            get_dataset, list_datasets, delete_dataset,
            materialize_dataset, get_dataset_features, store_experiment,
            get_experiment, list_experiments, _load_tip_groups,
        )
        from app.patterns.statistics import compute_experiment_results, minimum_sample_size

        class CreateDatasetRequest(BaseModel):
            name: str
            description: str = ""
            date_min: datetime | None = None
            date_max: datetime | None = None
            agent_ids: list[str] = []
            goal_pattern: str = ""
            outcome_filter: str = ""

        @router.post("/datasets")
        async def create_dataset(
            body: CreateDatasetRequest,
            r=Depends(get_replay_redis),
            identity: dict = Depends(require_scope("eval:read")),
        ) -> dict[str, Any]:
            """Create a dataset with filter criteria, then materialize matching sessions."""
            h = hashlib.sha256(f"{body.name}{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:8]
            ds = Dataset(
                id=f"dset_{h}",
                name=body.name,
                description=body.description,
                date_min=body.date_min,
                date_max=body.date_max,
                agent_ids=body.agent_ids,
                goal_pattern=body.goal_pattern,
                outcome_filter=body.outcome_filter,
            )
            ds = await materialize_dataset(r, ds)
            return {"dataset": ds.model_dump(mode="json")}

        @router.get("/datasets")
        async def list_datasets_endpoint(
            r=Depends(get_replay_redis),
            identity: dict = Depends(require_scope("eval:read")),
            limit: int = Query(default=50, ge=1, le=200),
        ) -> dict[str, Any]:
            """List all datasets."""
            datasets = await list_datasets(r, limit=limit)
            return {
                "datasets": [d.model_dump(mode="json") for d in datasets],
                "count": len(datasets),
            }

        @router.get("/datasets/{dataset_id}")
        async def get_dataset_endpoint(
            dataset_id: str,
            r=Depends(get_replay_redis),
            identity: dict = Depends(require_scope("eval:read")),
        ) -> dict[str, Any]:
            """Get a dataset by ID."""
            ds = await get_dataset(r, dataset_id)
            if not ds:
                raise HTTPException(status_code=404, detail=f"Dataset {dataset_id} not found")
            return {"dataset": ds.model_dump(mode="json")}

        @router.delete("/datasets/{dataset_id}")
        async def delete_dataset_endpoint(
            dataset_id: str,
            r=Depends(get_replay_redis),
            identity: dict = Depends(require_scope("admin")),
        ) -> dict[str, Any]:
            """Delete a dataset."""
            deleted = await delete_dataset(r, dataset_id)
            if not deleted:
                raise HTTPException(status_code=404, detail=f"Dataset {dataset_id} not found")
            return {"deleted": True, "id": dataset_id}

        class CreateExperimentRequest(BaseModel):
            name: str
            hypothesis: str
            pattern_id: str
            dataset_id: str

        @router.post("/experiments")
        async def create_experiment(
            body: CreateExperimentRequest,
            r=Depends(get_replay_redis),
            identity: dict = Depends(require_scope("eval:read")),
        ) -> dict[str, Any]:
            """Create an experiment linking a pattern to a dataset."""
            ds = await get_dataset(r, body.dataset_id)
            if not ds:
                raise HTTPException(status_code=404, detail=f"Dataset {body.dataset_id} not found")

            from app.patterns.store import _PATTERN_PREFIX
            raw = await r.get(f"{_PATTERN_PREFIX}{body.pattern_id}")
            if not raw:
                raise HTTPException(status_code=404, detail=f"Pattern {body.pattern_id} not found")

            h = hashlib.sha256(f"{body.name}{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:8]
            exp = Experiment(
                id=f"exp_{h}",
                name=body.name,
                hypothesis=body.hypothesis,
                pattern_id=body.pattern_id,
                dataset_id=body.dataset_id,
            )

            features = await get_dataset_features(r, ds)
            tip_groups = await _load_tip_groups(r)
            exp = compute_experiment_results(exp, features, body.pattern_id, tip_groups)

            await store_experiment(r, exp)
            return {"experiment": exp.model_dump(mode="json")}

        @router.get("/experiments")
        async def list_experiments_endpoint(
            r=Depends(get_replay_redis),
            identity: dict = Depends(require_scope("eval:read")),
            limit: int = Query(default=50, ge=1, le=200),
            status: str = Query(default="", description="Filter by status"),
        ) -> dict[str, Any]:
            """List all experiments."""
            experiments = await list_experiments(r, limit=limit)
            if status:
                experiments = [e for e in experiments if e.status == status]
            return {
                "experiments": [e.model_dump(mode="json") for e in experiments],
                "count": len(experiments),
            }

        @router.get("/experiments/{experiment_id}")
        async def get_experiment_endpoint(
            experiment_id: str,
            r=Depends(get_replay_redis),
            identity: dict = Depends(require_scope("eval:read")),
        ) -> dict[str, Any]:
            """Get experiment details with stats."""
            exp = await get_experiment(r, experiment_id)
            if not exp:
                raise HTTPException(status_code=404, detail=f"Experiment {experiment_id} not found")

            result = exp.model_dump(mode="json")

            ds = await get_dataset(r, exp.dataset_id)
            if ds and ds.metrics_summary:
                baseline = ds.metrics_summary.get("success_rate", 0.5)
                try:
                    result["recommended_sample_size"] = minimum_sample_size(baseline_rate=baseline)
                except Exception:
                    result["recommended_sample_size"] = None

            return {"experiment": result}

        @router.post("/experiments/{experiment_id}/conclude")
        async def conclude_experiment(
            experiment_id: str,
            r=Depends(get_replay_redis),
            identity: dict = Depends(require_scope("admin")),
        ) -> dict[str, Any]:
            """Manually conclude an experiment — recomputes stats and sets final status."""
            exp = await get_experiment(r, experiment_id)
            if not exp:
                raise HTTPException(status_code=404, detail=f"Experiment {experiment_id} not found")
            if exp.status != "running":
                raise HTTPException(status_code=400, detail=f"Experiment is already {exp.status}")

            ds = await get_dataset(r, exp.dataset_id)
            if not ds:
                raise HTTPException(status_code=404, detail=f"Dataset {exp.dataset_id} not found")

            features = await get_dataset_features(r, ds)
            tip_groups = await _load_tip_groups(r)
            exp = compute_experiment_results(exp, features, exp.pattern_id, tip_groups)

            if exp.verdict == "insufficient data":
                exp.status = "inconclusive"
            else:
                exp.status = "concluded"

            await store_experiment(r, exp)
            return {"experiment": exp.model_dump(mode="json")}

        logger.debug("Pattern experiment endpoints registered (PATTERN_EXPERIMENTS_ENABLED=true)")
    else:
        logger.debug("Pattern experiment endpoints disabled (PATTERN_EXPERIMENTS_ENABLED=false)")

    return router
