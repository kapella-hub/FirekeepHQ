"""Prior art — what the team already built, answered at the moment of intent.

`ctx_start_session` is the one call in the stack that knows what an agent is
about to do BEFORE it does it. Everything else in the memory product waits to be
asked, and the failure that keeps recurring is that nobody asks: an agent that
does not know the team has been here before has no trigger to call
`memory_recall`, so the knowledge is retrievable and never retrieved. Declaring
a goal IS the trigger, and this module is what turns it into an answer.

Two legs, deliberately different in kind:

* **Team memory** — a `/memory/recall` against Cortex for the goal text. Sibling
  of `app/proactive_recall.py`; the outbound shape (X-API-Key, `format: "raw"`,
  raw-cosine floor) is the same, and the two stay separate because this one
  carries `trigger: "prior-art"` for the compliance slice and maps sources into
  a summary shape rather than the shadow's `{content, score}`.
* **In flight now** — Bridge's OWN active sessions belonging to OTHER agents.
  No similarity filtering: teams are small, a wrong omission costs a duplicated
  week and a wrong inclusion costs one line, so the asymmetry says list them.

Everything here is best-effort and fails to `{}`. A session must be created
whether or not Cortex is reachable — prior art is an enhancement on the start
path, never a dependency of it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

#: Chars of a recalled memory kept for the one-line summary. Long enough to
#: recognise the work, short enough that three of them do not crowd out the
#: response the agent actually asked for.
SUMMARY_MAX_CHARS = 200

#: Same idea for an in-flight session's goal, tighter because several share one
#: rendered line.
GOAL_MAX_CHARS = 120

#: How deep into the recency-ordered session index to look for in-flight work.
#: `list_sessions` already filters by status server-side; this bounds how many
#: OTHER-agent candidates can be found before the caller's own active sessions
#: crowd the window, at 100 sessions max in the index (`NB_MAX_SESSIONS`).
IN_FLIGHT_SCAN_LIMIT = 10


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _collapse(text: str, limit: int) -> str:
    """One line, whitespace collapsed, truncated with a visible ellipsis."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rstrip() + "..."


def _when(metadata: dict) -> str:
    """The date a memory was written, or "" when it carries none.

    Cortex stamps `timestamp` (`engine/rag.py`); `created_at`/`date` are read
    too so a source shaped by a different writer still dates itself. Only the
    date part is kept — the hour a memory was stored is noise at this altitude.
    """
    if not isinstance(metadata, dict):
        return ""
    for field in ("timestamp", "created_at", "date"):
        value = metadata.get(field)
        if value:
            return str(value)[:10]
    return ""


def _ago(stamp: str) -> str:
    """"5m ago" / "2h ago" / "3d ago" from an ISO timestamp; "" if unreadable.

    Returning "" rather than the raw stamp is deliberate: the renderer drops the
    parenthetical entirely, which reads better than a naked ISO string, and an
    unparseable timestamp must never cost the reader the line it sits on.
    """
    try:
        started = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return ""
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - started).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


# ---------------------------------------------------------------------------
# The two legs
# ---------------------------------------------------------------------------


async def fetch_team_memories(
    goal: str,
    api_url: str,
    api_key: str | None = None,
    top_k: int = 3,
    min_score: float = 0.55,
    timeout: float = 2.5,
) -> list[dict]:
    """Memories the team already holds about *goal*, above the raw-cosine floor.

    The floor is applied to `metadata.raw_score`, NEVER to `score`. Cortex's own
    measurement comment (`cortex/app/main.py`) is explicit about why: `score`
    comes out of `_min_max_normalize`, which sets the best entry in the set to
    exactly 1.0 by construction, so it reads 1.0 whenever anything survives —
    "how do I deploy to the VPS" and a nonsense query about knitting patterns
    both returned 1.0 on the live deployment. A floor on that number filters
    nothing. Sources with no `raw_score` (graph-only bare entries) cannot be
    ranked honestly and are dropped rather than guessed at.

    Returns [] on ANY failure. Never raises.
    """
    if not goal or len(goal.strip()) < 10:
        return []

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    payload = {
        "task": goal[:500],
        "top_k": top_k,
        # Hot path on session start: raw list, no LLM synthesis.
        "format": "raw",
        # Marks this recall as PUSHED at the moment of intent rather than
        # deliberately called, so the compliance measurement can tell the two
        # apart (the `prompt-hook` precedent in Cortex's RecallRequest).
        "trigger": "prior-art",
    }
    # No `namespace`: on Cortex a namespace is a CATEGORY, not a partition, and
    # naming one here would hide every memory an agent filed under another.

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{api_url}/memory/recall",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        if data.get("degraded"):
            # Graph-only results carry no cosine, so the floor below would be
            # comparing against nothing. Silence beats a confident guess here.
            return []

        results: list[dict] = []
        for source in data.get("sources", []) or []:
            metadata = source.get("metadata") or {}
            raw = metadata.get("raw_score")
            if raw is None:
                continue
            try:
                raw = float(raw)
            except (TypeError, ValueError):
                continue
            if raw < min_score:
                continue
            results.append({
                "summary": _collapse(source.get("content", ""), SUMMARY_MAX_CHARS),
                "raw_score": raw,
                "when": _when(metadata),
            })
        return results
    except Exception as exc:
        logger.debug("Prior-art recall failed (non-fatal): %s", exc)
        return []


async def fetch_in_flight(mgr, agent_id: str, limit: int = 3) -> list[dict]:
    """Active sessions belonging to agents OTHER than the caller, newest first.

    The caller's own sessions are excluded rather than merely deduplicated —
    including the session `start_session` created two lines earlier would tell
    an agent it is in flight on its own goal, which is noise dressed as a
    finding, and a second terminal of the same agent is the same person.

    Returns [] on ANY failure. Never raises.
    """
    try:
        sessions = await mgr.list_sessions(status="active", limit=IN_FLIGHT_SCAN_LIMIT)
    except Exception as exc:
        logger.debug("Prior-art in-flight lookup failed (non-fatal): %s", exc)
        return []

    out: list[dict] = []
    for session in sessions:
        owner = session.get("agent_id") or ""
        if not owner or owner == agent_id:
            continue
        out.append({
            "agent_id": owner,
            "goal": _collapse(session.get("goal", ""), GOAL_MAX_CHARS),
            "started_at": session.get("created_at", ""),
        })
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Assembly and rendering
# ---------------------------------------------------------------------------


async def assemble_prior_art(
    goal: str,
    *,
    mgr,
    agent_id: str,
    api_url: str,
    api_key: str | None = None,
    top_k: int = 3,
    min_score: float = 0.55,
    in_flight_max: int = 3,
    timeout: float = 2.5,
) -> dict:
    """`{"memories": [...], "in_flight": [...]}`, or `{}` when there is nothing.

    Both legs run concurrently under ONE deadline, so the worst case a session
    start pays is `timeout` and not the sum. `httpx`'s own timeout is not enough
    on its own — it applies per phase, so a host that accepts and then stalls
    can spend it twice — and the deadline also bounds the Redis leg.

    `{}` rather than `{"memories": [], "in_flight": []}` on empty: the key's
    absence is the signal, and it makes "nothing found" and "Cortex was down"
    the same shape on the wire, which is what fail-open means here.
    """
    async def _leg(coro, label: str) -> list[dict]:
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except Exception as exc:
            logger.info("Prior-art %s leg skipped (non-fatal): %s", label, exc)
            return []

    try:
        memories, in_flight = await asyncio.gather(
            _leg(
                fetch_team_memories(
                    goal,
                    api_url=api_url,
                    api_key=api_key,
                    top_k=top_k,
                    min_score=min_score,
                    timeout=timeout,
                ),
                "recall",
            ),
            _leg(fetch_in_flight(mgr, agent_id, limit=in_flight_max), "in-flight"),
        )
    except Exception as exc:  # pragma: no cover — both legs already swallow
        logger.info("Prior-art assembly skipped (non-fatal): %s", exc)
        return {}

    if not memories and not in_flight:
        return {}
    return {"memories": memories, "in_flight": in_flight}


def render_prior_art(prior_art: dict) -> str:
    """The block an agent reads. "" when there is nothing to say.

    Structured fields are for the dashboard and the tests; this is for the
    model, which is why it leads with the instruction ("recall before building")
    rather than the data. A block that only listed matches would be a fact the
    agent is free to skim past.
    """
    memories = (prior_art or {}).get("memories") or []
    in_flight = (prior_art or {}).get("in_flight") or []
    if not memories and not in_flight:
        return ""

    lines = [
        "[prior art] the team may have been here before — recall before building:"
    ]
    for memory in memories:
        lines.append(f"- {memory['summary']} (raw {memory['raw_score']:.2f})")
    if in_flight:
        entries = []
        for session in in_flight:
            entry = f"{session['agent_id']} — \"{session['goal']}\""
            ago = _ago(session.get("started_at", ""))
            if ago:
                entry += f" ({ago})"
            entries.append(entry)
        lines.append("in flight right now: " + "; ".join(entries))
    return "\n".join(lines)
