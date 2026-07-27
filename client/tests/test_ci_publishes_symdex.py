"""Guards the GitLab pipeline's symdex publishing steps.

SKIPPED IN THIS REPO. `.gitlab-ci.yml` was the employer's pipeline and was
removed in the Firekeep seed; this file came across with the client kit and
read it at IMPORT time, so collection raised FileNotFoundError and took the
whole `test (client)` job down with it — on every commit, before any test ran.

Kept rather than deleted: it encodes real requirements about how the symdex
wheel reaches the bootstrap (build it, upload it under the versioned path, or
`firekeep update` 404s). Whatever replaces that pipeline has to satisfy the
same three assertions, and they are easier to port than to rediscover.
"""

from pathlib import Path

import pytest

_CI_PATH = Path(__file__).resolve().parents[2] / ".gitlab-ci.yml"
pytestmark = pytest.mark.skipif(
    not _CI_PATH.is_file(),
    reason=".gitlab-ci.yml is not part of this repo — see the module docstring",
)
CI = _CI_PATH.read_text() if _CI_PATH.is_file() else ""

def test_ci_builds_symdex_wheel():
    assert "cd symdex && python -m build --wheel --outdir ../dist" in CI

def test_ci_uploads_symdex_wheel():
    # The versioned upload loop must publish the symdex wheel, else the bootstrap 404s.
    assert "dist/firekeep_symdex-*.whl" in CI
