"""The headroom gate must fire on the real measurement that motivated it.

`scripts/check_container_headroom.py` exists because the stranger-install smoke
test PRINTED cortex-beat at 116.9MiB against a 128MiB limit -- 91% of its cap --
in a step called "Record the real resource high-water mark", and nothing
happened. Recording a number asserts nothing.

The fixture below is that exact `docker stats` output, verbatim from the run.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_container_headroom.py"

# Verbatim from the first green install-smoke run, when cortex-beat was still 128m.
REAL_STATS_BEFORE_FIX = "\n".join([
    "firekeephq-cortex-mcp-1\t65MiB / 256MiB",
    "firekeephq-dashboard-1\t11.21MiB / 64MiB",
    "firekeephq-cortex-worker-1\t124.1MiB / 2GiB",
    "firekeephq-cortex-api-1\t134.1MiB / 512MiB",
    "firekeephq-sentinel-1\t71.45MiB / 256MiB",
    "firekeephq-qdrant-1\t54.48MiB / 512MiB",
    "firekeephq-bridge-1\t69.14MiB / 256MiB",
    "firekeephq-relay-1\t69.5MiB / 256MiB",
    "firekeephq-cortex-beat-1\t116.9MiB / 128MiB",
    "firekeephq-ollama-1\t812MiB / 7.752GiB",
]) + "\n"

# The same measurements against the raised 256m cap.
REAL_STATS_AFTER_FIX = REAL_STATS_BEFORE_FIX.replace(
    "116.9MiB / 128MiB", "116.9MiB / 256MiB")


def _run(stdin: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)], input=stdin, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )


def test_fires_on_the_measurement_that_motivated_it():
    r = _run(REAL_STATS_BEFORE_FIX)
    assert r.returncode == 1
    assert "cortex-beat" in r.stdout
    assert "91." in r.stdout            # the actual percentage, not just "too high"


def test_passes_once_the_cap_was_raised():
    r = _run(REAL_STATS_AFTER_FIX)
    assert r.returncode == 0, r.stdout
    assert "OVER" not in r.stdout


def test_unparseable_input_fails_loud_rather_than_vacuously_passing():
    """The failure mode this whole file guards against is a check that cannot
    fail. If `docker stats` changes format, the gate must go RED, never green."""
    r = _run("some banner text with no numbers\n")
    assert r.returncode == 1
    assert "format changed" in r.stdout


def test_every_container_is_accounted_for():
    """A silently-dropped row is the same vacuous pass in miniature."""
    r = _run(REAL_STATS_AFTER_FIX)
    assert "parsed 10 containers" in r.stdout


@pytest.mark.parametrize("usage,should_fail", [
    ("900MiB / 1GiB", True),      # 87.9% - over, and units differ across the divide
    ("1.5GiB / 2GiB", False),     # 75% - under the 85% ceiling
    ("100MiB / 100MiB", True),    # 100% - exactly at the cap
    ("1MiB / 1GiB", False),       # 0.1% - trivially fine
    ("812MiB / 7.752GiB", False), # the real ollama row: 10.2%, NOT 812 > 7.752
])
def test_unit_handling_across_the_divide(usage, should_fail):
    """Used and limit routinely carry DIFFERENT units (812MiB / 7.752GiB), so a
    naive numeric compare would read 812 > 7.75 and invert the verdict."""
    r = _run(f"svc\t{usage}\n")
    if should_fail:
        assert r.returncode == 1, f"{usage} should have tripped the gate\n{r.stdout}"
    else:
        assert r.returncode == 0, f"{usage} should have passed\n{r.stdout}"
