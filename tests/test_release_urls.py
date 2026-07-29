"""Published URLs must name a repository that actually exists.

Why this file exists
--------------------
The client release workflow bakes a distribution base URL into the PUBLISHED
bootstrap scripts, and it bakes it *before* the checksums are computed — so the
URL is not merely documentation, it is part of the hashed artifact that
``firekeep update`` verifies before executing.

That URL said ``kapella-hub.github.io/Firekeep``. The repository is
``kapella-hub/FirekeepHQ``. Thirteen occurrences across the workflow, four docs,
the README and a served agent card all named a repo that has never existed, and
the release path had never been run, so nothing disagreed.

The existing bootstrap tests did not catch it and could not have. They assert a
dist-base is *present* (``assert "--dist-base" in text``) and that an *unset* one
is refused. Both pass just as happily against a URL pointing at nothing. A check
that cannot distinguish a working URL from a broken one is not a check.

What this guards
----------------
1. Every ``kapella-hub.github.io`` / ``github.com/kapella-hub`` URL in the repo
   names ``FirekeepHQ``.
2. The workflow's ``--dist-base`` agrees with the URL the docs tell users to set.
3. The bootstrap placeholder still exists in the repo copies (baking is what
   replaces it; if the placeholder disappears, ``make_release`` fails the release
   rather than shipping an unbaked script — this asserts the mechanism is intact).

It deliberately does NOT make a network call. This must pass in an air-gapped CI
run, and a reachability check would make the suite fail for reasons that have
nothing to do with the code.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: The product source repository (private).
OWNER = "kapella-hub"
REPO = "FirekeepHQ"

#: The PUBLIC artifacts repository. Separate on purpose: Pages cannot serve from a
#: private repo on a non-Enterprise plan, and making the source public would
#: publish the server -- which is the actual product. The client wheel is
#: py3-none-any and contains only firekeep_client*, and the Free Tier is gratis,
#: so gating its download protects nothing. Integrity, not secrecy, is the
#: property that matters there, and SHA256SUMS provides it.
DIST_REPO = "firekeep-dist"

#: Where the bootstrap and updater actually fetch from.
PAGES_BASE = f"https://{OWNER}.github.io/{DIST_REPO}"

#: Every repo name that may legitimately appear after this owner.
VALID_REPOS = (REPO, DIST_REPO)

#: Directories with no bearing on what gets published.
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    "dist", "build", ".mypy_cache", ".ruff_cache", "htmlcov",
}

#: Any URL naming this owner must name one of the VALID repos after it. The
#: negative lookahead is what makes this a real assertion: it matches only the
#: WRONG spellings, so a correct repo never trips it. Listing both repos rather
#: than loosening the pattern keeps a typo'd third name a failure.
_ALT = "|".join(re.escape(r) for r in VALID_REPOS)
WRONG_PAGES = re.compile(rf"{re.escape(OWNER)}\.github\.io/(?!(?:{_ALT})\b)([A-Za-z0-9._-]+)")
WRONG_CLONE = re.compile(rf"github\.com/{re.escape(OWNER)}/(?!(?:{_ALT})\b)([A-Za-z0-9._-]+)")

TEXT_SUFFIXES = {".py", ".sh", ".ps1", ".yml", ".yaml", ".md", ".toml", ".json", ".cfg", ".txt"}


def _tracked_text_files() -> list[Path]:
    out: list[Path] = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix not in TEXT_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


class TestPublishedUrlsNameARealRepo:
    def test_no_pages_url_names_the_wrong_repo(self):
        bad: list[str] = []
        for p in _tracked_text_files():
            if p.resolve() == Path(__file__).resolve():
                continue  # this file quotes the wrong spellings on purpose
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for m in WRONG_PAGES.finditer(text):
                line = text[: m.start()].count("\n") + 1
                bad.append(f"{p.relative_to(ROOT)}:{line} -> {OWNER}.github.io/{m.group(1)}")
        assert not bad, (
            "Pages URL(s) name a repository that does not exist. This URL is baked into the\n"
            "PUBLISHED bootstrap before its checksum is computed, so a wrong value breaks both\n"
            "the download AND `firekeep update`'s verification:\n  " + "\n  ".join(bad)
        )

    def test_no_clone_url_names_the_wrong_repo(self):
        bad: list[str] = []
        for p in _tracked_text_files():
            if p.resolve() == Path(__file__).resolve():
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for m in WRONG_CLONE.finditer(text):
                # `github.com/kapella-hub` with nothing after it is the profile, not a repo.
                if m.group(1) in {"", "settings"}:
                    continue
                line = text[: m.start()].count("\n") + 1
                bad.append(f"{p.relative_to(ROOT)}:{line} -> github.com/{OWNER}/{m.group(1)}")
        assert not bad, (
            "Clone URL(s) name a repository that does not exist:\n  " + "\n  ".join(bad)
        )


class TestTheWorkflowAndTheDocsAgree:
    """A user following the docs must land where CI actually publishes."""

    def test_workflow_bakes_the_documented_base(self):
        wf = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        m = re.search(r'--dist-base\s+"([^"]+)"', wf)
        assert m, "release.yml no longer passes --dist-base; the bootstrap would ship unbaked"
        assert m.group(1).rstrip("/") == PAGES_BASE, (
            f"release.yml bakes {m.group(1)!r} but the published base is {PAGES_BASE!r}"
        )

    def test_the_docs_tell_users_the_same_base(self):
        doc = ROOT / "docs/RELEASE-GITHUB.md"
        if not doc.is_file():
            pytest.skip("docs/RELEASE-GITHUB.md not present")
        text = doc.read_text(encoding="utf-8")
        assert PAGES_BASE in text, (
            f"{doc.name} does not mention {PAGES_BASE}; a user following it would fetch "
            f"from somewhere CI never publishes to"
        )


class TestTheBakingMechanismIsIntact:
    """The placeholder is what `make_release --dist-base` substitutes. If it is gone,
    the published scripts would carry whatever literal happened to be in the repo."""

    @pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
    def test_repo_bootstrap_keeps_its_placeholder(self, script):
        p = ROOT / "client/bootstrap" / script
        text = p.read_text(encoding="utf-8")
        assert "__FIREKEEP_DIST_BASE" in text, (
            f"{script} lost its dist-base placeholder. make_release bakes the real URL into "
            f"this token; without it a raw checkout would silently ship an unbaked bootstrap."
        )

    @pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
    def test_repo_bootstrap_has_no_baked_url(self, script):
        """The REPO copies must stay unbaked, so a raw-checkout run fails loudly with
        nowhere to fetch from rather than silently reaching a stale host."""
        text = (ROOT / "client/bootstrap" / script).read_text(encoding="utf-8")
        assert f"{OWNER}.github.io" not in text, (
            f"{script} carries a baked distribution URL in the repo copy. Only the PUBLISHED "
            f"copy should be baked; a checkout must demand an explicit FIREKEEP_DIST_BASE."
        )
