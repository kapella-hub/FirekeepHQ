#!/usr/bin/env python3
"""Cold-install lab: run the PUBLISHED install experience against a dist built from
this working tree, in throwaway containers, on every platform we claim to support.

The point is to be able to answer "is the install good?" with a transcript instead of
an opinion. Every scenario writes its full stdout/stderr to .runs/<scenario>/<image>.log
and returns a pass/fail on assertions about what a *stranger* would see.

    python scripts/installlab/lab.py up                  # network + dist HTTP server
    python scripts/installlab/lab.py client ubuntu       # one image, default scenario
    python scripts/installlab/lab.py matrix              # every image x every scenario
    python scripts/installlab/lab.py server              # full server install (dind)
    python scripts/installlab/lab.py down                # remove everything

Scenarios, and why each one exists:

  headless  stdin closed, no tty. The CI/provisioning path. The bootstrap detects no
            /dev/tty and writes a default config without prompting.
  enter     a REAL pty (via util-linux `script`) with a human who presses Enter at
            every prompt. This is what the author actually did on srv1574321, and it
            is the path no automated test has ever covered, because piping answers
            into the installer -- which is what .github/workflows/install-smoke.yml
            does -- supplies the answer key the real user did not have.
  init      `enter`, then `firekeep init`, capturing exactly where a self-hosting
            user lands. Reproduces the reported dead end.

`enter` is the scenario that matters. A prompt whose default is wrong is invisible to
every other kind of test.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LAB = REPO / "scripts" / "installlab"
DIST = LAB / ".dist"
RUNS = LAB / ".runs"

NETWORK = "firekeep-lab"
DIST_CONTAINER = "firekeep-lab-dist"
DIST_BASE = "http://dist:8000"
# Small, has python, and is not one of the product's pinned images -- the lab must
# never become a second place where a shipped image version is decided.
DIST_IMAGE = "python:3.12-alpine"


@dataclass(frozen=True)
class Target:
    """One OS we claim `curl … | sh` works on."""

    name: str
    image: str
    #: Bring the image up to "a machine a developer would actually have": a shell,
    #: TLS roots, a downloader, and util-linux's `script` for the pty scenarios.
    prep: str
    #: Some images ship no bash at all; the bootstrap is POSIX sh on purpose, and
    #: this records which shell the scenario command itself runs under.
    shell: str = "sh"
    notes: str = ""


TARGETS: tuple[Target, ...] = (
    Target(
        "ubuntu",
        "ubuntu:24.04",
        "apt-get update -qq && apt-get install -y -qq curl ca-certificates util-linux >/dev/null",
        shell="bash",
    ),
    Target(
        "debian",
        "debian:12-slim",
        "apt-get update -qq && apt-get install -y -qq curl ca-certificates util-linux >/dev/null",
        shell="bash",
    ),
    Target(
        "alpine",
        "alpine:3.20",
        "apk add --no-cache curl ca-certificates util-linux >/dev/null",
        notes="musl: proves the musl uv + musllinux wheel path, and /bin/sh is busybox ash",
    ),
    Target(
        "fedora",
        "fedora:41",
        "dnf install -y -q curl ca-certificates util-linux >/dev/null 2>&1",
        shell="bash",
    ),
    Target(
        "rocky",
        "rockylinux:9",
        "dnf install -y -q curl ca-certificates util-linux >/dev/null 2>&1",
        shell="bash",
        notes="RHEL-family, the most common enterprise VPS image",
    ),
    Target(
        "arch",
        "archlinux:base",
        "pacman -Sy --noconfirm --quiet curl ca-certificates util-linux >/dev/null 2>&1",
        shell="bash",
    ),
    Target(
        "opensuse",
        "opensuse/leap:15.6",
        "zypper -q --non-interactive install -y curl ca-certificates util-linux >/dev/null 2>&1",
        shell="bash",
    ),
)

TARGETS_BY_NAME = {t.name: t for t in TARGETS}


@dataclass
class Result:
    scenario: str
    target: str
    exit_code: int
    log: Path
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


# --------------------------------------------------------------------------- docker


def docker(*args: str, check: bool = True, capture: bool = False, **kw):
    cmd = ["docker", *args]
    if capture:
        kw.setdefault("stdout", subprocess.PIPE)
        kw.setdefault("stderr", subprocess.STDOUT)
        kw.setdefault("text", True)
    result = subprocess.run(cmd, **kw)
    if check and result.returncode != 0:
        detail = (result.stdout or "") if capture else ""
        raise SystemExit(f"lab: docker {' '.join(args)} failed\n{detail}")
    return result


def container_running(name: str) -> bool:
    out = docker(
        "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}",
        capture=True, check=False,
    ).stdout
    return name in (out or "")


def up() -> None:
    """Create the lab network and serve the dist over HTTP inside it."""
    if not (DIST / "latest" / "latest.json").is_file():
        raise SystemExit(
            "lab: no dist yet — run: python scripts/installlab/dist.py --dist-base "
            f"{DIST_BASE}"
        )
    docker("network", "create", NETWORK, check=False, capture=True)
    if container_running(DIST_CONTAINER):
        print("lab: dist server already up")
        return
    docker("rm", "-f", DIST_CONTAINER, check=False, capture=True)
    docker(
        "run", "-d", "--name", DIST_CONTAINER,
        "--network", NETWORK, "--network-alias", "dist",
        "-v", f"{DIST}:/srv:ro", "-w", "/srv",
        DIST_IMAGE, "python", "-m", "http.server", "8000",
        capture=True,
    )
    # The alias has to resolve before any target tries to fetch through it.
    for _ in range(40):
        probe = docker(
            "run", "--rm", "--network", NETWORK, DIST_IMAGE,
            "python", "-c",
            "import urllib.request,sys;"
            "sys.exit(0 if urllib.request.urlopen"
            "('http://dist:8000/latest/latest.json',timeout=2).status==200 else 1)",
            check=False, capture=True,
        )
        if probe.returncode == 0:
            manifest = json.loads((DIST / "latest" / "latest.json").read_text())
            print(f"lab: dist server up — serving client {manifest['version']} at {DIST_BASE}")
            return
        time.sleep(0.5)
    raise SystemExit("lab: dist server never became reachable on the lab network")


def down() -> None:
    docker("rm", "-f", DIST_CONTAINER, check=False, capture=True)
    for name in ("firekeep-lab-dind", "firekeep-lab-server"):
        docker("rm", "-f", name, check=False, capture=True)
    docker("volume", "rm", "-f", "firekeep-lab-dind-data", "firekeep-lab-root",
           check=False, capture=True)
    docker("network", "rm", NETWORK, check=False, capture=True)
    print("lab: torn down")


# ------------------------------------------------------------------- scenario bodies

ONELINER = f"curl -fsSL {DIST_BASE}/latest/install.sh | sh"

#: `script` gives the install a real pty, so the bootstrap's `( : < /dev/tty )` probe
#: succeeds and the wizard prompts exactly as it does for a human. Feeding \n from
#: stdin is a person pressing Enter -- accepting every default, which is the single
#: most common thing a first-time installer does and the case with no coverage today.
def enter_body(presses: int = 12) -> str:
    newlines = "\\n" * presses
    return f"printf '{newlines}' | script -qec '{ONELINER}' /dev/null"


SCENARIOS: dict[str, str] = {
    # No tty at all: the bootstrap must take its documented headless branch.
    # NOT `| sh < /dev/null` -- under `curl | sh` the SCRIPT is sh's stdin, so
    # redirecting stdin hands sh an empty file, it exits immediately, and curl
    # dies with "(23) Failure writing output to destination" before the installer
    # ever runs. `docker run` without -t already provides the no-tty condition.
    "headless": ONELINER,
    "enter": enter_body(),
    "init": (
        enter_body()
        + " ; echo '=== END CLIENT INSTALL ==='"
        + " ; export PATH=\"$HOME/.firekeep/shims:$PATH\""
        + " ; printf '\\n\\n\\n\\n' | script -qec 'firekeep init' /dev/null"
        + " ; echo \"=== firekeep init exit=$? ===\""
    ),
    # The whole journey as ONE command, which is what the redesign is for: the
    # one-liner, Enter through the single routing question, and end at an
    # enrolled machine. Run in the server lab, where Docker exists and the
    # routing question therefore defaults to "set one up here".
    "oneshot": (
        enter_body()
        + " ; echo \"=== ONELINER exit=$? ===\""
        + " ; export PATH=\"$HOME/.firekeep/shims:$PATH\""
        + " ; echo '=== DOCTOR ==='"
        + " ; firekeep doctor ; echo \"=== doctor exit=$? ===\""
    ),
    # `oneshot`, but with the model-pull grace forced to zero so the install
    # ALWAYS takes the background-and-report branch. On a fast link the pull can
    # finish inside the default 120s grace, which is a fine outcome and means the
    # interesting branch never runs — the first full lab run hit exactly that and
    # reported "[OK] models ready", proving nothing about the path that replaced
    # a 15-minute silent wait. The env var is exported before the one-liner and
    # inherited all the way down: bootstrap -> firekeep install -> cmd_init ->
    # subprocess install.sh, which is also a test of that inheritance.
    "oneshot-warming": (
        "export FIREKEEP_MODEL_PULL_GRACE=0 ; "
        + enter_body()
        + " ; echo \"=== ONELINER exit=$? ===\""
        + " ; export PATH=\"$HOME/.firekeep/shims:$PATH\""
        + " ; echo '=== DOCTOR ==='"
        + " ; firekeep doctor ; echo \"=== doctor exit=$? ===\""
        + " ; echo '=== WATCHER ==='"
        # Existence of the log proves the spawn was ATTEMPTED (the `>>` redirect
        # creates it). Only a live process proves setsid/disown actually detached
        # it from the shell `firekeep init` reaps — which is the real risk, and
        # the thing an empty log would silently hide. The pull will not finish
        # inside this container's lifetime, so "still running" is the pass.
        + " ; ls -l \"$HOME/.firekeep/server/model-pull.log\" 2>&1"
        + " ; pgrep -fa 'seq 1 360' >/dev/null 2>&1"
        + "   && echo 'WATCHER ALIVE' || echo 'WATCHER GONE'"
    ),
    "doctor": (
        ONELINER
        + " ; export PATH=\"$HOME/.firekeep/shims:$PATH\""
        + " ; echo '=== DOCTOR ==='"
        + " ; firekeep doctor ; echo \"=== doctor exit=$? ===\""
    ),
}


def run_scenario(target: Target, scenario: str, timeout: int = 900) -> Result:
    body = SCENARIOS[scenario]
    script = f"set -x\n{target.prep}\nset +x\n{body}\n"
    RUNS.joinpath(scenario).mkdir(parents=True, exist_ok=True)
    log = RUNS / scenario / f"{target.name}.log"

    print(f"lab: [{scenario}] {target.name} ({target.image}) …", flush=True)
    completed = subprocess.run(
        [
            "docker", "run", "--rm", "--network", NETWORK,
            # A stranger's box has no Firekeep state. Nothing is mounted from the
            # host, so every run starts from a genuinely empty $HOME.
            "-e", "HOME=/root",
            target.image, target.shell, "-c", script,
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        errors="replace", timeout=timeout,
    )
    output = completed.stdout or ""
    header = (
        f"# scenario: {scenario}\n# image: {target.image}\n"
        f"# exit: {completed.returncode}\n# command:\n{script}\n{'=' * 72}\n"
    )
    log.write_text(redact(header + readable(output)), encoding="utf-8")
    log.with_suffix(".raw.log").write_text(redact(header + output), encoding="utf-8")
    result = Result(scenario, target.name, completed.returncode, log)
    result.failures = assess(scenario, completed.returncode, output)
    verdict = "PASS" if result.ok else "FAIL"
    print(f"lab: [{scenario}] {target.name}: {verdict} (exit {completed.returncode}) -> {log}")
    for failure in result.failures:
        print(f"lab:     - {failure}")
    return result


#: uv draws download bars and spinners, and under a pty they all reach the log.
#: A transcript nobody can read is a transcript nobody checks, so the .log is the
#: human-readable rendering and .raw.log keeps every byte for when the escape
#: codes themselves are the thing under investigation.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")
_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


#: A successful server install prints a live admin key (`nxs_` + 48 hex) twice,
#: by design -- it is not stored anywhere else. The lab writes transcripts to
#: disk, so it must not be the thing that gives that key a second life in a file
#: nobody remembers. Lab stacks are throwaway, but the redaction is unconditional
#: because the day it matters is the day someone points the lab at a real host.
_SECRET = re.compile(r"\bnxs_[0-9a-f]{16,}")


def redact(text: str) -> str:
    return _SECRET.sub("nxs_<redacted-by-lab>", text)


def readable(output: str) -> str:
    text = _ANSI.sub("", output).replace("\r\n", "\n").replace("\r", "\n")
    kept: list[str] = []
    for line in text.split("\n"):
        if any(marker in line for marker in _SPINNER):
            continue
        if "(download)" in line and "B/" in line:
            continue
        if line.strip() in ("", "[2K", "[1A", "[1B"):
            if kept and kept[-1] == "":
                continue
            kept.append("")
            continue
        kept.append(line)
    return "\n".join(kept)


def assess(scenario: str, exit_code: int, output: str) -> list[str]:
    """What a stranger would call broken. Deliberately about the EXPERIENCE, not
    just the exit code — an install that 'succeeds' into an unusable config is the
    failure this whole exercise is about."""
    failures: list[str] = []
    low = output.lower()

    if scenario in ("headless", "enter", "doctor", "oneshot"):
        if exit_code != 0:
            failures.append(f"client install exited {exit_code}")
        if "firekeep: installed into" not in output:
            failures.append("never printed the 'installed into' confirmation")
        if "traceback (most recent call last)" in low:
            failures.append("a Python traceback reached the user")

    if scenario in ("enter", "oneshot"):
        # The heart of it: after accepting every default the user must NOT be left
        # holding a config that points at a server that does not exist, with no
        # statement of what to do next.
        if re.search(r"(?m)^Server host \(IP or hostname\)", output):
            failures.append(
                "prompted for a server host defaulting to 127.0.0.1 where no server exists"
            )
        if re.search(r"(?m)^API key", output):
            failures.append("prompted for an API key before any server could have minted one")
        if "where is your firekeep server?" not in low:
            failures.append("never asked the one question a machine cannot answer")

    if scenario == "enter":
        # A box with no Docker cannot host a server, so accepting the default must
        # land on "not connected yet" AND name the three ways out. Ending on a
        # bare "run firekeep doctor" is the old failure in a new costume.
        if "not connected to a server yet" not in low:
            failures.append("did not state that the client has no server")
        for command in ("firekeep init", "firekeep join", "firekeep connect"):
            if command not in output:
                failures.append(f"next steps never name `{command}`")

    if scenario == "oneshot-warming":
        # The branch that replaced a 15-minute silent wait. It must say the
        # stack is usable AND what is not yet true, and the detached watcher
        # must actually be running.
        if "[..] models are still downloading" not in output:
            failures.append("the model pull did not background")
        if 'status="partial"' not in output:
            failures.append("never stated that writes are not yet searchable")
        if "model-pull.log" not in output:
            failures.append("the watcher log was never created — no spawn happened")
        if "WATCHER GONE" in output:
            failures.append(
                "the watcher died with its parent — setsid/disown did not detach it"
            )

    if scenario in ("oneshot", "oneshot-warming"):
        # Docker is present, so Enter provisions. This is the whole redesign in
        # one assertion block: one command, one question, a working agent.
        for prompt in ("VPS IP address:", "Neo4j password:"):
            if re.search(rf"(?m)^{re.escape(prompt)}[ \t]*$", output):
                failures.append(f"server installer still prompts: {prompt!r}")
        if "firekeep is running!" not in low:
            failures.append("the server never came up")
        if "this machine is connected" not in low:
            failures.append("the box did not enrol itself against the server it built")
        if "firekeep_join=" not in low:
            failures.append("no paste-ready join line for the next machine")
        if "=== doctor exit=0 ===" not in output:
            failures.append("doctor is not green after a complete one-command install")

    if scenario == "init":
        # Match the PROMPTS, not the words. The fixed installer reports
        # "[OK] Neo4j password generated", which a bare substring check for
        # "neo4j password" flags as the very defect it proves is gone. A prompt
        # is a line that ENDS at the colon with no newline after it, so anchor
        # on the prompt shape rather than the noun.
        for prompt, complaint in (
            ("VPS IP address:", "asked for a VPS IP the machine already knows"),
            ("Neo4j password:", "asked the user to invent a machine-only secret"),
        ):
            if re.search(rf"(?m)^{re.escape(prompt)}[ \t]*$", output):
                failures.append(f"server installer {complaint}")
        if "must not be empty" in low:
            failures.append("rejected empty input instead of defaulting")
        if "[skip] cryptography not installed" in low or "could not generate vault_key" in low:
            failures.append("shipped without a VAULT_KEY — /vault/* will answer 503")
        if "exited with status 1" in low:
            failures.append("firekeep init failed")
        # The point of the whole exercise: does the user end up connected?
        if "vps ip address:" not in low and "exited with status" not in low:
            if "doctor" not in low and "firekeep join" not in low:
                failures.append("init succeeded but never named the next step")

    if scenario == "doctor":
        # Doctor is the documented next step. If it cannot say "you have no server,
        # here is how to get one", it is a status page, not a doctor.
        if "=== doctor exit=0 ===" not in output and "firekeep init" not in output:
            failures.append("doctor did not name the command that fixes the problem")

    return failures


# ------------------------------------------------------------------------ server lab

DIND_IMAGE = "docker:27-dind"
SERVER_IMAGE = "ubuntu:24.04"


def server_lab(scenario: str = "oneshot", timeout: int = 3600) -> Result:
    """A self-hosting user's whole journey on one box: client bootstrap, then
    `firekeep init`, then the server installer, with a real Docker daemon underneath.

    Docker-in-Docker rather than the host socket, deliberately: a container that
    talks to the HOST daemon cannot bind-mount its own paths (the daemon resolves
    them on the host), so compose volumes silently point at the wrong files. The
    control container shares the dind container's network namespace so 127.0.0.1
    means the same thing to both -- which matters because the product's shipped
    default is BIND_ADDR=127.0.0.1.
    """
    RUNS.joinpath("server").mkdir(parents=True, exist_ok=True)
    log = RUNS / "server" / f"{scenario}.log"

    docker("rm", "-f", "firekeep-lab-dind", "firekeep-lab-server", check=False, capture=True)
    docker("volume", "rm", "-f", "firekeep-lab-dind-data", "firekeep-lab-root",
           check=False, capture=True)
    docker("volume", "create", "firekeep-lab-dind-data", capture=True)
    docker("volume", "create", "firekeep-lab-root", capture=True)

    print("lab: starting docker-in-docker …", flush=True)
    docker(
        "run", "-d", "--name", "firekeep-lab-dind", "--privileged",
        "--network", NETWORK, "--network-alias", "fkserver",
        "-e", "DOCKER_TLS_CERTDIR=",  # plain tcp, only inside this netns
        "-v", "firekeep-lab-dind-data:/var/lib/docker",
        "-v", "firekeep-lab-root:/root",
        DIND_IMAGE, capture=True,
    )
    for _ in range(120):
        probe = docker(
            "exec", "firekeep-lab-dind", "docker", "info",
            check=False, capture=True,
        )
        if probe.returncode == 0:
            break
        time.sleep(1)
    else:
        raise SystemExit("lab: dind never became ready")
    print("lab: dind ready", flush=True)

    body = SCENARIOS[scenario]
    script = (
        "set -x\n"
        "apt-get update -qq && apt-get install -y -qq curl ca-certificates util-linux "
        "docker.io docker-compose-v2 openssl >/dev/null\n"
        "set +x\n"
        "docker info >/dev/null 2>&1 || { echo 'lab: no docker daemon reachable'; exit 90; }\n"
        f"{body}\n"
    )
    completed = subprocess.run(
        [
            "docker", "run", "--rm", "--name", "firekeep-lab-server",
            "--network", "container:firekeep-lab-dind",
            "-e", "DOCKER_HOST=tcp://127.0.0.1:2375",
            "-e", "HOME=/root",
            "-v", "firekeep-lab-root:/root",
            SERVER_IMAGE, "bash", "-c", script,
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        errors="replace", timeout=timeout,
    )
    output = completed.stdout or ""
    header = (
        f"# server lab scenario: {scenario}\n# exit: {completed.returncode}\n"
        f"# command:\n{script}\n{'=' * 72}\n"
    )
    log.write_text(redact(header + readable(output)), encoding="utf-8")
    log.with_suffix(".raw.log").write_text(redact(header + output), encoding="utf-8")
    result = Result(f"server:{scenario}", "dind", completed.returncode, log)
    result.failures = assess(scenario, completed.returncode, output)
    print(f"lab: [server:{scenario}] {'PASS' if result.ok else 'FAIL'} "
          f"(exit {completed.returncode}) -> {log}")
    for failure in result.failures:
        print(f"lab:     - {failure}")
    return result


# ----------------------------------------------------------------------------- cli


def report(results: list[Result]) -> int:
    print("\n" + "=" * 72)
    print("INSTALL LAB")
    print("=" * 72)
    width = max((len(f"{r.scenario}/{r.target}") for r in results), default=20)
    for r in results:
        print(f"  {'PASS' if r.ok else 'FAIL'}  {f'{r.scenario}/{r.target}':<{width}}  "
              f"exit={r.exit_code}")
        for failure in r.failures:
            print(f"          · {failure}")
    failed = [r for r in results if not r.ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("up")
    sub.add_parser("down")
    sub.add_parser("targets")

    client = sub.add_parser("client")
    client.add_argument("target", choices=sorted(TARGETS_BY_NAME))
    client.add_argument("--scenario", default="enter", choices=sorted(SCENARIOS))

    matrix = sub.add_parser("matrix")
    matrix.add_argument("--scenarios", default="headless,enter")
    matrix.add_argument("--targets", default=",".join(t.name for t in TARGETS))

    server = sub.add_parser("server")
    server.add_argument("--scenario", default="oneshot", choices=sorted(SCENARIOS))

    args = parser.parse_args(argv)
    if not shutil.which("docker"):
        raise SystemExit("lab: docker is not on PATH")

    if args.command == "up":
        up()
        return 0
    if args.command == "down":
        down()
        return 0
    if args.command == "targets":
        for t in TARGETS:
            print(f"  {t.name:<10} {t.image:<22} {t.notes}")
        return 0

    up()
    if args.command == "client":
        return report([run_scenario(TARGETS_BY_NAME[args.target], args.scenario)])
    if args.command == "server":
        return report([server_lab(args.scenario)])
    if args.command == "matrix":
        names = [n.strip() for n in args.targets.split(",") if n.strip()]
        scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
        results = [
            run_scenario(TARGETS_BY_NAME[name], scenario)
            for scenario in scenarios
            for name in names
        ]
        return report(results)
    return 2


if __name__ == "__main__":
    sys.exit(main())
