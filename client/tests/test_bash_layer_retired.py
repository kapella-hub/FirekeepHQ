"""The bash hook layer + repo-checkout installer are retired by the client kit.

Asserts the retired files are gone and that no live server/config/CI surface
still references them by path. Docs are handled by Task 31 (test_docs_...), so
docs/ is intentionally excluded here to avoid an ordering deadlock. The symdex
plugin's own scripts/ (different basenames) and generic PathDenyRule glob
examples ('scripts/*.sh') do not match the exact retired paths below.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

RETIRED_PATHS = [
    "scripts/briefing.sh",
    "scripts/debrief.sh",
    "scripts/multi-agent-poll.sh",
    "scripts/multi-agent-precheck.sh",
    "scripts/multi-agent-postaction.sh",
    "scripts/start-agent.sh",
    "scripts/lib/hook-log.sh",
    "scripts/lib/vps-detect.sh",
    "local-setup.sh",
    "local-setup.ps1",
    "local-setup-codex.sh",
    "local-setup-codex.ps1",
]

# Live executable/wiring surfaces scanned for stale path references (NOT docs).
CODE_ROOTS = [
    "cortex/app", "bridge/app", "sentinel/app", "relay/app",
    "deploy", ".github",
]
CODE_FILES = ["docker-compose.yml", "install.sh", "update.sh"]
SCAN_EXTS = {".py", ".yml", ".yaml", ".toml", ".json", ".sh", ".ps1"}

# Exact retired path strings to search for in code (path forms, not bare names).
FORBIDDEN_PATH_TOKENS = RETIRED_PATHS + ["scripts/lib/", "vps-detect", "hook-log.sh"]


def test_retired_files_are_deleted():
    present = [p for p in RETIRED_PATHS if (REPO_ROOT / p).exists()]
    assert not present, f"retired files still present: {present}"


def _iter_code_files():
    for root in CODE_ROOTS:
        base = REPO_ROOT / root
        if base.exists():
            for p in base.rglob("*"):
                if p.is_file() and p.suffix in SCAN_EXTS and "__pycache__" not in p.parts:
                    yield p
    for f in CODE_FILES:
        p = REPO_ROOT / f
        if p.exists():
            yield p


def test_no_code_surface_references_retired_scripts():
    offenders = []
    for p in _iter_code_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for tok in FORBIDDEN_PATH_TOKENS:
            if tok in text:
                offenders.append(f"{p.relative_to(REPO_ROOT)} -> {tok}")
    assert not offenders, "stale retired-script references in code:\n" + "\n".join(offenders)


def test_untagged_calls_docstring_names_new_consumer():
    text = (REPO_ROOT / "cortex/app/main.py").read_text(encoding="utf-8")
    assert "Used by briefing.sh" not in text, "main.py still names the retired briefing.sh as a live consumer"
    assert re.search(r"session_start hook core", text), "main.py should name the session_start hook core"
