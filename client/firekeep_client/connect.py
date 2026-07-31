"""`firekeep connect <user@host>` — one command from nothing to a working client.

Why this exists
---------------
Installing the client against a real server took NINE steps, of which a new user
completes two. The rest were undiscoverable:

  * The documented `--host <SERVER_IP>` cannot work: the shipped compose binds
    every port to 127.0.0.1 (deliberately — all-interfaces binding is what leaked
    twelve secrets once). A remote client needs a tunnel, and nothing said so.
    `doctor` reported "unreachable: timed out", which is true and useless.
  * `install` completes "successfully" writing a config with NO api_key, so every
    call 401s against an auth-enabled server. Nothing warns.
  * Getting a key required an admin credential that is printed once and never
    stored, so on a long-running server it does not exist any more.

Each of those is scriptable, and this is the script. It is deliberately a
SEQUENCE OF PROBES, not a fixed recipe: it asks the server what it is, rather
than assuming, so a server that is NOT loopback-bound skips the tunnel, and a
machine that already has a tunnel reuses it.

Stdlib only (SP1b import boundary) — subprocess for `ssh`, socket for the port
probe. `ssh` itself is the one external dependency, and its absence is reported
rather than raised.
"""
from __future__ import annotations

import os
import json
import shlex
import socket
import subprocess
import time

from firekeep_client import resolver

# Ports the client needs: MCP + REST across the four services, plus the dashboard
# (8040) which is browser-only but is the thing people check first.
TUNNEL_PORTS = (8040, 8050, 8060, 8070, 8080, 8100)

# Where the server install usually lives. Probed in order; --remote-dir overrides.
REMOTE_DIR_CANDIDATES = ("/opt/Firekeep", "/opt/firekeep", "/srv/Firekeep", "~/Firekeep")

_SSH_BASE = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")


class ConnectError(RuntimeError):
    """Actionable failure: the message is written for the person running the command."""


def _say(label: str, value: str) -> None:
    print(f"  {label:.<16} {value}", flush=True)


def _ssh(target: str, remote_cmd: str, *, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["ssh", *_SSH_BASE, target, remote_cmd],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:                     # no ssh on PATH
        raise ConnectError("`ssh` was not found on PATH. Install OpenSSH, or use "
                           "`firekeep install --host <h>` and configure the key by hand.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConnectError(f"ssh to {target} timed out after {timeout}s.") from exc
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _probe_server(target: str, remote_dir: str | None) -> dict:
    """Ask the server what it is. Every later decision keys off this, so nothing
    downstream has to assume a layout or a binding."""
    rc, out = _ssh(target, "echo FIREKEEP_SSH_OK")
    if rc != 0 or "FIREKEEP_SSH_OK" not in out:
        raise ConnectError(f"cannot ssh to {target}:\n{out.strip()[:400]}")
    _say("ssh ok", target)

    dirs = [remote_dir] if remote_dir else list(REMOTE_DIR_CANDIDATES)
    probe = " || ".join(f'[ -f {d}/docker-compose.yml ] && echo {d}' for d in dirs)
    rc, out = _ssh(target, probe)
    found = next((ln.strip() for ln in out.splitlines() if ln.strip().startswith(("/", "~"))), "")
    if not found:
        raise ConnectError(
            "no Firekeep install found on the server (looked in: "
            + ", ".join(dirs) + "). Pass --remote-dir if it lives elsewhere.")

    rc, out = _ssh(target, f"cd {found} && "
                           "echo COMMIT=$(git rev-parse --short HEAD 2>/dev/null) && "
                           "grep -E '^(BIND_ADDR|AUTH_ENABLED)=' .env 2>/dev/null")
    info = {"dir": found, "commit": "", "bind_addr": "", "auth": ""}
    for line in out.splitlines():
        if line.startswith("COMMIT="):
            info["commit"] = line.split("=", 1)[1].strip()
        elif line.startswith("BIND_ADDR="):
            info["bind_addr"] = line.split("=", 1)[1].strip()
        elif line.startswith("AUTH_ENABLED="):
            info["auth"] = line.split("=", 1)[1].strip()
    _say("server", f"{found} @ {info['commit'] or 'unknown'}, auth "
                   f"{'ON' if info['auth'].lower() == 'true' else 'off'}")
    return info


def _issue_invite(target: str, remote_dir: str, agent_id: str) -> str:
    """Issue a one-time code over SSH; redemption still goes through POST /enroll."""
    rc, out = _ssh(
        target,
        f"cd {shlex.quote(remote_dir)} && deploy/firekeep-admin invite "
        f"--agent {shlex.quote(agent_id)} --json < /dev/null",
        timeout=90,
    )
    for line in reversed(out.splitlines()):
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        code = result.get("code") if isinstance(result, dict) else None
        if isinstance(code, str) and code.startswith("fk_join_"):
            _say("invite", f"issued for {agent_id}")
            return code

    # Distinguish "server is too old" from a genuine failure. The local-mint path
    # is what makes this work without an admin key; a server predating it either
    # prompts (and gets EOF from our closed stdin, producing NOTHING) or curls its
    # own API with a credential that no longer exists. Reporting the empty output
    # verbatim would be the same undiagnosable dead end this command exists to end.
    rc2, probe = _ssh(
        target,
        f"grep -c 'invite)' {shlex.quote(remote_dir)}/deploy/firekeep-admin 2>/dev/null || echo 0",
    )
    if probe.strip().startswith("0"):
        raise ConnectError(
            f"the server's deploy/firekeep-admin predates client enrollment, so it cannot "
            f"issue a join code.\n"
            f"  Fix on the server:  cd {remote_dir} && git pull\n"
            f"  Then re-run this command.")
    raise ConnectError(
        "could not issue a join code on the server (exit "
        f"{rc}).\n{(out.strip() or '<no output>')[:500]}")


def _tunnel_running() -> bool:
    """A tunnel is already up if every port answers locally. Cheap, and it means
    re-running connect never stacks a second forwarder on top of a working one."""
    return all(_port_open(p) for p in TUNNEL_PORTS)


def _start_tunnel(target: str) -> None:
    forwards: list[str] = []
    for p in TUNNEL_PORTS:
        forwards += ["-L", f"{p}:127.0.0.1:{p}"]
    cmd = ["ssh", "-N", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30",
           "-o", "ExitOnForwardFailure=yes", *forwards, target]
    # Detach so the tunnel outlives this process. -f would background it in ssh
    # itself, but that hides failures; a detached child plus the port probe below
    # tells us whether it actually came up.
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kwargs)  # noqa: S603
    except FileNotFoundError as exc:
        raise ConnectError(
            "this join code needs an SSH tunnel (the server binds to loopback) "
            "but 'ssh' is not on PATH. Install OpenSSH, or ask the issuer to "
            "expose the stack over TLS and reissue the code."
        ) from exc

    for _ in range(20):
        time.sleep(0.5)
        if _tunnel_running():
            return
    raise ConnectError(
        f"started an SSH tunnel to {target} but the ports never came up. Something else may be "
        f"bound to one of {', '.join(map(str, TUNNEL_PORTS))}, or the server is not serving them.")


def connect(target: str, *, agent_id: str | None = None,
            remote_dir: str | None = None, use_tunnel: bool = True) -> int:
    """Probe, issue a join code over SSH, then use the one enrollment path."""
    existing_agent = ""
    try:
        cfg = resolver.load_config()
        existing_agent = resolver.agent_id(cfg)
    except resolver.ConfigMigrationConflict:
        # Connecting is a destructive repoint of the one config. Never let it
        # silently choose over an ambiguous legacy migration.
        raise
    except resolver.ConfigError as exc:
        if resolver._config_path().exists():
            raise ConnectError(f"cannot read existing Firekeep config: {exc}") from exc
        # First install: connect will create the config below.

    info = _probe_server(target, remote_dir)

    loopback = info["bind_addr"] in ("127.0.0.1", "localhost", "::1")
    _say("bind addr", f"{info['bind_addr'] or 'unknown'}"
                      f"{' (loopback -> tunnel required)' if loopback else ''}")

    if loopback and not use_tunnel:
        raise ConnectError(
            "the server binds to loopback, so a remote client cannot reach it without a "
            "tunnel, and --no-tunnel was given. Either drop --no-tunnel, or put a TLS "
            "reverse proxy in front of the stack and use a paths-style [server] connection.")
    if not agent_id:
        agent_id = existing_agent
    if not agent_id or agent_id == "CHANGEME":
        agent_id = f"agent-{socket.gethostname().lower()}"

    code = _issue_invite(target, info["dir"], agent_id)
    from firekeep_client.join import JoinError, join
    try:
        return join(code, agent_id=agent_id, force=True)
    except JoinError as exc:
        raise ConnectError(str(exc)) from exc
