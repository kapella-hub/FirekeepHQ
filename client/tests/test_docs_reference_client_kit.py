"""Live, user-facing docs must describe the client-kit install flow, not the
retired bash hooks / local-setup scripts. Historical design records under
docs/superpowers/** and docs/ops/** are excluded by construction (LIVE_DOCS is
an explicit allowlist).
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

LIVE_DOCS = [
    "CLAUDE.md",
    "cortex/CLAUDE.md",
    "AGENTS.md",
    "README.md",
    "docs/MULTI-AGENT.md",
    "docs/SETUP-CLAUDE-CODE.md",
    "docs/SETUP-CODEX.md",
    "docs/DESIGN.md",
    "docs/INTEGRATIONS.md",
    "docs/DEPLOYMENT-OFFICE.md",
    "docs/OVERVIEW.md",
    "docs/PITCH.md",
    "docs/COMPARISON.md",
]

# Retired tokens that must not survive in any live doc. NB: server scripts
# install.sh/update.sh are NOT forbidden (they stay).
FORBIDDEN = [
    "local-setup",
    "scripts/briefing.sh",
    "scripts/debrief.sh",
    "scripts/multi-agent-poll.sh",
    "scripts/multi-agent-precheck.sh",
    "scripts/multi-agent-postaction.sh",
    "scripts/start-agent.sh",
    "scripts/lib",
    "vps-detect",
    "hook-log.sh",
]

# New-content smoke checks (grep gates absence of stale tokens; these gate that
# real replacement prose was written, not just deletions).
REQUIRED = {
    "README.md": ["firekeep install"],
    "docs/SETUP-CLAUDE-CODE.md": ["firekeep install"],
    "docs/SETUP-CODEX.md": ["firekeep install"],
    "docs/OVERVIEW.md": ["firekeep install"],
    "docs/MULTI-AGENT.md": ["firekeep_client.hooks"],
    "CLAUDE.md": ["firekeep install", "~/.firekeep"],
}


def test_no_forbidden_tokens_in_live_docs():
    offenders = []
    for rel in LIVE_DOCS:
        p = REPO_ROOT / rel
        assert p.exists(), f"expected doc missing: {rel}"
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            for tok in FORBIDDEN:
                if tok in line:
                    offenders.append(f"{rel}:{i}: {tok}")
    assert not offenders, "stale retired references remain:\n" + "\n".join(offenders)


def test_required_client_kit_tokens_present():
    missing = []
    for rel, toks in REQUIRED.items():
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for tok in toks:
            if tok not in text:
                missing.append(f"{rel} -> {tok}")
    assert not missing, "client-kit replacement content missing:\n" + "\n".join(missing)
