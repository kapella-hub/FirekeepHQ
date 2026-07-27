"""Configuration a customer can set must do something.

Two dead settings shipped, and they failed differently:

`CORTEX_INSTALL_FINETUNE_DEPS` — `docker-compose.yml` passed it as a build arg, no
Dockerfile declared the `ARG`, nothing read it, and `docs/CONFIGURATION.md` told the
reader to set it to `true`, rebuild `cortex-worker`, and call
`POST /admin/embeddings/finetune`. That route does not exist, nothing imports
`sentence_transformers`, and the package is in no requirements file. The entire
feature was absent while three artifacts described it. Docker prints a warning for an
unconsumed build arg and proceeds, so nothing failed.

`CORS_ORIGINS` — `.env.example` said "used by all services". Only `cortex/app/main.py`
installs `CORSMiddleware`; sentinel and relay declared the setting and acted on it
nowhere, and bridge never declared one. A customer restricting it believed they had
narrowed five surfaces and had narrowed one.

The second is the worse shape: a **security** setting that appears to be enforced.
Both are removed rather than implemented — CORS on the MCP ports would protect
nothing (it is a browser restriction, and those ports serve MCP clients), and the
fine-tuning feature was never built.

These tests exist because both defects were found by reading, not by any check.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", "node_modules", ".git", "__pycache__"}


def _dockerfiles() -> list[Path]:
    return [
        p for p in REPO.rglob("Dockerfile*")
        if p.is_file() and not any(part in SKIP_DIRS for part in p.parts)
    ]


def _declared_args() -> set[str]:
    """Every `ARG NAME` across every Dockerfile."""
    names: set[str] = set()
    for p in _dockerfiles():
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)", line)
            if m:
                names.add(m.group(1))
    return names


def _compose_build_args() -> dict[str, str]:
    """{arg name: compose file} for every arg passed under a `build.args:` block.

    Raw-text parsing on purpose: `docker-compose.office.yml` uses the `!override`
    tag, and `yaml.safe_load` RAISES on it — a bare `except: continue` around that
    parse is exactly how an earlier inventory silently skipped a whole file.
    """
    found: dict[str, str] = {}
    for name in ("docker-compose.yml", "docker-compose.office.yml", "docker-compose.test.yml"):
        path = REPO / name
        if not path.is_file():
            continue
        in_args = False
        args_indent = 0
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            indent = len(raw) - len(raw.lstrip())
            if re.match(r"^\s*args:\s*$", raw):
                in_args, args_indent = True, indent
                continue
            if in_args:
                if indent <= args_indent:
                    in_args = False
                else:
                    m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", raw)
                    if m:
                        found[m.group(1)] = name
    return found


def test_every_compose_build_arg_is_declared_by_some_dockerfile() -> None:
    """The check that would have caught CORTEX_INSTALL_FINETUNE_DEPS.

    Docker warns and continues on an unconsumed build arg, so a knob can be passed,
    documented, and set by a customer while reaching nothing.
    """
    passed = _compose_build_args()
    declared = _declared_args()
    assert passed, "parsed no build args at all — this check would be vacuous"
    orphans = {k: v for k, v in passed.items() if k not in declared}
    assert not orphans, (
        f"compose passes build args no Dockerfile declares: {orphans}. Docker warns and "
        f"proceeds, so this reaches nothing. Either declare the ARG and use it, or stop "
        f"passing it — and check whether any doc tells a customer to set it."
    )


def _services_declaring_cors() -> list[str]:
    out = []
    for svc in ("cortex", "bridge", "sentinel", "relay"):
        cfg = REPO / svc / "app" / "config.py"
        if cfg.is_file() and "CORS_ORIGINS" in cfg.read_text(encoding="utf-8"):
            out.append(svc)
    return out


def _services_installing_cors() -> list[str]:
    out = []
    for svc in ("cortex", "bridge", "sentinel", "relay"):
        app = REPO / svc / "app"
        if not app.is_dir():
            continue
        for p in app.rglob("*.py"):
            text = p.read_text(encoding="utf-8", errors="replace")
            code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
            if "CORSMiddleware" in code:
                out.append(svc)
                break
    return out


def test_a_service_declaring_cors_origins_actually_installs_cors() -> None:
    """A security setting that is read and never acted on is worse than none.

    It is not enough that the value is loaded into Settings — sentinel and relay both
    did that. The middleware has to exist, or the operator is restricting nothing.
    """
    declaring = set(_services_declaring_cors())
    installing = set(_services_installing_cors())
    assert installing, "no service installs CORSMiddleware — check is vacuous"
    dead = sorted(declaring - installing)
    assert not dead, (
        f"{dead} declare a CORS_ORIGINS setting but never install CORSMiddleware. "
        f"An operator setting it believes those surfaces are restricted. Either install "
        f"the middleware or remove the setting."
    )


def test_env_example_does_not_claim_cors_covers_every_service() -> None:
    """The specific false claim, pinned so it cannot come back by copy-paste."""
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    idx = text.find("CORS (used by all services)")
    if idx == -1:
        return
    preceding = text[max(0, idx - 300):idx]
    assert "was not" in preceding or "previously" in preceding, (
        ".env.example claims CORS is 'used by all services'. Only cortex-api installs "
        "CORSMiddleware."
    )


@pytest.mark.parametrize("term", ["CORTEX_INSTALL_FINETUNE_DEPS"])
def test_removed_settings_are_not_reintroduced_as_live_config(term: str) -> None:
    """Documented-but-absent features may be DISCUSSED, not offered.

    The prose recording why this was removed necessarily names it, so a bare
    "the string must not appear" check would forbid explaining the fix — a trap this
    repo has hit repeatedly. Assert on live config lines instead.
    """
    for name in (".env.example", "docker-compose.yml"):
        path = REPO / name
        if not path.is_file():
            continue
        for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if line.startswith("#") or term not in line:
                continue
            pytest.fail(
                f"{name}:{i} reintroduces {term} as live config: {line!r}. "
                f"The feature it names does not exist — no route, no import, "
                f"not in any requirements file."
            )
