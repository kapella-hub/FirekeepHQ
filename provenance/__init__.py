"""Build provenance — the single source of version identity for every service.

Values are injected at image build time via env vars (see each service's
Dockerfile ARGs and the docker-compose build args). They fall back to
development-safe placeholders when unset, so local runs and tests work without
a build pipeline.

Read from env on EVERY call, never cached at import: services are imported in
varying orders and tests reload this module.
"""
from __future__ import annotations

import os

# Fallback when APP_VERSION is not provided by the build.
_FALLBACK_VERSION = "0.6.0"


def get_version_info(service: str) -> dict[str, str]:
    """Return build provenance for one service as a plain dict.

    Args:
        service: The service's own name, echoed back so a support bundle
            collecting several services' /version output stays unambiguous.
    """
    return {
        "service": service,
        "version": os.environ.get("APP_VERSION") or _FALLBACK_VERSION,
        "git_sha": os.environ.get("GIT_SHA") or "unknown",
        "build_time": os.environ.get("BUILD_TIME") or "unknown",
    }
