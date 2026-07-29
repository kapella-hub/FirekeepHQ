"""The release workflow must be gated, serialised, and self-verifying.

Why this file exists
--------------------
An adversarial audit of a release path that had NEVER BEEN EXECUTED found four
defects here, each of which fails *green* — the workflow reports success while
producing a broken or invisible result:

1. **Nothing was tested before publishing.** `ci.yml` triggers on
   ``push: branches:[main]`` and ``pull_request`` only, so a ``client-v*`` tag
   matched neither and ran zero verification. A tag on an unmerged branch would
   ship a wheel whose own tests never executed.

2. **Nothing confirmed the artifacts were served.** The last action was
   ``git push``. Pages being disabled, the site build failing on size, or the
   repo being private (it is) all leave the job green and the clients fetching
   nothing.

3. **Re-running an older tag rewrote the ``latest/`` pointer backwards**, and
   nothing repairs the fleet afterwards: ``is_newer("0.1.20", "0.1.23")`` is
   False, so every client already on a newer version reports "already up to
   date" forever and auto-update never fires again.

4. **No concurrency group.** Two tags in flight and the loser's push is rejected
   non-fast-forward; its ``<version>/`` directory never publishes, job still green.

These tests are structural — they read the workflow, they do not run it. That is
the point: the workflow cannot be exercised without cutting a real release, which
is precisely how it accumulated four defects. A structural guard is what is
available, so it should at least be a guard that can fail.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github/workflows/release.yml"


@pytest.fixture(scope="module")
def wf() -> dict:
    return yaml.safe_load(WF.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def publish_step(wf) -> str:
    for s in wf["jobs"]["release"]["steps"]:
        if "gh-pages" in (s.get("name") or ""):
            return s.get("run") or ""
    pytest.fail("no gh-pages publish step found in the release job")


class TestNothingShipsUntested:
    def test_a_test_job_exists(self, wf):
        assert "test" in wf["jobs"], (
            "release.yml has no test job. ci.yml does not trigger on tags, so without "
            "one a client-v* tag publishes a wheel that was never tested."
        )

    def test_release_is_gated_on_it(self, wf):
        needs = wf["jobs"]["release"].get("needs")
        needs = [needs] if isinstance(needs, str) else (needs or [])
        assert "test" in needs, "the release job must not start before tests pass"

    def test_the_test_job_actually_runs_tests(self, wf):
        runs = " ".join(s.get("run", "") for s in wf["jobs"]["test"]["steps"])
        assert "pytest" in runs, "the test job runs no pytest — it would gate on nothing"


class TestPublicationIsVerified:
    #: The post-publish step, by name. NOT a substring like "erif" -- that also
    #: matches "Resolve + verify version", which runs long before anything is
    #: published, so the test would pass against a workflow with no verification
    #: at all. Caught by this test failing when it was written that way.
    STEP = "Verify the release is actually served"

    def test_a_post_publish_verification_step_exists(self, wf):
        names = [s.get("name", "") for s in wf["jobs"]["release"]["steps"]]
        assert self.STEP in names, (
            "nothing confirms the release is actually SERVED. git push succeeding is not "
            "evidence: Pages can be disabled, unavailable on a private repo, or fail its "
            "own build on size — all of which leave this workflow green."
        )

    def test_the_verification_fetches_the_live_manifest(self, wf):
        step = next(s for s in wf["jobs"]["release"]["steps"] if (s.get("name") or "") == self.STEP)
        run = step.get("run", "")
        assert "latest/latest.json" in run, "verification must fetch the manifest clients read"
        assert "curl" in run, "verification must make a real network request"
        assert "VERSION" in run, (
            "verification must assert the served version EQUALS this release — fetching "
            "the URL and ignoring its contents would pass against a stale site"
        )

    def test_verification_is_the_final_step(self, wf):
        names = [s.get("name", "") for s in wf["jobs"]["release"]["steps"]]
        assert names[-1] == self.STEP, (
            f"verification must be last; it currently runs before {names[names.index(self.STEP)+1]!r}"
        )


class TestTheLatestPointerCannotMoveBackwards:
    def test_the_latest_copy_is_guarded(self, publish_step):
        assert "publish_latest" in publish_step, (
            "the latest/ copy is unconditional. Re-running an older tag would pin every "
            "client to that older version permanently, with no error anywhere."
        )

    def test_the_guard_reuses_the_shipped_comparison(self, publish_step):
        assert "is_newer" in publish_step, (
            "the guard must use the SHIPPED updater.is_newer, not a reimplementation — "
            "the publish rule and the client's update rule must not be able to drift"
        )

    def test_the_versioned_copy_stays_unconditional(self, publish_step):
        # `firekeep update --to <older>` resolves that version's own wheel and
        # SHA256SUMS from <version>/, so it must publish even on a re-run.
        assert 'gh/${VERSION}/' in publish_step or "gh/$VERSION/" in publish_step, (
            "the versioned directory copy must remain unconditional or --to breaks"
        )


class TestConcurrentReleasesCannotClobberEachOther:
    def test_a_concurrency_group_exists(self, wf):
        assert "concurrency" in wf, (
            "gh-pages is a shared mutable branch and this job read-modify-writes it; "
            "two tags in flight means one push is rejected and never publishes"
        )

    def test_the_group_is_ref_independent(self, wf):
        group = str(wf["concurrency"]["group"])
        assert "github.ref" not in group, (
            "a ref-scoped group serialises nothing here: two tags are two different refs, "
            "so concurrent releases would land in different groups. server-release.yml's "
            "ref-scoped group is correct there and wrong as a precedent for this."
        )

    def test_in_progress_releases_are_not_cancelled(self, wf):
        assert not wf["concurrency"].get("cancel-in-progress", False), (
            "cancelling a release mid-publish can leave gh-pages half-written"
        )


class TestFetchIsNotUnbounded:
    def test_every_gh_pages_fetch_is_shallow(self, publish_step):
        """EVERY fetch, not merely one of them.

        Written first as `assert "--depth=1" in publish_step`, which mutation
        testing exposed as a non-check: the step has two fetches (the initial one
        and the push-retry), so deepening either still left the substring present
        and the guard passed with the defect in place. Checking each occurrence
        is what makes it discriminate.
        """
        fetches = [
            ln.strip() for ln in publish_step.splitlines()
            if "git" in ln and " fetch" in ln
        ]
        assert fetches, "no git fetch found in the publish step"
        bad = [f for f in fetches if "--depth" not in f]
        assert not bad, (
            "gh-pages accumulates ~175MB of byte-identical binaries per release; an "
            "unbounded fetch clones that entire history on every run just to add one "
            "directory. Unbounded fetch(es): " + "; ".join(bad)
        )
