"""Each service's requirements.lock must be a valid resolution of its requirements.txt.

Why a lock exists at all
------------------------
`cortex/requirements.txt:2` reads `fastapi>=0.115,<1`. A dev box that installed
months ago holds 0.128; a fresh resolve gets 0.140. Between those two FastAPI
changed its route table shape, and three tests passed locally while failing in
CI's resolution. That is the visible symptom. The invisible one is that
`<svc>/Dockerfile` ran `pip install -r requirements.txt`, so two builds of the
SAME COMMIT produced different images — a customer reporting a bug against a git
SHA was not describing a knowable artifact, and the CI CVE gate and the SBOM both
described whatever resolved on the day they ran rather than what shipped.

Why this test and not `uv pip compile --check`
-----------------------------------------------
Two reasons. `--check` does not exist in uv 0.11.7 (it errors with a usage
message, and a `| tail` pipeline will happily report exit 0 for that).

More importantly, regenerate-and-diff is the WRONG SEMANTIC. It fails whenever
any upstream publishes a new release, which is not drift — the lock is not
supposed to track latest, it is supposed to pin one known-good resolution. The
real invariant is narrower and deterministic offline:

    every direct requirement is present in the lock, at a version its
    specifier allows.

That catches the failure that matters — a dependency added to or bumped in
requirements.txt without regenerating — and stays green when upstream moves.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SERVICES = ("cortex", "bridge", "sentinel", "relay")

# Only these four build an image from a requirements file. client/ and symdex/
# are WHEELS installed into a user's virtualenv: pinning a library's transitive
# deps is wrong and would fight the bootstrap's own resolution.

packaging_specifiers = pytest.importorskip(
    "packaging.specifiers", reason="needs packaging for specifier matching"
)
packaging_version = pytest.importorskip("packaging.version")


def _canon(name: str) -> str:
    """PEP 503 normalisation — `pydantic-settings` and `pydantic_settings` are one name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _direct_requirements(service: str) -> dict[str, str]:
    """{canonical name: specifier} from requirements.txt, extras stripped."""
    out: dict[str, str] = {}
    for raw in (REPO / service / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)$", line)
        assert m, f"{service}/requirements.txt: cannot parse {raw!r}"
        out[_canon(m.group(1))] = m.group(3).strip()
    return out


def _locked_versions(service: str) -> dict[str, str]:
    """{canonical name: pinned version} from requirements.lock."""
    out: dict[str, str] = {}
    for raw in (REPO / service / "requirements.lock").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("--hash"):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?==([^\s\\;]+)", line)
        if m:
            out[_canon(m.group(1))] = m.group(3)
    return out


@pytest.mark.parametrize("service", SERVICES)
def test_lock_exists(service: str) -> None:
    lock = REPO / service / "requirements.lock"
    assert lock.is_file(), f"{service} has no requirements.lock — its image is unpinned"
    assert lock.stat().st_size > 0


@pytest.mark.parametrize("service", SERVICES)
def test_every_direct_requirement_is_locked(service: str) -> None:
    """The check that catches a dependency added without regenerating."""
    direct = _direct_requirements(service)
    locked = _locked_versions(service)
    assert direct, f"{service}/requirements.txt parsed to nothing"
    missing = sorted(set(direct) - set(locked))
    assert not missing, (
        f"{service}: {missing} in requirements.txt but absent from requirements.lock. "
        f"Regenerate:\n  uv pip compile {service}/requirements.txt "
        f"--python-platform linux --python-version 3.11 --generate-hashes "
        f"--output-file {service}/requirements.lock"
    )


@pytest.mark.parametrize("service", SERVICES)
def test_locked_versions_satisfy_their_specifiers(service: str) -> None:
    """Catches a bumped floor/ceiling that the lock predates."""
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    locked = _locked_versions(service)
    for name, spec in _direct_requirements(service).items():
        if not spec or name not in locked:
            continue
        got = locked[name]
        assert Version(got) in SpecifierSet(spec), (
            f"{service}: requirements.txt asks for {name}{spec} but the lock pins "
            f"{name}=={got}. Regenerate the lock."
        )


@pytest.mark.parametrize("service", SERVICES)
def test_lock_is_fully_hash_pinned(service: str) -> None:
    """Every entry carries a hash, or pip silently leaves --require-hashes mode.

    pip enters hash-checking mode when ANY requirement has a hash, and then
    demands one for all of them — so a partially-hashed lock does not degrade
    quietly, it fails the build. Asserting it here means that failure surfaces in
    a 2-second test rather than in a Docker build.
    """
    text = (REPO / service / "requirements.lock").read_text(encoding="utf-8")
    pinned = len(re.findall(r"(?m)^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]*\])?==", text))
    hashed = len(re.findall(r"--hash=sha256:", text))
    assert pinned > 0, f"{service}: lock has no pinned packages"
    assert hashed >= pinned, (
        f"{service}: {pinned} pinned packages but only {hashed} hashes — regenerate "
        f"with --generate-hashes"
    )


@pytest.mark.parametrize("service", SERVICES)
def test_dockerfile_installs_the_lock_not_the_txt(service: str) -> None:
    """The lock is inert unless the image actually installs it."""
    text = (REPO / service / "Dockerfile").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "-r requirements.lock" in code, (
        f"{service}/Dockerfile does not install requirements.lock — the lock exists "
        f"but the image is still resolved at build time"
    )
    assert "-r requirements.txt" not in code, (
        f"{service}/Dockerfile still installs requirements.txt; that reintroduces "
        f"build-time resolution alongside the lock"
    )


def test_only_image_services_are_locked() -> None:
    """client/ and symdex/ ship as WHEELS and must NOT be locked.

    Pinning a library's transitive dependencies forces them on every consumer and
    fights the bootstrap's own resolution. Recorded as a test so a future
    well-meaning sweep does not "finish the job".
    """
    for pkg in ("client", "symdex"):
        assert not (REPO / pkg / "requirements.lock").exists(), (
            f"{pkg}/requirements.lock should not exist — it is a wheel, not an image"
        )
