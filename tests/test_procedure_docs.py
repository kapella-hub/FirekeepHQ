"""Documentation drift guards for Living Procedures.

The shape is `tests/test_recall_synthesis_default.py`'s: a default a customer can
read in `CLAUDE.md` and a default the code actually applies must be the same
number, and nothing but a test keeps them that way. Eleven settings ship here,
all inert until `PROCEDURE_ENABLED=true`, which is exactly the situation in which
a wrong documented default is discovered by a customer rather than by us.

The endpoint and module-map checks are derived from the SOURCE, not from a copy
of the literal strings: adding a route or a module without documenting it must
turn this red, which a hand-written list cannot do.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
# The narrative moved out of the root CLAUDE.md when that file was cut from 264 KB
# to ~16 KB: it is a prompt prefix loaded into every session, and reference material
# does not belong there. The GUARD follows the content rather than being deleted —
# a documented default that disagrees with the code is exactly as wrong in
# docs/guides/ as it was in CLAUDE.md.
ROOT_GUIDE = (REPO / "docs" / "guides" / "living-procedures.md").read_text(encoding="utf-8")
CORTEX_GUIDE = (REPO / "cortex" / "CLAUDE.md").read_text(encoding="utf-8")
PROCEDURES = REPO / "cortex" / "app" / "procedures"

# h3 in the old root guide, h2 as its own document.
SECTION_HEADING = "## Living Procedures (`cortex/app/procedures/`)"


def _code_defaults() -> dict[str, str]:
    """Every `PROCEDURE_*` Settings field and its literal default, via ast.

    Root tests cannot import `app.config` (it is not on the path), which is why
    `test_recall_synthesis_default.py` parses the same file the same way.
    """
    tree = ast.parse((REPO / "cortex" / "app" / "config.py").read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            if name.startswith("PROCEDURE_") and node.value is not None:
                out[name] = str(ast.literal_eval(node.value)).lower()
    return out


def _section() -> str:
    """The Living Procedures section of the root guide, heading to next heading."""
    start = ROOT_GUIDE.find(SECTION_HEADING)
    assert start != -1, (
        f"root CLAUDE.md has no {SECTION_HEADING!r} section — the feature is "
        f"undocumented where every other Cortex subsystem is documented"
    )
    rest = ROOT_GUIDE[start + len(SECTION_HEADING):]
    end = rest.find("\n### ")
    return rest if end == -1 else rest[:end]


def test_the_root_guide_has_a_living_procedures_section() -> None:
    assert len(_section().strip()) > 500, "the section exists but says almost nothing"


def test_every_procedure_setting_exists_in_code() -> None:
    """Guards the guard: if this ever parses nothing, the drift checks below are
    vacuous and would pass against an empty documentation table."""
    found = _code_defaults()
    assert len(found) == 11, f"expected 11 PROCEDURE_* settings, parsed {sorted(found)}"


@pytest.mark.parametrize("name,default", sorted(_code_defaults().items()))
def test_the_root_guide_documents_each_setting_with_the_code_default(
    name: str, default: str
) -> None:
    """A documented default that disagrees with the code is worse than none — a
    customer tunes against it and gets the other number."""
    rows = re.findall(rf"\|\s*`{name}`\s*\|\s*`([^`]*)`\s*\|", _section())
    assert rows, f"{name} is not in the root CLAUDE.md config table"
    for got in rows:
        assert got.strip().lower() == default, (
            f"root CLAUDE.md documents {name}={got!r}, code default is {default!r}"
        )


@pytest.mark.parametrize("invariant", ["I1", "I2", "I3", "I4", "I5", "I6", "I7"])
def test_each_invariant_is_named_in_the_section(invariant: str) -> None:
    """The spec's §5 invariants are the load-bearing part: each exists because
    the naive version is provably wrong, so a reader who does not meet them will
    reimplement the wrong version."""
    assert f"**{invariant}" in _section(), f"{invariant} is not stated in the section"


def test_the_section_states_that_adapter_cannot_identify_the_runtime() -> None:
    """`Adapter` is a transport class and pre_tool hardcodes "shell-hook" on
    every runtime, which is precisely why I2 is not optional. A reader who
    assumes it names the runtime will propose the mitigation that cannot work."""
    section = _section()
    assert "transport class" in section
    assert "shell-hook" in section


def test_the_section_states_the_honest_limits() -> None:
    section = _section()
    for term in ["Tier A", "Tier B", "unobservable", "cold start"]:
        assert term.lower() in section.lower(), f"the section never mentions {term!r}"


def test_the_owm_paragraph_itself_records_the_outcome_signal_finding() -> None:
    """An OWM reader will never look in the Living Procedures section.

    Measured: no production emitter passes `outcome=` to replay except Bridge's
    session lifecycle, so `_failure_rate` is 0.0 and effectively every session
    reads as a success — which means `owm_efficacy` discriminates far less than
    the design intends. That belongs where OWM is documented.
    """
    # Deliberately NOT ROOT_GUIDE: OWM is documented in the memory guide, and the
    # whole point of this test is that the finding must sit where OWM's readers are
    # rather than in the feature that discovered it. Pointing it at the procedures
    # guide would re-create exactly the mis-filing it exists to prevent.
    memory_guide = (REPO / "docs" / "guides" / "memory-and-recall.md").read_text(encoding="utf-8")
    bullets = [
        line for line in memory_guide.splitlines()
        if line.lstrip().startswith("- **Outcome-Weighted Memory")
    ]
    assert bullets, "the OWM bullet is gone from docs/guides/memory-and-recall.md"
    owm = bullets[0]
    for term in ["outcome=", "_failure_rate"]:
        assert term in owm, (
            f"the OWM paragraph does not name {term!r}. The measured degeneracy in "
            f"its own input signal must be recorded where OWM's readers are, not "
            f"only in the feature that discovered it."
        )


def _module_files() -> list[str]:
    return sorted(
        p.name for p in PROCEDURES.glob("*.py") if p.name != "__init__.py"
    )


def test_the_cortex_guide_maps_every_procedures_module() -> None:
    """Derived from the directory, so a new module that nobody documented fails
    here rather than being discovered by the next reader."""
    files = _module_files()
    assert files, "parsed no procedures modules — this check would be vacuous"
    assert "procedures/" in CORTEX_GUIDE, "cortex/CLAUDE.md's module map omits procedures/"
    missing = [f for f in files if f not in CORTEX_GUIDE]
    assert not missing, f"cortex/CLAUDE.md does not name {missing}"


def _routes() -> list[str]:
    """Every route path declared by the procedures router, read from source."""
    text = (PROCEDURES / "api.py").read_text(encoding="utf-8")
    return sorted(set(re.findall(r"@router\.(?:get|post)\(\s*\"([^\"]+)\"", text)))


def test_the_cortex_guide_lists_every_procedures_route() -> None:
    routes = _routes()
    assert len(routes) == 3, f"expected 3 procedures routes, found {routes}"
    for path in routes:
        assert path in CORTEX_GUIDE, f"cortex/CLAUDE.md does not document {path}"


def test_the_cortex_guide_records_the_scope_gate_on_each_route() -> None:
    """The reads gate `memory:read` and the dismiss gates `admin`. Stating this
    matters because the neighbouring skills router gates NOTHING, so a reader
    cannot infer the posture from its surroundings."""
    idx = CORTEX_GUIDE.find("/procedures")
    assert idx != -1
    window = CORTEX_GUIDE[idx:idx + 4000]
    assert "memory:read" in window
    assert "admin" in window
