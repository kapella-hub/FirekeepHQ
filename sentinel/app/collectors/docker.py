"""Docker container state change collector."""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.store import push_event

logger = logging.getLogger(__name__)


class DockerCollector:
    """Tracks Docker container state changes."""

    def __init__(self):
        self.name = "docker"
        self.healthy = True


_collector = DockerCollector()


def get_collector() -> DockerCollector:
    return _collector


async def run_docker_collector(redis, settings, stop_event: asyncio.Event) -> None:
    """Poll Docker API for container state changes."""
    collector = get_collector()
    known_states: dict[str, str] = {}

    while not stop_event.is_set():
        try:
            transport = httpx.AsyncHTTPTransport(uds=settings.DOCKER_SOCKET)
            async with httpx.AsyncClient(transport=transport, base_url="http://docker") as client:
                resp = await client.get("/containers/json?all=true", timeout=10)
                if resp.status_code == 200:
                    containers = resp.json()
                    for c in containers:
                        name = c.get("Names", ["/unknown"])[0].lstrip("/")
                        state = c.get("State", "unknown")
                        status = c.get("Status", "")
                        prev = known_states.get(name)
                        if prev != state:
                            if prev is not None:  # skip initial population
                                await push_event(
                                    redis,
                                    "docker",
                                    f"container.{state}",
                                    f"Container {name}: {status}",
                                    {"container": name, "state": state, "status": status},
                                    "warning" if state != "running" else "info",
                                    ["docker", name],
                                    maxlen=settings.EVENT_MAXLEN,
                                )
                            known_states[name] = state
            collector.healthy = True
        except Exception as e:
            logger.warning("Collector %s error: %s", collector.name, e)
            collector.healthy = False

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.POLL_INTERVAL_DOCKER)
        except asyncio.TimeoutError:
            pass
