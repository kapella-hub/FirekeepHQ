"""Container image pins must exist, be immutable, and agree with each other.

Python dependencies are locked and hash-pinned (`tests/test_requirements_lock.py`
guards that). This is the other half of the same job one layer down: the images
those locks install *into*. Three things a floating tag costs, in descending
order of how much they matter for software being sold:

1. **The licence of what ships can change with no commit.** `redis:7-alpine` is
   the proven case, not a hypothetical: Redis relicensed at 7.4 from BSD to
   RSALv2/SSPLv1 (source-available, not OSI open source) and that tag moved
   across the boundary on its own. See `docs/THIRD-PARTY-DATASTORES.md`.
2. **One-way data migrations.** `neo4j:5-community` floats across 5.x minors and
   Neo4j store-format upgrades are irreversible. A customer's `docker compose
   pull` could upgrade their database into a state they cannot roll back.
3. **Reproducibility.** Two installs a month apart were two different products.

This guard checks pin *integrity*: that every image reference carries a digest,
that the digest is well-formed, that a pin keeps a human-readable tag beside it,
that references which must agree do, and — the one assertion that reaches past
the pins themselves — that the datastore versions
`docs/THIRD-PARTY-DATASTORES.md` states a licence for are the versions actually
pinned, so a bump cannot leave the licence analysis behind.

It does **not** check licences. Nothing here can tell you what
`sha256:e7723ff…` is licensed under; that is prose in
`docs/THIRD-PARTY-DATASTORES.md`, reviewed by a human. What pinning buys is that
the prose stays true between reviews instead of only on the day it was written,
and what the last assertion buys is that a deliberate bump has to walk past it.

**Raw text, not parsed YAML**, for two independent reasons:

- A commented-out `# image: foo@sha256:…` example must not satisfy the check.
  Parsing hides comments entirely, so a parser cannot even see the difference
  between a real pin and a documented one. This repo has been caught four times
  by prose that satisfied its own guard.
- `docker-compose.office.yml` **cannot be parsed** by `yaml.safe_load` at all —
  it uses the compose `!override` tag and safe_load raises `could not determine a
  constructor for the tag '!override'`. An inventory built by parsing silently
  lost that whole file (and with it `caddy:2-alpine`) behind a bare `except`.
  Reading text is the only approach that sees every file.

Comment handling is deliberately asymmetric: a line whose first non-whitespace
character is `#` is dropped entirely, so this docstring and the WHY-comments
above every pinned `FROM` cannot trip the scan. A *trailing* comment is left
intact, so `image: redis:7-alpine  # TODO pin this` still fails, which is the
whole point.

Every assertion below has a plant test proving it can fail. An assertion nobody
has made fail is decoration — the lesson `tests/test_forbidden_tokens.py` was
written to record, applied here.
"""
import re
from pathlib import Path
from typing import NamedTuple

import pytest

REPO = Path(__file__).resolve().parents[1]

# Never walked. `.venv` is load-bearing, not hygiene: symdex/.venv contains
# tree_sitter_language_pack's `dockerfile.pyd`, a compiled grammar binary that a
# case-insensitive filesystem (Windows, macOS) matches against a `Dockerfile*`
# glob. The name check in _is_dockerfile is a second, independent guard.
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__",
             ".pytest_cache", ".ruff_cache", ".mypy_cache", "site-packages"}


def _is_skipped_path(path: Path) -> bool:
    parts = path.parts
    return (
        any(part in SKIP_DIRS for part in parts)
        or any(parts[i:i + 2] == (".claude", "worktrees") for i in range(len(parts) - 1))
    )

# The files that must be covered. Discovery is a walk, so a NEW compose file or
# Dockerfile is picked up automatically; this set is the floor, asserted as a
# subset so that deleting or renaming one of these fails loudly rather than
# quietly shrinking what the guard looks at.
CRITICAL_FILES = {
    "docker-compose.yml",
    "docker-compose.office.yml",
    "docker-compose.test.yml",
    "Dockerfile",
    "cortex/Dockerfile",
    "bridge/Dockerfile",
    "relay/Dockerfile",
    "sentinel/Dockerfile",
    "docker/Dockerfile.dashboard",
    "docker/Dockerfile.embed",
    "docker/Dockerfile.neo4j",
    "docker/Dockerfile.ollama",
    "docker/Dockerfile.qdrant",
    "docker/Dockerfile.redis",
}

# Counts, so the "these must all match" assertions cannot pass vacuously. An
# `assert len(set(digests)) <= 1` is TRUE for an empty list: rename a service and
# the guard goes green while checking nothing. Bump these deliberately, in the
# same commit that adds or removes the reference.
EXPECTED_OLLAMA_COMPOSE_SERVICES = 2
EXPECTED_PYTHON_BASE_IMAGES = 5

# The four datastore versions `docs/THIRD-PARTY-DATASTORES.md` states a LICENCE
# for, version by version. That document is the product's answer to "what are we
# shipping and under what terms", and its answers are version-specific — Redis
# 7.2.4 is BSD, Redis 7.4.10 is RSALv2/SSPLv1, and the pin is the only thing
# saying which one a customer gets.
#
# Pinning stops a version moving on its own; it does nothing about a version
# moved on purpose by someone who did not know a licence analysis hung off it.
# This constant is the tripwire between the two: bumping a datastore pin fails
# CI until this list AND the document are revisited together. That is the intent
# — it is not a duplication to be "cleaned up" by deriving it from the compose
# file, which would make it agree with any bump automatically and guard nothing.
DOCUMENTED_DATASTORE_PINS = {
    "neo4j:5.26.28-community",
    "redis:7.4.10-alpine",
    "qdrant/qdrant:v1.13.2",
    "ollama/ollama:0.32.4",
}
DATASTORE_LICENCE_DOC = "docs/THIRD-PARTY-DATASTORES.md"

_IMAGE_RE = re.compile(r"^\s*image:\s*(?P<ref>\S+)")
_FROM_RE = re.compile(r"^\s*FROM\s+(?P<rest>\S.*?)\s*$", re.IGNORECASE)
_AS_RE = re.compile(r"\s+AS\s+(?P<stage>\S+)\s*$", re.IGNORECASE)
_FLAG_RE = re.compile(r"^--\S+$")
# Captures whatever follows `@sha256:` — including nothing, and including a
# truncated value — so a malformed digest is REPORTED rather than read as absent.
_DIGEST_RE = re.compile(r"@sha256:(?P<digest>\S*)")
_WELL_FORMED_DIGEST = re.compile(r"[0-9a-f]{64}")
# `${REGISTRY}/` and `$REGISTRY/` prefixes are deliberate in this repo (a
# content-addressed digest resolves identically through a pull-through mirror),
# so they are stripped before comparing repositories rather than treated as part
# of the name.
_REGISTRY_VAR_RE = re.compile(r"^\$(\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)/")


class Ref(NamedTuple):
    """One image reference as it appears in the tree."""

    path: str  # repo-relative, forward slashes
    line: int  # 1-based, for a usable failure message
    raw: str  # exactly as written, e.g. "${REGISTRY}/python:3.11.15-slim@sha256:db3f…"

    def __str__(self) -> str:
        return f"{self.path}:{self.line}  {self.raw}"


def strip_comment_lines(text: str) -> list[str]:
    """Blank out full-line comments, preserving line numbering.

    A line is a comment only if its FIRST non-whitespace character is `#`.
    Trailing comments survive on purpose — `image: redis:7-alpine # pin later`
    is an unpinned image, and a guard that let a comment excuse it would be
    worse than no guard.
    """
    return ["" if line.lstrip().startswith("#") else line for line in text.splitlines()]


def _is_dockerfile(path: Path) -> bool:
    # Case-SENSITIVE string comparison, not a glob: Path.glob is case-insensitive
    # on Windows and macOS, where `dockerfile.pyd` would otherwise match.
    return path.name == "Dockerfile" or path.name.startswith("Dockerfile.")


def _is_compose_file(path: Path) -> bool:
    return path.name.startswith("docker-compose") and path.suffix in (".yml", ".yaml")


def _walk(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _is_skipped_path(path.relative_to(root)):
            continue
        yield path


def find_compose_files(root: Path) -> list[Path]:
    return sorted(p for p in _walk(root) if _is_compose_file(p))


def find_dockerfiles(root: Path) -> list[Path]:
    return sorted(p for p in _walk(root) if _is_dockerfile(p))


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def compose_image_refs(root: Path) -> list[Ref]:
    """Every `image:` value across every compose file under `root`."""
    refs: list[Ref] = []
    for path in find_compose_files(root):
        for lineno, line in enumerate(
            strip_comment_lines(path.read_text(encoding="utf-8", errors="replace")), 1
        ):
            m = _IMAGE_RE.match(line)
            if m:
                refs.append(Ref(_rel(path, root), lineno, m.group("ref").strip("\"'")))
    return refs


def dockerfile_base_refs(root: Path) -> list[Ref]:
    """Every `FROM` base image across every Dockerfile under `root`.

    Skips the two FROM targets that are not pullable images and therefore cannot
    carry a digest: `scratch`, and a re-reference to an earlier build stage
    (`FROM builder`). Neither exists in this tree today — every FROM here is a
    real registry reference — but a multi-stage Dockerfile that adds one must not
    be reported as an unpinned image.
    """
    refs: list[Ref] = []
    for path in find_dockerfiles(root):
        lines = strip_comment_lines(path.read_text(encoding="utf-8", errors="replace"))
        stages = {
            m.group("stage").lower()
            for line in lines
            if (fm := _FROM_RE.match(line)) and (m := _AS_RE.search(fm.group("rest")))
        }
        for lineno, line in enumerate(lines, 1):
            fm = _FROM_RE.match(line)
            if not fm:
                continue
            rest = _AS_RE.sub("", fm.group("rest"))
            # `FROM --platform=$BUILDPLATFORM ref` — flags precede the reference.
            tokens = [t for t in rest.split() if not _FLAG_RE.match(t)]
            if not tokens:
                continue
            ref = tokens[0]
            if ref.lower() == "scratch" or ref.lower() in stages:
                continue
            refs.append(Ref(_rel(path, root), lineno, ref))
    return refs


def all_refs(root: Path) -> list[Ref]:
    return compose_image_refs(root) + dockerfile_base_refs(root)


def split_ref(raw: str) -> tuple[str, str | None, str | None]:
    """`${REGISTRY}/python:3.11.15-slim@sha256:db3f…` -> ("python", "3.11.15-slim", "db3f…").

    Digest is the raw captured text, NOT validated here — validation is a
    separate assertion so a truncated paste reports as malformed rather than
    silently as absent.
    """
    digest = None
    m = _DIGEST_RE.search(raw)
    if m:
        digest = m.group("digest")
        raw = raw[: m.start()]
    raw = _REGISTRY_VAR_RE.sub("", raw)
    tag = None
    colon = raw.rfind(":")
    if colon > raw.rfind("/"):  # a ":" before the last "/" is a registry port
        tag = raw[colon + 1 :]
        raw = raw[:colon]
    return raw, tag, digest


# Images this repo BUILDS AND PUBLISHES itself. They are exempt from the
# digest requirement, and the distinction is not a loophole — it is the whole
# reason the rule exists.
#
# A third-party tag can move under us: `redis:7-alpine` silently crossed a
# LICENCE boundary from BSD to RSALv2/SSPL with no commit here, which is what
# the pinning pass was for. A first-party tag cannot: it names an image built
# from this commit by our own release workflow, and its digest does not exist
# when the compose file is written. Demanding one is not "stricter", it is
# impossible.
#
# The compensating control is real, not a promise: `image:` resolves
# `${IMAGE_TAG}`, and a digest-bearing ref is valid there — a customer who wants
# immutability sets IMAGE_TAG to `v1.2.3@sha256:...`, and server-release.yml
# prints each pushed digest into the job summary so there is something to copy.
FIRST_PARTY_PREFIX = "ghcr.io/kapella-hub/firekeep-"


def _is_first_party(ref: str) -> bool:
    return ref.startswith(FIRST_PARTY_PREFIX)


def unpinned(refs) -> list[Ref]:
    """Third-party references carrying no digest at all."""
    return [
        r for r in refs
        if split_ref(r.raw)[2] is None and not _is_first_party(r.raw)
    ]


def malformed_digests(refs) -> list[Ref]:
    """References whose digest is present but not 64 lowercase hex characters."""
    out = []
    for r in refs:
        digest = split_ref(r.raw)[2]
        if digest is not None and not _WELL_FORMED_DIGEST.fullmatch(digest):
            out.append(r)
    return out


def tagless(refs) -> list[Ref]:
    """References pinned by digest but carrying no tag.

    `image: redis@sha256:e7723ff…` is immutable and therefore passes every other
    assertion in this file, but it is unreviewable: nobody reading that line — in
    a diff, in an audit, in `docs/THIRD-PARTY-DATASTORES.md`'s summary table —
    can tell it is Redis 7.4.10 and therefore RSALv2/SSPLv1 rather than a 7.2.x
    on the old BSD terms. The digest is the half that makes the pin immutable;
    the tag is the half that makes the licence statement checkable by a human.
    A pin is both, which is why this is asserted rather than assumed.
    """
    return [r for r in refs if split_ref(r.raw)[1] is None]


def refs_for_repository(refs, repository: str) -> list[Ref]:
    return [r for r in refs if split_ref(r.raw)[0] == repository]


def digest_disagreements(refs) -> dict[str, set[str]]:
    """Same `repository:tag` written with different digests — one of them is stale.

    A digest is content-addressed: two lines naming the identical tag must resolve
    to the identical bytes, or the tag moved between the two edits and half the
    tree is running something the other half is not.
    """
    seen: dict[str, set[str]] = {}
    for r in refs:
        repository, tag, digest = split_ref(r.raw)
        if digest is None:
            continue
        seen.setdefault(f"{repository}:{tag}", set()).add(digest)
    return {k: v for k, v in seen.items() if len(v) > 1}


# The four datastore repositories `docs/THIRD-PARTY-DATASTORES.md` analyses.
# Infrastructure bases (python, nginx, caddy, ubuntu) are pinned by this guard
# but carry no licence row in that file, so they are out of scope for the
# doc-agreement check below.
DATASTORE_REPOSITORIES = {"neo4j", "redis", "qdrant/qdrant", "ollama/ollama"}

_DOC_REF_RE = re.compile(r"`((?:[a-z0-9]+/)?[a-z0-9]+):([A-Za-z0-9][A-Za-z0-9.\-]*)`")


def documented_datastore_versions(text: str) -> set[str]:
    """`repo:tag` code spans from the summary table of the licence analysis.

    Scoped to that one table deliberately. The per-datastore sections below it
    discuss versions that are explicitly NOT pinned — `redis:7.2.4-alpine` is
    offered there as a remediation option — and reading those as claims about
    what ships would make the check fire on correct prose. Returns an empty set
    if the heading is gone, which the caller asserts against rather than
    treating as agreement.
    """
    start = text.find("## Summary table")
    if start == -1:
        return set()
    rest = text[start + len("## Summary table"):]
    end = min((i for i in (rest.find("\n---"), rest.find("\n## ")) if i != -1),
              default=len(rest))
    return {
        f"{repo}:{tag}"
        for repo, tag in _DOC_REF_RE.findall(rest[:end])
        if repo in DATASTORE_REPOSITORIES
    }


def pinned_datastore_versions(refs) -> set[str]:
    """`repo:tag` for every datastore reference in `refs`, digest stripped.

    Takes refs rather than a root so the plants below can feed it synthetic
    references. Filtered to `DATASTORE_REPOSITORIES`: `nginx` and `caddy` are
    pinned in the compose files too, but carry no licence row, and letting them
    through would make the "pinned but undocumented" direction fire on them.
    """
    out = set()
    for r in refs:
        repository, tag, _ = split_ref(r.raw)
        if repository in DATASTORE_REPOSITORIES and tag:
            out.add(f"{repository}:{tag}")
    return out




def _report(refs) -> str:
    return "\n".join(f"  {r}" for r in refs)


# --------------------------------------------------------------------------
# The real tree. These are the assertions that exist for their own sake.
# --------------------------------------------------------------------------


def test_discovery_covers_every_file_that_must_be_covered():
    """Discovery is a walk, so this set is a floor, not a whitelist. If a file
    here is renamed or deleted, the guard silently stops watching it — which is
    exactly how a check becomes decoration."""
    found = {_rel(p, REPO) for p in find_compose_files(REPO) + find_dockerfiles(REPO)}
    missing = CRITICAL_FILES - found
    assert not missing, f"discovery no longer reaches: {sorted(missing)}"


def test_no_dependency_directories_are_scanned():
    """symdex/.venv ships a `dockerfile.pyd`; a case-insensitive filesystem
    matches it against a Dockerfile glob."""
    scanned = find_compose_files(REPO) + find_dockerfiles(REPO)
    assert not [p for p in scanned if any(part in SKIP_DIRS for part in p.parts)]


def test_every_compose_image_is_digest_pinned():
    bad = unpinned(compose_image_refs(REPO))
    assert not bad, (
        "compose `image:` values with no @sha256: digest — a `docker compose pull` "
        "can move these under the customer with no commit here:\n" + _report(bad)
    )


def test_every_dockerfile_base_image_is_digest_pinned():
    bad = unpinned(dockerfile_base_refs(REPO))
    assert not bad, (
        "Dockerfile `FROM` lines with no @sha256: digest — two builds of the same "
        "git SHA are two different images:\n" + _report(bad)
    )


def test_every_digest_is_well_formed():
    """A truncated or upper-cased digest is not a pin; it is a build failure
    waiting for the next rebuild, and it reads like a pin in review."""
    bad = malformed_digests(all_refs(REPO))
    assert not bad, "digest is not 64 lowercase hex characters:\n" + _report(bad)


def test_every_pin_keeps_a_human_readable_tag():
    """Tag AND digest, never digest alone. A bare `repo@sha256:…` is immutable
    but tells a reviewer nothing about what version — or what LICENCE — it is,
    which is precisely the question `docs/THIRD-PARTY-DATASTORES.md` answers per
    version. Dropping the tag while "cleaning up" a long line would satisfy
    every other assertion here."""
    bad = tagless(all_refs(REPO))
    assert not bad, (
        "pinned by digest with no readable tag — immutable but unreviewable:\n"
        + _report(bad)
    )


def test_documented_datastore_versions_are_the_versions_pinned():
    """The licence analysis and the pins must describe the same software.

    Checked in both directions, because each catches a different mistake:

    - a version named in `DOCUMENTED_DATASTORE_PINS` that is no longer pinned
      means somebody bumped a datastore and the licence analysis now describes
      software nobody runs;
    - a version pinned but not *mentioned* in the document means the bump
      reached the constant and the compose file but never the prose it exists
      to protect.

    This is the assertion that keeps `docs/THIRD-PARTY-DATASTORES.md` from
    quietly becoming a historical record. It does not — cannot — check that the
    LICENCE stated for a version is correct; that is a human re-read, and this
    failing is the prompt for one.

    Three sources must agree, and each is checked against the tree rather than
    against each other in a chain: the pins themselves, the `SUMMARY TABLE` of
    the document (parsed, so the prose a reader actually sees is what is
    verified — not merely a constant claiming to mirror it), and
    `DOCUMENTED_DATASTORE_PINS` (a deliberate speed bump, so a bump cannot be
    green without a human editing a test file and being made to think).
    """
    doc = (REPO / DATASTORE_LICENCE_DOC).read_text(encoding="utf-8")
    pinned = pinned_datastore_versions(all_refs(REPO))

    # 1. The parsed summary table vs the tree, both directions. This is the one
    #    that verifies the document itself rather than a constant about it.
    documented = documented_datastore_versions(doc)
    assert documented, (
        f"no datastore versions parsed out of {DATASTORE_LICENCE_DOC}'s summary "
        "table — the heading moved, and an empty set would otherwise agree with "
        "anything"
    )
    stale = documented - pinned
    assert not stale, (
        f"{DATASTORE_LICENCE_DOC}'s summary table names versions the tree does "
        f"not pin: {sorted(stale)}\n"
        f"currently pinned: {sorted(pinned)}\n"
        "A pin was bumped without revisiting the licence row, so the documented "
        "licence may describe bytes that no longer ship."
    )
    undocumented = pinned - documented
    assert not undocumented, (
        f"pinned but absent from {DATASTORE_LICENCE_DOC}'s summary table, so "
        f"nothing states their licence: {sorted(undocumented)}"
    )

    # 2. The explicit expectation, as a review tripwire.
    no_longer_pinned = DOCUMENTED_DATASTORE_PINS - pinned
    unmentioned = {p for p in DOCUMENTED_DATASTORE_PINS if p not in doc}
    assert not no_longer_pinned, (
        f"{DATASTORE_LICENCE_DOC} states a licence for versions that are no longer "
        f"pinned: {sorted(no_longer_pinned)}\n"
        f"currently pinned: {sorted(pinned)}\n"
        "Re-read that document's summary table — the licence may have changed with "
        "the version — then update DOCUMENTED_DATASTORE_PINS."
    )
    assert not unmentioned, (
        f"pinned and expected, but not named anywhere in {DATASTORE_LICENCE_DOC}: "
        f"{sorted(unmentioned)}"
    )


def test_the_two_ollama_compose_services_pin_the_same_digest():
    """`ollama` and `ollama-pull` are one runtime and the client that populates
    its model store. Split them across versions and the pull runs against a
    daemon it was not built for.

    Scoped to compose deliberately: `docker/Dockerfile.ollama` and
    `docker/Dockerfile.embed` pin `ollama/ollama:0.32.0`, an intentionally
    different (office-only, chunked-image) version. A repo-wide "all ollama refs
    match" assertion would be wrong.
    """
    refs = refs_for_repository(compose_image_refs(REPO), "ollama/ollama")
    assert len(refs) == EXPECTED_OLLAMA_COMPOSE_SERVICES, (
        f"expected {EXPECTED_OLLAMA_COMPOSE_SERVICES} ollama compose services, "
        f"found {len(refs)} — update EXPECTED_OLLAMA_COMPOSE_SERVICES deliberately, "
        "do not let this assertion go vacuous:\n" + _report(refs)
    )
    digests = {split_ref(r.raw)[2] for r in refs}
    assert len(digests) == 1, "ollama services pin different digests:\n" + _report(refs)


def test_all_five_python_base_images_pin_the_same_digest():
    """A stack whose services sit on different Python patch releases is a
    debugging trap nobody thinks to suspect."""
    refs = refs_for_repository(dockerfile_base_refs(REPO), "python")
    assert len(refs) == EXPECTED_PYTHON_BASE_IMAGES, (
        f"expected {EXPECTED_PYTHON_BASE_IMAGES} python base images, found "
        f"{len(refs)} — update EXPECTED_PYTHON_BASE_IMAGES deliberately:\n"
        + _report(refs)
    )
    # The tag too, not only the digest: a matching digest with a mismatched tag
    # would mean one line is lying about what it runs.
    tags = {split_ref(r.raw)[1] for r in refs}
    assert len(tags) == 1, "python base images name different tags:\n" + _report(refs)
    assert next(iter(tags)).startswith("3.11"), (
        "python base image is no longer on the 3.11 line the locks are compiled "
        f"for (`--python-version 3.11`): {sorted(tags)}"
    )
    digests = {split_ref(r.raw)[2] for r in refs}
    assert len(digests) == 1, "python base images pin different digests:\n" + _report(refs)




def test_identical_tags_carry_identical_digests():
    """Covers every image the two assertions above do not name explicitly —
    redis, neo4j, qdrant and ubuntu each appear in both a compose file and a
    Dockerfile, and a partial re-pin would leave them disagreeing."""
    bad = digest_disagreements(all_refs(REPO))
    assert not bad, "same tag pinned to different digests:\n" + "\n".join(
        f"  {tag} -> {sorted(digests)}" for tag, digests in sorted(bad.items())
    )


# --------------------------------------------------------------------------
# Plants. Every assertion above must be demonstrably able to fail.
# --------------------------------------------------------------------------


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_plant_discovery_gap_is_caught(tmp_path):
    """The floor assertion needs a plant like everything else.

    `test_discovery_covers_every_file_that_must_be_covered` is the guard's own
    floor: if discovery stops reaching a file, every other assertion here keeps
    passing while watching less. That makes it the one assertion whose silent
    failure is invisible — so it is the last one that should go unplanted.

    A tree containing only SOME of CRITICAL_FILES must be reported as a gap.
    """
    _write(
        tmp_path,
        "docker-compose.yml",
        "\n".join(["services:", "  a:", "    image: x@sha256:" + "a" * 64, ""]),
    )
    found = {_rel(pp, tmp_path) for pp in find_compose_files(tmp_path) + find_dockerfiles(tmp_path)}
    missing = CRITICAL_FILES - found
    assert missing, "a tree missing every Dockerfile was not reported as a discovery gap"
    assert "docker/Dockerfile.redis" in missing


def test_plant_discovery_gap_check_is_not_vacuous(tmp_path):
    """...and the same computation returns EMPTY for the real tree.

    Without this, the assertion above would also pass if CRITICAL_FILES were
    emptied — `set() - found` is falsy, and a guard whose floor is the empty set
    guards nothing.
    """
    assert CRITICAL_FILES, "CRITICAL_FILES is empty — the floor assertion is vacuous"
    found = {_rel(pp, REPO) for pp in find_compose_files(REPO) + find_dockerfiles(REPO)}
    assert not (CRITICAL_FILES - found)


def test_plant_compose_image_without_a_digest_is_caught(tmp_path):
    _write(tmp_path, "docker-compose.yml", "services:\n  cache:\n    image: redis:7-alpine\n")
    bad = unpinned(compose_image_refs(tmp_path))
    assert [r.raw for r in bad] == ["redis:7-alpine"]


def test_plant_from_without_a_digest_is_caught(tmp_path):
    _write(tmp_path, "Dockerfile", "ARG REGISTRY=docker.io/library\nFROM ${REGISTRY}/python:3.11-slim\n")
    bad = unpinned(dockerfile_base_refs(tmp_path))
    assert [r.raw for r in bad] == ["${REGISTRY}/python:3.11-slim"]
    assert bad[0].line == 2, "must report the real line number to be usable"


def test_plant_truncated_digest_is_caught(tmp_path):
    """The nastiest plant: it LOOKS pinned in review. `unpinned` must not
    excuse it and `malformed_digests` must name it."""
    _write(tmp_path, "docker-compose.yml", "    image: redis:7.4.10-alpine@sha256:e7723ff7\n")
    refs = compose_image_refs(tmp_path)
    assert unpinned(refs) == [], "a truncated digest is present, just wrong"
    assert len(malformed_digests(refs)) == 1


def test_plant_uppercase_digest_is_caught(tmp_path):
    _write(tmp_path, "Dockerfile", "FROM redis:7.4.10-alpine@sha256:" + "E7723FF7" * 8 + "\n")
    assert len(malformed_digests(dockerfile_base_refs(tmp_path))) == 1


def test_plant_bare_digest_without_a_tag_is_caught(tmp_path):
    """Plant: the tag deleted, the digest kept. It is genuinely immutable, so
    `unpinned` and `malformed_digests` both correctly pass it — only the tag
    assertion fires. That is this plant's whole point."""
    _write(tmp_path, "docker-compose.yml", "    image: redis@sha256:" + "a" * 64 + "\n")
    refs = compose_image_refs(tmp_path)
    assert unpinned(refs) == [], "it IS pinned; the other assertions cannot catch it"
    assert malformed_digests(refs) == [], "the digest is well-formed too"
    assert [r.raw for r in tagless(refs)] == ["redis@sha256:" + "a" * 64]


def test_a_tagged_pin_is_not_a_false_positive(tmp_path):
    """The other half: the forms this repo actually uses must not fire."""
    _write(
        tmp_path,
        "docker-compose.yml",
        "    image: redis:7.4.10-alpine@sha256:" + "a" * 64 + "\n",
    )
    _write(
        tmp_path,
        "Dockerfile",
        "FROM ${REGISTRY}/python:3.11.15-slim@sha256:" + "a" * 64 + "\n",
    )
    assert tagless(all_refs(tmp_path)) == []


def test_plant_divergent_ollama_digests_are_caught(tmp_path):
    _write(
        tmp_path,
        "docker-compose.yml",
        "    image: ollama/ollama:0.32.4@sha256:" + "a" * 64 + "\n"
        "    image: ollama/ollama:0.32.4@sha256:" + "b" * 64 + "\n",
    )
    refs = refs_for_repository(compose_image_refs(tmp_path), "ollama/ollama")
    assert len(refs) == EXPECTED_OLLAMA_COMPOSE_SERVICES
    assert len({split_ref(r.raw)[2] for r in refs}) == 2, "divergence not detected"


def test_plant_one_missing_ollama_service_is_caught(tmp_path):
    """The count assertion's reason to exist. With only one service the
    same-digest check passes trivially — `len(set(digests)) == 1` is true of any
    single element — so without the count the guard would be green while
    verifying nothing."""
    _write(tmp_path, "docker-compose.yml", "    image: ollama/ollama:0.32.4@sha256:" + "a" * 64 + "\n")
    refs = refs_for_repository(compose_image_refs(tmp_path), "ollama/ollama")
    assert len({split_ref(r.raw)[2] for r in refs}) == 1, "same-digest check passes vacuously"
    assert len(refs) != EXPECTED_OLLAMA_COMPOSE_SERVICES, "only the count catches this"


def test_plant_divergent_python_digests_are_caught(tmp_path):
    for i, digest in enumerate(["a" * 64] * 4 + ["b" * 64]):
        _write(tmp_path, f"svc{i}/Dockerfile", f"FROM ${{REGISTRY}}/python:3.11.15-slim@sha256:{digest}\n")
    refs = refs_for_repository(dockerfile_base_refs(tmp_path), "python")
    assert len(refs) == EXPECTED_PYTHON_BASE_IMAGES
    assert len({split_ref(r.raw)[2] for r in refs}) == 2, "divergence not detected"


def test_plant_one_missing_python_reference_is_caught(tmp_path):
    """Same vacuity trap as the ollama count: four matching references pass the
    same-digest check while the fifth service is silently unwatched."""
    for i in range(4):
        _write(tmp_path, f"svc{i}/Dockerfile", "FROM python:3.11.15-slim@sha256:" + "a" * 64 + "\n")
    refs = refs_for_repository(dockerfile_base_refs(tmp_path), "python")
    assert len({split_ref(r.raw)[2] for r in refs}) == 1, "same-digest check passes vacuously"
    assert len(refs) != EXPECTED_PYTHON_BASE_IMAGES, "only the count catches this"


def test_plant_mismatched_python_tag_with_matching_digest_is_caught(tmp_path):
    for i, tag in enumerate(["3.11.15-slim"] * 4 + ["3.12-slim"]):
        _write(tmp_path, f"svc{i}/Dockerfile", f"FROM python:{tag}@sha256:" + "a" * 64 + "\n")
    refs = refs_for_repository(dockerfile_base_refs(tmp_path), "python")
    assert len({split_ref(r.raw)[2] for r in refs}) == 1, "digests agree, so only the tag check fires"
    assert len({split_ref(r.raw)[1] for r in refs}) == 2


_DOC_STUB = """# Third-Party Datastores

## Summary table

| Datastore | Version pinned | Licence |
|---|---|---|
| Redis | **7.4.10** — `redis:7.4.10-alpine` (`sha256:e7723ff7…`) | RSALv2 or SSPLv1 |

---

## Redis

Remediation option: pin `redis:7.2.4-alpine`, the last BSD-licensed release.
"""


def test_plant_pin_bumped_without_updating_the_licence_table_is_caught():
    """The case this exists for, and the dangerous direction: the bytes move to
    a version the table does not describe, so the doc keeps asserting the OLD
    licence for the NEW software."""
    documented = documented_datastore_versions(_DOC_STUB)
    bumped = pinned_datastore_versions(
        [Ref("docker-compose.yml", 1, "redis:7.6.0-alpine@sha256:" + "a" * 64)]
    )
    assert documented == {"redis:7.4.10-alpine"}
    assert documented - bumped == {"redis:7.4.10-alpine"}, "stale doc not detected"


def test_plant_a_newly_pinned_datastore_with_no_licence_row_is_caught():
    """The other direction: something ships whose licence nothing states."""
    documented = documented_datastore_versions(_DOC_STUB)
    pinned = pinned_datastore_versions([
        Ref("docker-compose.yml", 1, "redis:7.4.10-alpine@sha256:" + "a" * 64),
        Ref("docker-compose.yml", 2, "neo4j:5.26.28-community@sha256:" + "b" * 64),
    ])
    assert pinned - documented == {"neo4j:5.26.28-community"}


def test_remediation_options_outside_the_table_are_not_read_as_pins():
    """`redis:7.2.4-alpine` appears in the Redis section as an option that was
    NOT taken. Reading it as a claim about what ships would fire on correct
    prose, and a guard that fires on correct prose gets deleted."""
    assert "redis:7.2.4-alpine" in _DOC_STUB, "the decoy must be present"
    assert documented_datastore_versions(_DOC_STUB) == {"redis:7.4.10-alpine"}


def test_a_renamed_summary_table_heading_does_not_pass_vacuously():
    """If the heading moves, parsing returns nothing — and "no documented
    versions" must read as a failure, not as agreement."""
    assert documented_datastore_versions(_DOC_STUB.replace("## Summary table", "## Table")) == set()


def test_infrastructure_bases_are_not_expected_in_the_datastore_table():
    """python/nginx/caddy/ubuntu are pinned and guarded, but carry no licence
    row in that file — demanding one would be a false positive."""
    pinned = pinned_datastore_versions([
        Ref("Dockerfile", 1, "${REGISTRY}/python:3.11.15-slim@sha256:" + "a" * 64),
        Ref("docker/Dockerfile.dashboard", 1, "nginx:1.31.3-alpine@sha256:" + "b" * 64),
    ])
    assert pinned == set()


def test_plant_same_tag_different_digests_across_files_is_caught(tmp_path):
    _write(tmp_path, "docker-compose.yml", "    image: redis:7.4.10-alpine@sha256:" + "a" * 64 + "\n")
    _write(tmp_path, "docker/Dockerfile.redis", "FROM redis:7.4.10-alpine@sha256:" + "b" * 64 + "\n")
    assert digest_disagreements(all_refs(tmp_path)) == {"redis:7.4.10-alpine": {"a" * 64, "b" * 64}}


def test_plant_commented_pin_does_not_rescue_an_unpinned_line(tmp_path):
    """The trap this repo has fallen into four times: explanatory prose that
    satisfies the very check it explains."""
    _write(
        tmp_path,
        "docker-compose.yml",
        "  cache:\n"
        "    # Pin like this: image: redis:7.4.10-alpine@sha256:" + "a" * 64 + "\n"
        "    image: redis:7-alpine\n",
    )
    bad = unpinned(compose_image_refs(tmp_path))
    assert [r.raw for r in bad] == ["redis:7-alpine"], "the comment must not rescue line 3"
    assert bad[0].line == 3


def test_plant_trailing_comment_does_not_rescue_an_unpinned_line(tmp_path):
    _write(tmp_path, "docker-compose.yml", "    image: redis:7-alpine  # TODO: pin this\n")
    assert len(unpinned(compose_image_refs(tmp_path))) == 1


def test_a_commented_out_unpinned_example_is_not_a_false_positive(tmp_path):
    """The other half. A guard that fires on this file's own documentation is a
    guard people learn to route around."""
    _write(
        tmp_path,
        "docker-compose.yml",
        "# Never do this — `redis:7-alpine` floats and crossed a licence boundary:\n"
        "#    image: redis:7-alpine\n"
        "    image: redis:7.4.10-alpine@sha256:" + "a" * 64 + "\n",
    )
    refs = compose_image_refs(tmp_path)
    assert len(refs) == 1, "a commented example must not be collected at all"
    assert unpinned(refs) == []


def test_a_build_stage_reference_is_not_reported_as_unpinned(tmp_path):
    """`FROM chunker` names an earlier stage, not a registry image. It cannot
    carry a digest and must not be demanded to."""
    _write(
        tmp_path,
        "Dockerfile",
        "FROM ubuntu:24.04@sha256:" + "a" * 64 + " AS chunker\n"
        "FROM chunker\n",
    )
    assert unpinned(dockerfile_base_refs(tmp_path)) == []
    assert len(dockerfile_base_refs(tmp_path)) == 1


def test_scratch_is_not_reported_as_unpinned(tmp_path):
    _write(tmp_path, "Dockerfile", "FROM scratch\n")
    assert dockerfile_base_refs(tmp_path) == []


def test_platform_flag_and_as_clause_are_parsed_off_the_reference(tmp_path):
    _write(
        tmp_path,
        "Dockerfile",
        "FROM --platform=$BUILDPLATFORM ${REGISTRY}/ubuntu:24.04@sha256:" + "a" * 64 + " AS chunker\n",
    )
    refs = dockerfile_base_refs(tmp_path)
    assert unpinned(refs) == []
    assert split_ref(refs[0].raw) == ("ubuntu", "24.04", "a" * 64)


def test_venv_dockerfiles_are_not_discovered(tmp_path):
    """`dockerfile.pyd` under symdex/.venv is a compiled tree-sitter grammar; a
    case-insensitive filesystem matches it against a Dockerfile glob."""
    _write(tmp_path, "symdex/.venv/Lib/site-packages/bindings/dockerfile.pyd", "FROM nope\n")
    _write(tmp_path, "Dockerfile", "FROM scratch\n")
    assert [p.name for p in find_dockerfiles(tmp_path)] == ["Dockerfile"]


def test_nested_agent_worktree_dockerfiles_are_not_discovered(tmp_path):
    _write(tmp_path, ".claude/worktrees/feature/Dockerfile", "FROM scratch\n")
    _write(tmp_path, "Dockerfile", "FROM scratch\n")
    assert [p.name for p in find_dockerfiles(tmp_path)] == ["Dockerfile"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("redis:7.4.10-alpine@sha256:" + "a" * 64, ("redis", "7.4.10-alpine", "a" * 64)),
        ("${REGISTRY}/python:3.11.15-slim", ("python", "3.11.15-slim", None)),
        ("$REGISTRY/ollama/ollama:0.32.0", ("ollama/ollama", "0.32.0", None)),
        ("registry.example:5000/neo4j:5-community", ("registry.example:5000/neo4j", "5-community", None)),
        ("ubuntu", ("ubuntu", None, None)),
    ],
)
def test_split_ref(raw, expected):
    assert split_ref(raw) == expected


def test_the_first_party_exemption_is_narrow():
    """The exemption must not become a way to skip pinning anything else.

    It keys off our own registry path. A third-party image cannot acquire it,
    and if someone ever publishes under a different path the exemption stops
    applying to them rather than silently widening.
    """
    for bad in ("redis:7-alpine", "ghcr.io/someone-else/firekeep-cortex:v1",
                "docker.io/kapella-hub/firekeep-cortex:v1",
                "ghcr.io/kapella-hub/other-thing:v1"):
        assert not _is_first_party(bad), f"exemption wrongly covers {bad}"
    assert _is_first_party("ghcr.io/kapella-hub/firekeep-cortex:${IMAGE_TAG:-dev}")


def test_first_party_images_are_actually_present():
    """...and the exemption is not covering an empty set.

    If the published images were removed from compose, `unpinned` would return
    nothing for them and every pinning assertion would pass while the product
    had no distribution path at all.
    """
    refs = [r.raw for r in compose_image_refs(REPO)]
    first_party = [r for r in refs if _is_first_party(r)]
    assert len(first_party) >= 7, (
        f"expected the 7 built services to name published images, found "
        f"{len(first_party)} — see tests/test_server_release.py"
    )
