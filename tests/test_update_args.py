"""update.sh argument handling on a SOURCE checkout.

The two update shapes take different arguments, and `update.sh` picks between
them by a file: a published, source-free install has `SERVER_BUNDLE.json` and
MUST be given `--to vX.Y.Z` (there is no git history to pull); a source
checkout has no such file and must never be given one (it pulls its branch).

Until this guard existed, the source path parsed no arguments at all except
`--no-backup`, so `bash update.sh --to v1.3.3` was SILENTLY IGNORED — it ran a
plain `git pull` while the operator believed they had pinned a release. That is
reachable from the product's own advice: `firekeep doctor` prints
`bash update.sh --to vY` whenever cortex reports a clean vX.Y.Z, and a source
checkout sitting exactly on a release tag reports exactly that.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from test_deploy_lib import BASH

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(BASH is None, reason="no POSIX bash available")


def _source_checkout(tmp_path: Path) -> Path:
    """A directory update.sh will treat as a source checkout: .env, and
    deliberately NO SERVER_BUNDLE.json."""
    (tmp_path / ".env").write_text("IMAGE_TAG=dev\n", encoding="utf-8")
    return tmp_path


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(REPO / "update.sh"), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(tmp_path),
    )


def test_to_is_rejected_on_a_source_checkout(tmp_path):
    result = _run(_source_checkout(tmp_path), "--to", "v1.3.3")
    assert result.returncode != 0, "--to must not be silently accepted here"
    combined = result.stdout + result.stderr
    assert "--to" in combined
    assert "source checkout" in combined.lower()


def _out(result: subprocess.CompletedProcess) -> str:
    return result.stdout + result.stderr


def test_the_rejection_names_the_command_that_does_work(tmp_path):
    """A hard error that leaves the operator stuck is a worse bug than the
    silent ignore. doctor sent them here with `--to`; the message has to say
    what to run instead."""
    combined = _out(_run(_source_checkout(tmp_path), "--to", "v1.3.3"))
    assert "bash update.sh" in combined


def test_rejection_happens_before_any_side_effect(tmp_path):
    """Fail fast: no git pull, no backup, no compose call. The tmp dir has no
    .git and no docker-compose.yml, so reaching any of those stages would
    surface as their errors rather than the argument error."""
    result = _run(_source_checkout(tmp_path), "--to", "v1.3.3")
    combined = (result.stdout + result.stderr).lower()
    for leaked in ("not a git repository", "docker", "backing up"):
        assert leaked not in combined, f"ran past argument validation: {leaked!r}"


def test_unknown_arguments_are_rejected_too(tmp_path):
    result = _run(_source_checkout(tmp_path), "--definitely-not-a-flag")
    assert result.returncode != 0
    assert "--definitely-not-a-flag" in result.stdout + result.stderr


def test_no_backup_still_works_on_a_source_checkout(tmp_path):
    """The one flag this path has always accepted must keep working -- it must
    get PAST argument validation (it then fails later for lack of a git repo,
    which is what proves validation let it through)."""
    combined = _out(_run(_source_checkout(tmp_path), "--no-backup")).lower()
    assert "unknown" not in combined
    assert "--no-backup" not in combined or "usage" not in combined


def test_bare_invocation_is_unaffected(tmp_path):
    """No arguments is the normal source-checkout update and must not trip the
    new validation."""
    combined = _out(_run(_source_checkout(tmp_path))).lower()
    assert "usage" not in combined
