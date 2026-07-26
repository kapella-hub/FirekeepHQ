"""List indexed repositories."""

import time
from typing import Optional

from ..storage import IndexStore


def list_repos(storage_path: Optional[str] = None) -> dict:
    """List all indexed repositories."""
    start = time.perf_counter()
    store = IndexStore(base_path=storage_path)
    repos = store.list_repos()
    elapsed = (time.perf_counter() - start) * 1000

    return {
        "count": len(repos),
        "repos": repos,
        "_meta": {
            "timing_ms": round(elapsed, 1),
        },
    }


TOOL_DEF = {
    "name": "list_repos",
    "description": "List all indexed repositories.",
    "inputSchema": {
        "type": "object",
        "properties": {},
    },
    "handler": list_repos,
}
