"""One guarded LLM call: a cluster of episodes -> 1-3 durable insights.

`think: False` is NOT optional. Measured on the VPS (qwen3:4b, 4 vCPU): with it,
22.5s and correct JSON; without it the model burns its entire num_predict budget
on thinking that the JSON grammar blocks and returns EMPTY content after 101s.
Both spellings are sent because ollama accepts the native `think` flag and the
OpenAI-compatible path reads `chat_template_kwargs.enable_thinking`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import httpx

from app.dreams.select import Candidate

logger = logging.getLogger(__name__)

_MAX_INSIGHTS = 3
_EPISODE_TRUNCATE_CHARS = 600
_DEFAULT_MAX_CHARS = 800


def _system_prompt(max_chars: int) -> str:
    """The char budget is interpolated in so the model has a concrete length
    cue — without one it has no idea what "concise" means and can burn a full
    ~22s CPU call producing insights parse_insights then silently discards for
    being over max_chars."""
    return (
        "You are the Dreaming pass for a long-term agent memory store. You are given "
        "a cluster of episodic memories that were judged similar to each other. Find "
        "1-3 DURABLE, GENERAL insights that are each supported by MULTIPLE episodes "
        "in the cluster — not a restatement of any single episode.\n\n"
        "Rules:\n"
        "- Each insight must be a general lesson, not a specific event.\n"
        "- Each insight must be supported by at least 2 episodes; cite them by index.\n"
        "- Do not invent facts not present in the episodes.\n"
        f"- Keep each insight under {max_chars} characters; longer insights are discarded.\n\n"
        "## Output Format\n"
        "Return ONLY a valid JSON object. No markdown fencing, no explanation.\n\n"
        "{\n"
        '  "insights": [\n'
        '    {"content": "<the durable lesson>", "memory_type": "procedural", '
        '"source_indices": [0, 2]}\n'
        "  ]\n"
        "}"
    )


@dataclass
class Insight:
    content: str
    memory_type: str
    source_ids: list[str] = field(default_factory=list)


def build_messages(members: list[Candidate], *, max_chars: int = _DEFAULT_MAX_CHARS) -> list[dict]:
    lines = []
    for i, member in enumerate(members):
        text = member.text[:_EPISODE_TRUNCATE_CHARS]
        lines.append(f"[{i}] {text}")
    user_content = "\n".join(lines)
    return [
        {"role": "system", "content": _system_prompt(max_chars)},
        {"role": "user", "content": user_content},
    ]


def build_request_body(model: str, messages: list[dict]) -> dict:
    return {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "think": False,
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": 700,
    }


def parse_insights(raw: str, members: list[Candidate], *, max_chars: int) -> list[Insight]:
    """Validate and convert raw LLM JSON into Insights. Never raises — any
    malformed input (bad JSON, wrong shape, out-of-range indices, empty/overlong
    content) is rejected item-by-item or as a whole, returning [] at worst."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []

    if not isinstance(data, dict):
        return []

    raw_insights = data.get("insights")
    if not isinstance(raw_insights, list):
        return []

    member_ids = [m.id for m in members]
    out: list[Insight] = []
    for item in raw_insights:
        if len(out) >= _MAX_INSIGHTS:
            break
        if not isinstance(item, dict):
            continue

        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if len(content) > max_chars:
            continue

        source_indices = item.get("source_indices")
        if not isinstance(source_indices, list) or not source_indices:
            continue
        if not all(isinstance(i, int) and not isinstance(i, bool) for i in source_indices):
            continue
        if not all(0 <= i < len(member_ids) for i in source_indices):
            continue

        source_ids = [member_ids[i] for i in source_indices]

        # memory_type is FORCED to procedural regardless of what the model
        # returned — "reference" means no age decay at all (permanent rank
        # immunity), which an unreviewed auto-approved memory must never get.
        out.append(Insight(content=content, memory_type="procedural", source_ids=source_ids))

    return out[:_MAX_INSIGHTS]


async def synthesize(
    members: list[Candidate],
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float,
    max_chars: int,
    client: httpx.AsyncClient | None = None,
) -> list[Insight]:
    """Turn a cluster of episodes into 0-3 durable insights via one guarded LLM
    call. Returns [] on ANY failure — unreachable backend, non-2xx response, an
    unusable cluster (e.g. a corrupt Qdrant payload with non-string text), or an
    empty/invalid parse. Never raises.

    The whole body is wrapped in a single outer guard (mirrors
    app/knowledge/classifier.py's fail-loud-but-never-raise posture): request
    construction (build_messages/build_request_body) runs INSIDE the try, not
    before it, because a background orchestrator loops many clusters and one bad
    candidate must not kill the run.

    Malformed model JSON gets exactly one retry (a fresh call to the same
    backend); any other failure (connection error, non-2xx, unexpected response
    shape) returns [] immediately without retrying."""
    own_client = client is None
    http_client: httpx.AsyncClient | None = None
    try:
        http_client = client if client is not None else httpx.AsyncClient(timeout=timeout)
        messages = build_messages(members, max_chars=max_chars)
        body = build_request_body(model, messages)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        url = f"{base_url}/chat/completions"

        text: str | None = None
        for attempt in range(2):
            try:
                resp = await http_client.post(url, json=body, headers=headers, timeout=timeout)
                resp.raise_for_status()

                msg = resp.json()["choices"][0]["message"]
                candidate = msg.get("content") or ""
                if not candidate.strip():
                    candidate = msg.get("reasoning") or ""

                # Validate the model actually returned parseable JSON before
                # committing to it — a malformed body gets one retry; a real
                # backend/transport failure does not.
                json.loads(candidate)
                text = candidate
                break
            except (json.JSONDecodeError, TypeError) as exc:
                if attempt == 0:
                    continue
                logger.warning("Dream synthesis returned malformed JSON after retry: %s", exc)
                return []
            except Exception as exc:
                logger.warning("Dream synthesis failed: %s", exc)
                return []

        if text is None:
            return []
        return parse_insights(text, members, max_chars=max_chars)
    except Exception as exc:
        # Catches anything the loop above doesn't — e.g. a corrupt candidate
        # (non-string .text) blowing up build_messages, or client construction
        # itself failing. Nothing may escape synthesize().
        logger.warning("Dream synthesis failed: %s", exc)
        return []
    finally:
        if own_client and http_client is not None:
            await http_client.aclose()
