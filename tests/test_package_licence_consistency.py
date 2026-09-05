"""A shipped wheel must not state two different licences.

`symdex/pyproject.toml` sets `license = "LicenseRef-Firekeep-BUSL-1.1"` AND
`readme = "README.md"`. That README's License section said `MIT` — inherited from the
standalone tool the package grew out of. Both statements travel inside the built
wheel's METADATA: `License-Expression` from the first, the long description from the
second. Every developer machine got a package asserting it was simultaneously
source-available and MIT, and the bootstrap installs this package unconditionally.

The metadata field was audited and corrected. The README was not, because nothing
connected the two — `docs/LICENSING.md` does not mention the README at all.

These tests are cheap and the failure mode is legal, not functional: nothing breaks, a
customer simply acquires a colourable claim that a component was offered to them under
an OSI licence.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Every directory that builds a wheel handed to a customer. maildex and hands were
# added after this tuple was last updated — both verified to already comply
# (LicenseRef-Firekeep-BUSL-1.1 in pyproject.toml; no README License section leads
# with an OSI name) and added here now rather than leaving the gap to be found later.
SHIPPED_PACKAGES = ("client", "symdex", "docdex", "maildex", "hands")

EXPECTED_LICENCE = "LicenseRef-Firekeep-BUSL-1.1"

# Bare OSI identifiers. A packaged README may DISCUSS these (the symdex README explains
# which licence it used to claim, and points at NOTICE for dependency licences) — what
# it may not do is lead its License section with one, which is the form a reader and a
# metadata scraper both take as the declaration.
OSI_NAMES = re.compile(
    r"^\s*[`*_]*(MIT|Apache(?:[ -]2\.0)?|BSD(?:[- ]\d)?|GPL(?:v?\d)?|LGPL|MPL|ISC|Unlicense)"
    r"[`*_.]*\s*$",
    re.I,
)


def _pyproject(pkg: str) -> dict:
    return tomllib.loads((REPO / pkg / "pyproject.toml").read_text(encoding="utf-8"))


def _readme_for(pkg: str) -> tuple[Path, str] | None:
    data = _pyproject(pkg).get("project", {})
    name = data.get("readme")
    if not name:
        return None
    if isinstance(name, dict):  # {file = "..."} form
        name = name.get("file")
    if not name:
        return None
    path = REPO / pkg / name
    return path, path.read_text(encoding="utf-8")


def _licence_section(text: str) -> str | None:
    """Body of the `## License` (or `## Licence`) section, up to the next heading."""
    m = re.search(r"^#{1,6}\s+Licen[cs]e\s*$(.*?)(?=^#{1,6}\s|\Z)", text, re.M | re.S)
    return m.group(1) if m else None


@pytest.mark.parametrize("pkg", SHIPPED_PACKAGES)
def test_pyproject_declares_the_source_available_licence(pkg: str) -> None:
    proj = _pyproject(pkg).get("project", {})
    assert proj.get("license") == EXPECTED_LICENCE, (
        f"{pkg}/pyproject.toml declares license={proj.get('license')!r}; "
        f"Firekeep ships one licence and it is {EXPECTED_LICENCE}"
    )


@pytest.mark.parametrize("pkg", SHIPPED_PACKAGES)
def test_readme_licence_section_does_not_contradict_metadata(pkg: str) -> None:
    """The README is the wheel's long description — it ships INSIDE the same METADATA."""
    found = _readme_for(pkg)
    if found is None:
        pytest.skip(f"{pkg} declares no readme, so nothing can contradict its metadata")
    path, text = found
    section = _licence_section(text)
    if section is None:
        return  # no claim made is not a contradiction
    lines = [ln for ln in section.splitlines() if ln.strip()]
    assert lines, f"{path.relative_to(REPO)}: empty License section"
    first = lines[0]
    assert not OSI_NAMES.match(first), (
        f"{path.relative_to(REPO)}: License section opens with {first.strip()!r}, but "
        f"{pkg}/pyproject.toml declares {EXPECTED_LICENCE}. This README is that wheel's "
        f"long description, so both statements ship in one METADATA."
    )
    assert re.search(r"source.available|business source", first, re.I), (
        f"{path.relative_to(REPO)}: License section opens with {first.strip()!r}; it "
        f"should state the software is source-available. Discussion may follow, but the "
        f"first line is the declaration."
    )


def test_root_licence_file_is_busl() -> None:
    """/LICENSE is the document all three wheels point at via `license-files`."""
    text = (REPO / "LICENSE").read_text(encoding="utf-8")
    head = "\n".join(text.splitlines()[:40])
    assert "Business Source License 1.1" in head
    assert "Additional Use Grant:" in head
    assert "Change License: Apache License, Version 2.0" in head


def test_notice_copies_match_and_cover_every_shipped_wheel() -> None:
    notice_paths = [REPO / "NOTICE", *(REPO / pkg / "NOTICE" for pkg in SHIPPED_PACKAGES)]
    contents = [path.read_bytes() for path in notice_paths]
    assert all(content == contents[0] for content in contents[1:])

    text = contents[0].decode("utf-8")
    assert "Client kit (firekeep-client)" in text
    assert "Symdex (firekeep-symdex)" in text
    assert "Docdex (firekeep-docdex)" in text
    assert all(line == line.rstrip() for line in text.splitlines())
    assert not any(
        line.startswith(("<<<<<<< ", ">>>>>>> ")) or line == "======="
        for line in text.splitlines()
    )


def test_client_readme_states_the_current_early_access_team_grant() -> None:
    """PyPI's long description must not resurrect the retired one-person grant."""
    text = (REPO / "client/README.md").read_text(encoding="utf-8")
    section = _licence_section(text)
    assert section is not None
    assert "individuals and teams while Firekeep is in early access" in section
    assert "Teams of more than one person run on a paid" not in section
    assert "production use by one natural person" not in section
