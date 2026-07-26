"""Build provenance for cortex.

Thin adapter over the shared `provenance` module so all four services report
identical fields from identical env vars. cortex's own endpoint contract
predates the shared module and omits the `service` key — preserved exactly,
because cortex/tests/test_version.py asserts on it.
"""

from __future__ import annotations

from provenance import _FALLBACK_VERSION  # noqa: F401  (re-exported for compatibility)
from provenance import get_version_info as _shared_version_info


def get_version_info() -> dict[str, str]:
    """Return build provenance as a plain dict.

    Read from env on each call so tests can monkeypatch and reload cleanly.
    """
    info = _shared_version_info("cortex")
    return {
        "version": info["version"],
        "git_sha": info["git_sha"],
        "build_time": info["build_time"],
    }


# Module-level convenience constants (resolved at import time).
_info = get_version_info()
VERSION = _info["version"]
GIT_SHA = _info["git_sha"]
BUILD_TIME = _info["build_time"]
