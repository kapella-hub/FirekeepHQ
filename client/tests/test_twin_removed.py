"""twin/ is dead code (removed in 0632bfc); this pins its full removal.

Verifies the working tree has no twin/ directory and that every source/config
file that carried a cosmetic 'twin' reference has been scrubbed. Historical
design records (docs/superpowers/**, docs/ops/**) and intentional retired-scope
assertions (deploy/** -> 'twin:read is retired') are deliberately NOT scanned.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every file that held a cosmetic/docstring 'twin' reference before this task.
SCRUBBED_FILES = [
    "AGENTS.md",
    "docker-compose.yml",
    "docs/COMPARISON.md",
    "cortex/tests/conftest.py",
    "cortex/app/db/graph.py",
    "bridge/app/mcp_server.py",
    "bridge/tests/test_sessions_route.py",
    "sentinel/app/mcp_server.py",
    "relay/app/routes.py",
    "relay/tests/test_status_route.py",
]

_TWIN = re.compile(r"\btwin", re.IGNORECASE)


def test_twin_directory_is_gone():
    assert not (REPO_ROOT / "twin").exists(), "twin/ still present in the working tree"


def test_no_twin_references_in_scrubbed_files():
    offenders = []
    for rel in SCRUBBED_FILES:
        p = REPO_ROOT / rel
        assert p.exists(), f"expected file missing: {rel}"
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if _TWIN.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "live twin references remain:\n" + "\n".join(offenders)
