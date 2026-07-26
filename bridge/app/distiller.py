"""Distillation — converts session data to FirekeepCortex memories."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

# Field length limits for Cortex payloads
_MAX_FIELD_LEN = 5000
_MAX_TAGS = 20


def _truncate(text: str, limit: int = _MAX_FIELD_LEN) -> str:
    """Truncate text to *limit* characters, appending '...' if trimmed."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


class Distiller:
    """Distills completed session data into FirekeepCortex long-term memory."""

    def __init__(self, settings: Settings) -> None:
        self._api_url = settings.FIREKEEP_API_URL
        self._api_key = settings.FIREKEEP_API_KEY
        self._namespace = settings.FIREKEEP_NAMESPACE
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # Deprecated — use _build_episodic_payload instead.
    def build_payload(self, data: dict[str, Any], outcome: str | None = None) -> dict[str, Any]:
        """Build the /memory/learn payload from session data (legacy flat format)."""
        goal = data.get("goal", "Unknown task")
        plan = data.get("plan", "")

        # Extract completed steps from plan
        completed = re.findall(r"- \[x\] (.+)", plan)
        plan_summary = "; ".join(completed[:5]) if completed else "No plan steps recorded"

        action = f"{goal} — {plan_summary}"

        # Outcome: explicit > last progress > fallback
        if not outcome:
            progress = data.get("progress", [])
            if progress:
                outcome = progress[-1].get("content", "Session completed")
            else:
                outcome = "Session completed"

        # Resolution: join top 3 decisions
        decisions = data.get("decisions", [])
        resolution = "; ".join(d.get("content", "") for d in decisions[:3]) or None

        tags = list(data.get("tags", [])) + ["firekeepbridge"]

        return {
            "action": action,
            "outcome": outcome,
            "resolution": resolution,
            "tags": tags,
            "domain": "development",
            "namespace": self._namespace,
        }

    def _build_episodic_payload(
        self, data: dict[str, Any], outcome: str | None = None
    ) -> dict[str, Any]:
        """Build a rich episodic payload that preserves decision sequence and file context."""
        goal = data.get("goal", "Unknown task")
        plan = data.get("plan", "")

        # --- action: rich narrative ---
        completed = re.findall(r"- \[x\] (.+)", plan)
        plan_part = "; ".join(completed) if completed else "No plan steps recorded"

        decisions = data.get("decisions", [])
        decision_texts = [d.get("content", "") for d in decisions[:10] if d.get("content")]
        decisions_part = " → ".join(decision_texts) if decision_texts else "No decisions recorded"

        files = data.get("files", {})
        file_paths = sorted(files.keys()) if files else []
        files_part = ", ".join(file_paths) if file_paths else "No files"

        action = f"Task: {goal} | Plan: {plan_part} | Decisions: {decisions_part} | Files: {files_part}"
        action = _truncate(action)

        # --- outcome: explicit > last progress > fallback ---
        if not outcome:
            progress = data.get("progress", [])
            if progress:
                outcome = progress[-1].get("content", "Session completed")
            else:
                outcome = "Session completed"
        outcome = _truncate(outcome)

        # --- resolution: full progress sequence ---
        progress = data.get("progress", [])
        progress_texts = [p.get("content", "") for p in progress if p.get("content")]
        resolution: str | None = " → ".join(progress_texts) if progress_texts else None
        if resolution:
            resolution = _truncate(resolution)

        # --- tags: original + file extensions + marker ---
        tags = list(data.get("tags", []))
        for path in file_paths:
            _, ext = os.path.splitext(path)
            if ext:
                tag = ext.lstrip(".")
                if tag and tag not in tags:
                    tags.append(tag)
        if "firekeepbridge" not in tags:
            tags.append("firekeepbridge")
        tags = tags[:_MAX_TAGS]

        return {
            "action": action,
            "outcome": outcome,
            "resolution": resolution,
            "tags": tags,
            "domain": "development",
            "namespace": self._namespace,
            "memory_type": "episodic",
        }

    async def distill(
        self,
        data: dict[str, Any],
        outcome: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Send distilled session to FirekeepCortex. Returns status dict.

        Sends X-Session-Id / X-Agent-Id headers and the session's declared
        project (SP0 D2) so distillates are attributed instead of landing
        as agent_id="unknown". When the session declared no project, the
        field is omitted — never fabricated.
        """
        payload = self._build_episodic_payload(data, outcome)
        # Fall back to the session data dict so callers that pass only the
        # data (e.g. direct/legacy call sites) still produce attributed
        # distillates; Task 18's worker passes session_id explicitly.
        session_id = session_id or (data.get("session_id") or "").strip() or None
        project = (data.get("project") or "").strip()
        if project:
            payload["project"] = project

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        if session_id:
            headers["X-Session-Id"] = session_id
        agent_id = (data.get("agent_id") or "").strip()
        if agent_id:
            headers["X-Agent-Id"] = agent_id

        try:
            resp = await self._client.post(
                f"{self._api_url}/memory/learn",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            resp_data = resp.json()
            memory_id = resp_data.get("vector_id") or resp_data.get("graph_id")
            logger.info("Distilled session to FirekeepCortex: %s", memory_id)
            return {"status": "success", "firekeep_memory_id": memory_id}
        except Exception as exc:
            logger.error("Distillation failed: %s", exc)
            return {"status": "failed", "error": str(exc)}
