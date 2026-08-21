"""Persistent token savings tracker.

Records cumulative tokens saved across all tool calls by comparing
raw file sizes against actual MCP response sizes.

Stored in ~/.code-index/_savings.json — a single small JSON file.
No API calls, no file reads — only os.stat for file sizes.

Community meter: OPT-IN, off by default. Nothing leaves the machine unless
FIREKEEP_SYMDEX_SHARE_STATS=1 is set. When it is, a fire-and-forget POST to
https://j.gravelle.us carries exactly {"delta": N, "anon_id": "<uuid>"} —
never code, paths, repo names, or anything identifying.

Until 2026-08-21 this paragraph described the opposite — a default-on share
gated by a flag name that does not exist in this package. The behaviour was
always the safe one; only the documentation was wrong, which on the single
code path here that opens an outbound connection is its own kind of bug.

If you change the flag or the endpoint, change this paragraph in the same
commit. It is the only place a reader can learn what leaves their machine, and
tests/test_telemetry_is_opt_in.py pins the two together.

The savings counters are recorded here and deliberately do NOT ride in tool
results — see tests/test_wire_economy.py. Surface them via `firekeep doctor`.
"""

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Optional

_SAVINGS_FILE = "_savings.json"
_BYTES_PER_TOKEN = 4  # ~4 bytes per token (rough but consistent)
_TELEMETRY_URL = "https://j.gravelle.us/APIs/savings/post.php"
_LOCK = threading.Lock()

# Input token pricing ($ per token). Update as models reprice.
PRICING = {
    "claude_opus":  15.00 / 1_000_000,  # Claude Opus 4.6 — $15.00 / 1M input tokens
    "gpt5_latest":  10.00 / 1_000_000,  # GPT-5.2 (latest flagship GPT) — $10.00 / 1M input tokens
}


def _savings_path(base_path: Optional[str] = None) -> Path:
    root = Path(base_path) if base_path else Path.home() / ".code-index"
    root.mkdir(parents=True, exist_ok=True)
    return root / _SAVINGS_FILE


def _get_or_create_anon_id(data: dict) -> str:
    """Return the persistent anonymous install ID, creating it if absent."""
    if "anon_id" not in data:
        data["anon_id"] = str(uuid.uuid4())
    return data["anon_id"]


def _share_savings(delta: int, anon_id: str) -> None:
    """Fire-and-forget POST to the community meter. Never raises."""
    def _post() -> None:
        try:
            import httpx
            httpx.post(
                _TELEMETRY_URL,
                json={"delta": delta, "anon_id": anon_id},
                timeout=3.0,
            )
        except Exception:
            pass

    threading.Thread(target=_post, daemon=True).start()


def record_savings(tokens_saved: int, base_path: Optional[str] = None) -> int:
    """Add tokens_saved to the running total. Returns new cumulative total.

    Thread-safe: uses a module-level lock to protect the read-modify-write cycle.
    """
    path = _savings_path(base_path)
    delta = max(0, tokens_saved)

    with _LOCK:
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except Exception:
            data = {}

        total = data.get("total_tokens_saved", 0) + delta
        data["total_tokens_saved"] = total

        if delta > 0 and os.environ.get("FIREKEEP_SYMDEX_SHARE_STATS", "0") == "1":
            anon_id = _get_or_create_anon_id(data)
            _share_savings(delta, anon_id)

        try:
            path.write_text(json.dumps(data))
        except Exception:
            pass

    return total


def get_total_saved(base_path: Optional[str] = None) -> int:
    """Return the current cumulative total without modifying it."""
    path = _savings_path(base_path)
    try:
        return json.loads(path.read_text()).get("total_tokens_saved", 0)
    except Exception:
        return 0


def estimate_savings(raw_bytes: int, response_bytes: int) -> int:
    """Estimate tokens saved: (raw - response) / bytes_per_token."""
    return max(0, (raw_bytes - response_bytes) // _BYTES_PER_TOKEN)


def cost_avoided(tokens_saved: int, total_tokens_saved: int) -> dict:
    """Return cost avoided estimates for this call and the running total.

    Returns a dict ready to be merged into a _meta envelope:
        cost_avoided:       {claude_opus: float, gpt5_latest: float}
        total_cost_avoided: {claude_opus: float, gpt5_latest: float}

    Values are in USD, rounded to 4 decimal places.
    """
    return {
        "cost_avoided": {
            model: round(tokens_saved * rate, 4)
            for model, rate in PRICING.items()
        },
        "total_cost_avoided": {
            model: round(total_tokens_saved * rate, 4)
            for model, rate in PRICING.items()
        },
    }
