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
import re
import socket
import subprocess
import time
from pathlib import Path

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


def _mint_key(target: str, remote_dir: str, agent_id: str) -> str:
    """Mint through firekeep-admin's LOCAL path — no admin key needed on the server
    (deploy/firekeep-admin). stdin is closed so it can never sit on a prompt."""
    rc, out = _ssh(
        target,
        f"cd {remote_dir} && bash deploy/firekeep-admin keys create --agent {agent_id} < /dev/null",
        timeout=90,
    )
    m = re.search(r'"api_key"\s*:\s*"([^"]+)"', out)
    if m:
        _say("key", f"minted for {agent_id}")
        return m.group(1)

    # Distinguish "server is too old" from a genuine failure. The local-mint path
    # is what makes this work without an admin key; a server predating it either
    # prompts (and gets EOF from our closed stdin, producing NOTHING) or curls its
    # own API with a credential that no longer exists. Reporting the empty output
    # verbatim would be the same undiagnosable dead end this command exists to end.
    rc2, probe = _ssh(target, f"grep -c mint_local {remote_dir}/deploy/firekeep-admin 2>/dev/null || echo 0")
    if probe.strip().startswith("0"):
        raise ConnectError(
            f"the server's deploy/firekeep-admin predates local key minting, so it cannot "
            f"issue a key without an admin credential that is printed once and never stored.\n"
            f"  Fix on the server:  cd {remote_dir} && git pull\n"
            f"  Then re-run this command.")
    raise ConnectError(
        "could not mint an API key on the server (exit "
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
    subprocess.Popen(cmd, **kwargs)  # noqa: S603

    for _ in range(20):
        time.sleep(0.5)
        if _tunnel_running():
            return
    raise ConnectError(
        "started an SSH tunnel but the ports never came up. Something else may be "
        f"bound to one of {', '.join(map(str, TUNNEL_PORTS))}, or the server is not serving them.")


def _write_profile(profile: str, host: str, api_key: str, agent_id: str) -> Path:
    import configparser
    from firekeep_client.cli import _config_path
    path = _config_path()
    cp = configparser.ConfigParser()
    cp.optionxform = str
    if path.exists():
        cp.read(path, encoding="utf-8")
    if profile not in cp:
        cp[profile] = {}
    cp[profile].update({"kind": "ports", "scheme": "http", "host": host,
                        "verify_tls": "false", "agent_id": agent_id, "api_key": api_key})
    if "active" not in cp:
        cp["active"] = {}
    cp["active"]["profile"] = profile
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        cp.write(fh)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass          # Windows ACLs — `doctor`'s config-perms row reports on this
    return path


def connect(target: str, *, profile: str = "personal", agent_id: str | None = None,
            remote_dir: str | None = None, use_tunnel: bool = True) -> int:
    """Probe the server, mint a key, set up access, write the profile, verify."""
    info = _probe_server(target, remote_dir)

    host_only = target.split("@", 1)[-1]
    loopback = info["bind_addr"] in ("127.0.0.1", "localhost", "::1")
    _say("bind addr", f"{info['bind_addr'] or 'unknown'}"
                      f"{' (loopback -> tunnel required)' if loopback else ''}")

    if loopback and use_tunnel:
        if _tunnel_running():
            _say("tunnel", "already running, reused")
        else:
            _start_tunnel(target)
            _say("tunnel", "started (" + ", ".join(map(str, TUNNEL_PORTS)) + ")")
        client_host = "127.0.0.1"
    elif loopback:
        raise ConnectError(
            "the server binds to loopback, so a remote client cannot reach it without a "
            "tunnel, and --no-tunnel was given. Either drop --no-tunnel, or put a TLS "
            "reverse proxy in front of the stack and use a `paths` profile.")
    else:
        client_host = host_only

    if not agent_id:
        try:
            cfg = resolver.load_config()
            agent_id = resolver.agent_id(cfg, resolver.active_profile(cfg))
        except Exception:                      # noqa: BLE001 — first run, no config yet
            agent_id = ""
    if not agent_id or agent_id == "CHANGEME":
        agent_id = f"agent-{socket.gethostname().lower()}"

    api_key = _mint_key(target, info["dir"], agent_id)
    path = _write_profile(profile, client_host, api_key, agent_id)
    _say("config", f"{path} [{profile}]")

    from firekeep_client.cli import run_doctor
    print()
    rows = run_doctor()
    bad = [r for r in rows if r[1] != "ok"]
    print()
    if bad:
        print("  Some checks did not pass:")
        for name, status, detail in bad:
            print(f"    [{status.upper()}] {name}: {detail}")
        return 1
    print("  All checks passed. Restart your agent session to pick up the new servers.")
    return 0
