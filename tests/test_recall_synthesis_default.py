"""Keep recall synthesis opt-in across every shipped configuration surface."""

import ast
import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _compose_default() -> bool:
    text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    match = re.search(
        r"RECALL_SYNTHESIS_ENABLED:\s*\$\{RECALL_SYNTHESIS_ENABLED:-(true|false)\}",
        text,
    )
    assert match is not None, "Compose must expose RECALL_SYNTHESIS_ENABLED"
    return match.group(1) == "true"


def _example_default() -> bool:
    for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("RECALL_SYNTHESIS_ENABLED="):
            value = line.partition("=")[2]
            assert value in {"true", "false"}
            return value == "true"
    raise AssertionError(".env.example must document RECALL_SYNTHESIS_ENABLED")


def _application_default() -> bool:
    tree = ast.parse(
        (REPO / "cortex" / "app" / "config.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "RECALL_SYNTHESIS_ENABLED":
                return ast.literal_eval(node.value)
    raise AssertionError("Settings must define RECALL_SYNTHESIS_ENABLED")


def _project_guide_default() -> bool:
    text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    table_match = re.search(
        r"\| `RECALL_SYNTHESIS_ENABLED` \| `(true|false)` \|",
        text,
    )
    prose_match = re.search(
        r"Config:[^\n]*RECALL_SYNTHESIS_ENABLED=(true|false)",
        text,
    )
    assert table_match is not None, "CLAUDE.md configuration table must document recall synthesis"
    assert prose_match is not None, "CLAUDE.md recall guidance must document synthesis"
    assert table_match.group(1) == prose_match.group(1), (
        "CLAUDE.md recall guidance and configuration table must agree"
    )
    return table_match.group(1) == "true"


def test_recall_synthesis_is_opt_in_everywhere():
    defaults = {
        "application": _application_default(),
        "compose": _compose_default(),
        "env_example": _example_default(),
        "project_guide": _project_guide_default(),
    }
    assert defaults == {
        "application": False,
        "compose": False,
        "env_example": False,
        "project_guide": False,
    }
