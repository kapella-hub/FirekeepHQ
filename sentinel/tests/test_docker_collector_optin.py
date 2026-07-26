"""The Docker socket is not reachable from Sentinel unless someone opts in.

Audit finding Major 10: `/var/run/docker.sock` was bind-mounted read-WRITE into a
service whose HTTP port is published on 0.0.0.0 and unauthenticated by default. A
process that can reach the Docker API can `POST /containers/create` with a host bind
mount, so that mount is root on the host. `:ro` does not change this — it restricts the
socket file, not the API served over it — and the mount did not carry `:ro` anyway,
while `docs/DEPLOYMENT.md` claimed it did.

The design's answer to this finding used to be "we are deleting Sentinel." Sentinel is
kept (owner decision, 2026-07-26), so the finding needed a real fix: the collector is
opt-in, and the default compose does not mount the socket at all.

The collector makes exactly ONE call, `GET /containers/json`. Nothing else in the
process touches the socket — `handle_get_environment` reconstructs container states from
the Redis event stream — so gating the collector removes the last reason to mount it.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[2]



def _compose_sentinel_text() -> str:
    """The raw `sentinel:` service block, comments included.

    Read as TEXT, not parsed YAML: a parser would normalise away the commented-out
    mounts, and half of what this test asserts is that the opt-in instructions are
    still there for someone who wants the collector back.
    """
    text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    m = re.search(r"^  sentinel:\n(.*?)(?=^  \S)", text, re.M | re.S)
    assert m, "could not locate the sentinel service block"
    return m.group(0)


def _active_lines(block: str) -> str:
    """Non-comment lines only — a commented mount is documentation, not a mount."""
    return "\n".join(ln for ln in block.splitlines() if not ln.lstrip().startswith("#"))


@pytest.mark.asyncio
async def test_docker_collector_not_started_by_default() -> None:
    from app.config import Settings

    assert Settings().DOCKER_COLLECTOR_ENABLED is False, (
        "the docker collector must ship OFF — enabling it requires a host-root-"
        "equivalent socket mount"
    )


@pytest.mark.asyncio
async def test_ensure_collectors_skips_docker_when_disabled() -> None:
    import app.mcp_server as srv

    started: list[str] = []

    def _capture(coro):
        started.append(getattr(coro, "cr_code", None).co_name if hasattr(coro, "cr_code") else "?")
        coro.close()  # never actually run the loop
        return MagicMock()

    settings = MagicMock(DOCKER_COLLECTOR_ENABLED=False)
    srv._collectors_started = False
    with patch.object(srv, "get_settings", return_value=settings), \
         patch.object(srv, "get_redis", AsyncMock(return_value=MagicMock())), \
         patch.object(srv.asyncio, "create_task", side_effect=_capture):
        await srv._ensure_collectors()

    assert "run_docker_collector" not in started, started
    assert "run_git_collector" in started, started
    assert "run_file_collector" in started, started


@pytest.mark.asyncio
async def test_ensure_collectors_starts_docker_when_enabled() -> None:
    """The opt-in must actually work, or this is a removal wearing a flag's clothes."""
    import app.mcp_server as srv

    started: list[str] = []

    def _capture(coro):
        started.append(getattr(coro, "cr_code", None).co_name if hasattr(coro, "cr_code") else "?")
        coro.close()
        return MagicMock()

    settings = MagicMock(DOCKER_COLLECTOR_ENABLED=True)
    srv._collectors_started = False
    with patch.object(srv, "get_settings", return_value=settings), \
         patch.object(srv, "get_redis", AsyncMock(return_value=MagicMock())), \
         patch.object(srv.asyncio, "create_task", side_effect=_capture):
        await srv._ensure_collectors()

    assert "run_docker_collector" in started, started


@pytest.mark.asyncio
async def test_disabled_collector_is_omitted_not_reported_unhealthy(redis) -> None:
    """Omitted, never False.

    The briefing's `_environment_summary` renders any falsey collector entry as
    "Collector(s) degraded". Reporting a deliberate opt-out as False would put a
    permanent fake fault into every agent's session briefing — a warning that is always
    on is a warning nobody reads.
    """
    import app.mcp_server as srv

    with patch.object(srv, "get_settings", return_value=MagicMock(DOCKER_COLLECTOR_ENABLED=False)):
        health = await srv.handle_get_environment(redis)
    assert "docker" not in health["collectors"], health["collectors"]
    assert set(health["collectors"]) == {"git", "files"}

    with patch.object(srv, "get_settings", return_value=MagicMock(DOCKER_COLLECTOR_ENABLED=True)):
        health = await srv.handle_get_environment(redis)
    assert "docker" in health["collectors"]


def test_compose_does_not_mount_the_docker_socket() -> None:
    active = _active_lines(_compose_sentinel_text())
    assert "/var/run/docker.sock" not in active, (
        "the docker socket is mounted into sentinel again — that grant is root on the "
        "host, and the collector that needed it is default-off"
    )


def test_compose_does_not_mount_the_repo_root() -> None:
    """`./:/watch:ro` put .env — NEO4J_PASSWORD, VAULT_KEY, minted API keys — inside a
    container with a published, unauthenticated HTTP surface. The git and file
    collectors read their watch list from Redis and NS_WATCH_PATHS, both empty by
    default, so it was never read either."""
    active = _active_lines(_compose_sentinel_text())
    assert not re.search(r"^\s*-\s*\./:", active, re.M), (
        "the repository root is bind-mounted into sentinel again — that exposes .env"
    )


def test_compose_keeps_the_opt_in_instructions() -> None:
    """Removing a capability without saying how to get it back is a silent regression."""
    block = _compose_sentinel_text()
    assert "NS_DOCKER_COLLECTOR_ENABLED" in block
    assert "/var/run/docker.sock" in block, "the commented opt-in recipe is gone"
    assert "NS_WATCH_PATHS" in block


def test_deployment_doc_no_longer_calls_the_socket_read_only() -> None:
    """The old text said "This is read-only but grants visibility into all containers."

    Both halves were false, and a false reassurance in the security section is worse
    than no text — it is what a reviewer reads instead of looking.
    """
    text = (REPO / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "not mounted by default" in text, "the doc no longer states the current default"
    # The sentence may appear ONLY as a quoted correction. Checking for its absence
    # outright would forbid documenting the error -- the same trap that made an earlier
    # version of this suite fail on its own explanatory comment.
    claim = "This is read-only but grants visibility"
    idx = text.find(claim)
    if idx != -1:
        preceding = text[max(0, idx - 400):idx]
        assert "previously read" in preceding, (
            "the read-only claim appears as a live assertion, not a quoted correction — "
            "the mount carries no :ro, and :ro would not make the Docker API read-only"
        )
