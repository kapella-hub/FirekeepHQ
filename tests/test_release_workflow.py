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
   ``git push``. Pages being disabled or the site build failing on size both
   leave the job green and the clients fetching nothing.

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


class TestTheE2EBootstrapGateRuns:
    """The gate that exists because 0.1.2 shipped a bootstrap that wiped its own
    venv at the wizard step.

    docs/RELEASE-GITHUB.md mandated running it by hand before every tag. It was
    invoked by NO workflow, excluded from the default suite (`-m 'not e2e'`), and
    skips wholesale on os.name == "nt" (test_e2e_bootstrap.py:23) -- so on the
    author's Windows machine all five tests skip even with uv installed. A gate
    that is manual-only, documented on a platform that cannot run it, and absent
    from CI is not a gate.
    """

    def _test_job_runs(self, wf) -> str:
        return " ".join(s.get("run", "") for s in wf["jobs"]["test"]["steps"])

    def test_the_release_runs_the_e2e_suite(self, wf):
        runs = self._test_job_runs(wf)
        assert "test_e2e_bootstrap.py" in runs, (
            "no release step runs the e2e bootstrap suite -- the only thing that "
            "drives the real install.sh through venv provisioning and the wizard"
        )

    def test_it_selects_the_e2e_marker(self, wf):
        """Without `-m e2e` the suite's own markers exclude every test in it."""
        runs = self._test_job_runs(wf)
        assert "-m e2e" in runs, (
            "the e2e suite is invoked without `-m e2e`, so pytest deselects all of "
            "it and the step passes having run nothing"
        )

    def test_uv_is_installed_first(self, wf):
        """The suite skips itself when uv is absent (test_e2e_bootstrap.py:102).
        Installing it is what turns 5 skips into 5 results."""
        runs = self._test_job_runs(wf)
        assert "astral.sh/uv/install.sh" in runs or "uv --version" in runs, (
            "uv is never installed, so the e2e suite skips itself and the gate "
            "reports success having verified nothing"
        )

    def test_the_gate_runs_on_a_posix_runner(self, wf):
        """It skips entirely on os.name == 'nt', so a windows runner would make the
        whole step a no-op."""
        runner = str(wf["jobs"]["test"].get("runs-on", ""))
        assert "windows" not in runner.lower(), (
            f"the test job runs on {runner!r}; the e2e bootstrap suite skips on "
            f"Windows, so the gate would verify nothing"
        )


class TestTheSignaturePathIsServedAndVerified:
    """Security review (LOW): CI polled only latest/latest.json, so a signed build
    whose .minisig never reached the site — or served stale bytes — went green while
    every updating client silently fell back to the unsigned path. And make_release
    lists install.sh/install.ps1 in <version>/SHA256SUMS (the signature must cover
    the script `firekeep update` executes — updater.bootstrap_sha256 cross-checks
    latest.json against those entries, so the listing cannot be dropped), which
    obliges <version>/ to actually SERVE those files."""

    VERIFY_STEP = "Verify the release is actually served"

    def _verify_run(self, wf) -> str:
        step = next(s for s in wf["jobs"]["release"]["steps"]
                    if (s.get("name") or "") == self.VERIFY_STEP)
        return step.get("run", "")

    def test_the_served_minisig_is_polled_when_signing_ran(self, wf):
        run = self._verify_run(wf)
        assert "SHA256SUMS.minisig" in run, (
            "the verify step never confirms the signature is SERVED — a green "
            "publish with a missing .minisig downgrades every client to the "
            "unsigned warning path"
        )
        assert "dist/SHA256SUMS.minisig" in run, (
            "the check must be conditional on signing having actually run "
            "(unsigned builds publish no .minisig by design)"
        )

    def test_the_served_minisig_is_byte_compared_not_just_fetched(self, wf):
        run = self._verify_run(wf)
        assert "cmp" in run, (
            "fetching a 200 is not verification — a stale or foreign .minisig "
            "must fail the step, so the served bytes must be compared against "
            "the built signature"
        )

    def test_the_bootstraps_are_published_under_the_version_directory(self, publish_step):
        # Join shell line continuations so the versioned cp reads as one command.
        joined = publish_step.replace("\\\n", " ")
        version_cps = [ln for ln in joined.splitlines()
                       if ln.strip().startswith("cp ") and 'gh/${VERSION}/"' in ln]
        assert version_cps, "no versioned copy found in the publish step"
        assert any("dist/install.sh" in ln and "dist/install.ps1" in ln
                   for ln in version_cps), (
            "install.sh/install.ps1 are listed in <version>/SHA256SUMS but were "
            "not served under <version>/ — a sums file describing files its "
            "directory does not carry"
        )


class TestTheWorkflowIsValidToGitHub:
    """PyYAML is more permissive than GitHub's parser, and the gap is not academic.

    A line-range edit deleted `python-version: "3.12"` and left a bare `with:`
    under actions/setup-python. yaml.safe_load parsed it happily as None, every
    local check passed, and GitHub then refused the file outright: two runs with
    ZERO jobs, "This run likely failed because of a workflow file issue", and the
    workflow's own name reported as its path because GitHub could not read the
    `name:` key.

    It also triggered on pushes to main, which the `on:` block does not permit --
    an unparseable workflow does not honour its own triggers.
    """

    def test_no_step_has_an_empty_with(self, wf):
        empty = []
        for job_name, job in wf["jobs"].items():
            for step in job.get("steps", []):
                if "with" in step and not step["with"]:
                    empty.append(f"{job_name}: {step.get('uses') or step.get('name')}")
        assert not empty, (
            "step(s) declare `with:` and provide nothing. GitHub rejects the whole "
            "workflow file for this; PyYAML does not: " + "; ".join(empty)
        )

    def test_every_setup_python_pins_a_version(self, wf):
        """The specific instance that broke, named so a regression is obvious."""
        missing = []
        for job_name, job in wf["jobs"].items():
            for step in job.get("steps", []):
                if "setup-python" in str(step.get("uses", "")):
                    if not (step.get("with") or {}).get("python-version"):
                        missing.append(job_name)
        assert not missing, (
            f"setup-python without python-version in: {missing}. An unpinned "
            f"interpreter is a silent behaviour change; an EMPTY with: is a hard "
            f"workflow-file error."
        )

    def test_no_job_is_empty(self, wf):
        """A job with no steps is the shape GitHub reports as a file issue."""
        for job_name, job in wf["jobs"].items():
            assert job.get("steps"), f"job {job_name!r} has no steps"

    def test_the_workflow_declares_a_name(self, wf):
        """GitHub falls back to the file PATH as the display name when it cannot
        read `name:` -- which is how the breakage was spotted."""
        assert wf.get("name"), "release.yml declares no name:"


class TestMCPRegistryPublication:
    """The public Registry entry must follow, never race, the immutable package."""

    @pytest.fixture()
    def job(self, wf) -> dict:
        assert "mcp-registry" in wf["jobs"]
        return wf["jobs"]["mcp-registry"]

    def test_registry_waits_for_pypi(self, job):
        needs = job.get("needs")
        needs = [needs] if isinstance(needs, str) else (needs or [])
        assert "pypi" in needs

    def test_registry_and_licence_contracts_gate_immutable_uploads(self, wf):
        test_job = wf["jobs"]["test"]
        test_runs = "\n".join(step.get("run", "") for step in test_job["steps"])
        assert "tests/test_mcp_registry_manifest.py" in test_runs
        assert "tests/test_package_licence_consistency.py" in test_runs
        assert "mcp-publisher validate server.json" in test_runs
        assert "sha256sum -c" in test_runs

        pypi_needs = wf["jobs"]["pypi"].get("needs")
        pypi_needs = [pypi_needs] if isinstance(pypi_needs, str) else (pypi_needs or [])
        assert "release" in pypi_needs
        release_needs = wf["jobs"]["release"].get("needs")
        release_needs = [release_needs] if isinstance(release_needs, str) else (release_needs or [])
        assert "test" in release_needs

    def test_registry_uses_oidc_with_minimum_permissions(self, job):
        assert job.get("permissions") == {"contents": "read", "id-token": "write"}
        runs = "\n".join(step.get("run", "") for step in job["steps"])
        assert "login github-oidc" in runs

    def test_registry_publisher_is_pinned_and_verified(self, wf, job):
        assert wf["env"].get("MCP_PUBLISHER_VERSION")
        assert wf["env"].get("MCP_PUBLISHER_LINUX_AMD64_SHA256")
        runs = "\n".join(step.get("run", "") for step in job["steps"])
        assert "sha256sum -c" in runs
        assert "mcp-publisher validate server.json" in runs

    def test_registry_publish_is_separate_and_live_verified(self, job):
        names = [step.get("name", "") for step in job["steps"]]
        assert "Publish to MCP Registry" in names
        assert names[-1] == "Verify official MCP Registry publication"
        verify = job["steps"][-1].get("run", "")
        assert "registry.modelcontextprotocol.io" in verify
        assert 'response["server"] == expected' in verify
