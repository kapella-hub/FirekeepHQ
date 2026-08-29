"""A slow container must fail its own row, not the whole matrix.

On 2026-08-29 one openSUSE run exceeded the lab's 900s ceiling. subprocess.run
raises TimeoutExpired, run_scenario let it escape, and it reached main() as an
uncaught traceback: the remaining 8 of 14 combinations never ran, and because
the exception fired before the transcript was written the artifact upload found
nothing. A seventeen-minute job reported one stack trace and no evidence.

These tests exist so a timeout stays a FAIL with a reason attached — the shape
report() already knows how to print — rather than an abort.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "installlab"))

import lab  # noqa: E402


@pytest.fixture
def target():
    return lab.TARGETS_BY_NAME["opensuse"]


@pytest.fixture(autouse=True)
def runs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(lab, "RUNS", tmp_path / ".runs")
    return tmp_path / ".runs"


def _timeout(*args, **kwargs):
    raise subprocess.TimeoutExpired(
        cmd=["docker", "run"], timeout=900, output="partial output before the stall\n"
    )


def test_a_timeout_is_a_failed_result_not_an_exception(target, monkeypatch):
    monkeypatch.setattr(lab.subprocess, "run", _timeout)
    result = lab.run_scenario(target, "headless", timeout=900)
    assert isinstance(result, lab.Result)
    assert not result.ok
    assert any("timed out" in f for f in result.failures), result.failures


def test_the_timeout_reason_names_the_ceiling_it_hit(target, monkeypatch):
    monkeypatch.setattr(lab.subprocess, "run", _timeout)
    result = lab.run_scenario(target, "headless", timeout=900)
    assert any("900" in f for f in result.failures), result.failures


def test_the_partial_transcript_still_reaches_disk(target, monkeypatch, runs_dir):
    """The evidence is worth more after a timeout than after a pass."""
    monkeypatch.setattr(lab.subprocess, "run", _timeout)
    result = lab.run_scenario(target, "headless", timeout=900)
    assert result.log.exists(), "no transcript written for a timed-out run"
    assert "partial output before the stall" in result.log.read_text(encoding="utf-8")


def test_a_timeout_still_fails_the_run_overall(target, monkeypatch):
    """report() is what CI reads: one timeout must still exit non-zero."""
    monkeypatch.setattr(lab.subprocess, "run", _timeout)
    timed_out = lab.run_scenario(target, "headless", timeout=900)
    passed = lab.Result("headless", "ubuntu", 0, Path("ubuntu.log"))
    assert lab.report([passed, timed_out]) == 1


def test_a_timeout_does_not_leave_the_container_running(target, monkeypatch):
    """docker run's client dying detaches; --rm never fires and the container
    keeps burning the runner underneath every later target."""
    killed: list[list[str]] = []

    def fake_run(argv, **kwargs):
        if argv[:2] == ["docker", "rm"]:
            killed.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise subprocess.TimeoutExpired(cmd=argv, timeout=900, output="")

    monkeypatch.setattr(lab.subprocess, "run", fake_run)
    lab.run_scenario(target, "headless", timeout=900)
    assert killed, "timed-out container was never force-removed"
    assert any("firekeep-lab" in part for part in killed[0]), killed


def test_the_container_is_named_so_a_timeout_has_something_to_kill(target, monkeypatch):
    """--name is what makes _abandon possible; losing it silently re-opens the leak."""
    seen: list[list[str]] = []

    def capture(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "firekeep: installed into /root\n", "")

    monkeypatch.setattr(lab.subprocess, "run", capture)
    lab.run_scenario(target, "headless", timeout=900)
    argv = seen[0]
    assert "--name" in argv, argv
    name = argv[argv.index("--name") + 1]
    assert name.startswith("firekeep-lab-headless-opensuse-"), name
    # Docker takes flags before the image; a name after it becomes a container argument.
    assert argv.index("--name") < argv.index(target.image), argv


def test_two_runs_of_one_target_never_collide_on_the_name(target, monkeypatch):
    """A lab killed mid-run leaves a container; a fixed name would then fail every
    later run of that target on the collision instead of on its own merits."""
    seen: list[list[str]] = []

    def capture(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "firekeep: installed into /root\n", "")

    monkeypatch.setattr(lab.subprocess, "run", capture)
    lab.run_scenario(target, "headless", timeout=900)
    lab.run_scenario(target, "headless", timeout=900)
    names = [a[a.index("--name") + 1] for a in seen]
    assert names[0] != names[1], names
