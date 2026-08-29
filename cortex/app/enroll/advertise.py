"""Where a joining device should point itself: the address this server serves on.

`BIND_ADDR` and `VPS_IP` are not interchangeable, and treating them as one
address is what made dashboard-issued invites unusable. `BIND_ADDR` is the host
interface every published app port actually binds to (see the `ports:` blocks in
`docker-compose.yml`); `VPS_IP` is the machine's declared address, used for the
CORS origin and as an ssh destination. On the deployment that surfaced this they
are different machines' worth of different: `BIND_ADDR=100.64.0.1` answers on
:8100 over the tailnet, while `VPS_IP=203.0.113.7` is a public address where
nothing is published at all. An invite built from `VPS_IP` could therefore only
ever be redeemed through an ssh tunnel, and the code it minted told the joining
machine to keep `host = 127.0.0.1` forever after.

So: ask `BIND_ADDR` what is reachable, and fall back to `VPS_IP` only when
`BIND_ADDR` is a wildcard, where every routable address of the box is published
and `VPS_IP` is the operator's own statement of which one that is.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "[::1]"})
WILDCARD = frozenset({"0.0.0.0", "::", "[::]", "*"})


@dataclass(frozen=True)
class Advertised:
    """The service address a new device should dial, and why."""

    host: str
    source: str
    detail: str

    @property
    def reachable(self) -> bool:
        return bool(self.host)


def advertised_host(env: dict[str, str] | None = None) -> Advertised:
    """Resolve the address an off-box client can reach this server's ports at."""
    environ = os.environ if env is None else env
    bind = (environ.get("BIND_ADDR") or "").strip()
    vps = (environ.get("VPS_IP") or "").strip()

    if bind and bind not in LOOPBACK and bind not in WILDCARD:
        return Advertised(
            bind,
            "BIND_ADDR",
            f"every published port binds to {bind}, so that is what a device dials",
        )
    if bind in WILDCARD:
        if vps and vps not in LOOPBACK:
            return Advertised(
                vps,
                "VPS_IP",
                f"ports are published on every interface; VPS_IP says to use {vps}",
            )
        return Advertised(
            "",
            "none",
            "ports are published on every interface but no VPS_IP names one — "
            "enter the address devices should use",
        )
    return Advertised(
        "",
        "none",
        "published ports bind to localhost only (BIND_ADDR is unset or loopback), "
        "so nothing off this machine can reach them without a tunnel",
    )


def resolve_connection(
    *,
    transport: str,
    kind: str,
    host: str,
    env: dict[str, str] | None = None,
) -> tuple[str, str, bool]:
    """Fill an unnamed transport/host from what this server actually publishes.

    This is the policy `deploy/firekeep-admin invite` has always applied in
    shell, moved here so the dashboard and the API share it rather than each
    inventing one. It diverges on a single point: the shell demands
    `--insecure-http` before minting a network-reachable HTTP code, while an
    address resolved here needs no such flag.

    Returns ``(transport, host, server_chosen)``. ``server_chosen`` marks a plain
    HTTP host the server picked from its own published address rather than one
    the caller named — the `insecure_http` confirmation guards operator-chosen
    hosts, and there is nothing to confirm about the address this process is
    already serving cleartext on. The caller that has no flag to pass is the
    dashboard, which states the cleartext consequence in the form instead.
    """
    advertised = advertised_host(env)
    if not transport:
        if advertised.reachable and kind == "ports":
            return "http", advertised.host, True
        return "tunnel", host or "127.0.0.1", False
    if transport == "tunnel":
        # The client reaches the forwarded port, never `host`; keep it honest.
        return transport, "127.0.0.1", False
    if kind == "ports" and not host and advertised.reachable:
        return transport, advertised.host, False
    return transport, host, False
