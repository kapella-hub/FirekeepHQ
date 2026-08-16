"""The install.sh / update.sh health-probe loops must parse their own service table.

Why this file exists
--------------------
`install.sh` grew a third field (name:port:probe-path) while its extraction stayed
`port="${svc##*:}"`. `##` strips the LONGEST `*:` prefix, so on a three-field entry
that expression returns the PROBE PATH. The installer built `http://localhost:/health/`,
curl answered 000 for all six services, every probe timed out, and `bash install.sh`
exited 1 — on a clean install, as the customer's first command.

Nothing caught it. It reads correctly, `bash -n` accepts it, and `update.sh` used the
identical idiom *correctly* because its entries had only two fields. The bug was born
the moment the field was added, and was invisible to every check that existed.

So this test does not re-implement the split — a reimplementation would have been
written with the same wrong assumption. It extracts the assignment lines *out of the
script* and executes them in a real bash, then asserts the port is a port. If someone
reverts to `${svc##*:}`, `port` becomes `/health` and `test_ports_are_numeric` fails.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from test_deploy_lib import BASH

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = ("install.sh", "update.sh")

# Published host ports, from docker-compose.yml. The health loop probing a port the
# stack does not publish is the other half of this defect class.
EXPECTED_PORTS = {"8100", "8080", "8070", "8060", "8050", "8040"}

# BASH comes from test_deploy_lib's resolver, NOT `shutil.which("bash")`, and the
# difference is not cosmetic on Windows. `CreateProcess` resolves a bare `bash` to
# the System32 WSL shim regardless of PATH order; when the WSL2 VM is busy — for
# instance running Docker Desktop while scripts/installlab/ drives a
# docker-in-docker install — that shim does not merely fail, it BLOCKS and then
# returns UTF-16 text:
#
#   The operation timed out because a response was not received from the virtual
#   machine or container. Error code: Bash/Service/CreateInstance/HCS_E_CONNECTION_TIMEOUT
#
# Six tests here went red that way with nothing wrong in install.sh. The resolver
# finds Git Bash and VERIFIES it, which is what this file needed all along.
pytestmark = pytest.mark.skipif(
    BASH is None, reason="needs a real bash to execute the extracted expansions"
)


def _script(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8")


def _service_entries(text: str) -> list[str]:
    """The literal strings inside the health-check `services=( ... )` array."""
    m = re.search(r"^services=\(\s*$(.*?)^\)\s*$", text, re.M | re.S)
    assert m, "could not find the `services=(...)` array"
    return re.findall(r'"([^"]+)"', m.group(1))


def _parse_block(text: str) -> str:
    """The shell assignments the script itself uses to split an entry.

    Everything between `for svc in "${services[@]}"; do` and the `printf` that
    announces the service. Extracted rather than copied so the test exercises the
    real expression.
    """
    m = re.search(
        r'for svc in "\$\{services\[@\]\}"; do(.*?)printf', text, re.S
    )
    assert m, "could not find the health-check loop"
    lines = [
        ln.strip()
        for ln in m.group(1).splitlines()
        if re.match(r'^\s*(name|rest|port|probe)=', ln)
    ]
    assert lines, "found no field-splitting assignments in the loop"
    return "\n".join(lines)


def _split_via_bash(parse_block: str, entry: str, tmp_path: Path) -> dict[str, str]:
    """Run the extracted assignments in a real bash and report what they produced.

    Via a temp FILE, not `bash -c <string>`: on Windows, subprocess joins an argv
    list with Windows quoting rules, and a multi-line script containing spaces and
    quotes comes out the far side mangled — every variable silently empty, which
    looks exactly like the bug this test hunts.

    And invoked as a BARE FILENAME with cwd, not an absolute path: Git Bash treats
    the backslashes in `C:\\Users\\...` as escapes and resolves the argument to
    `C:UsersmoganAppData...`, which does not exist. No separators, no mangling.
    """
    script = tmp_path / "split.sh"
    script.write_text(
        f"svc={entry!r}\n{parse_block}\n"
        'echo "$name"\necho "$port"\necho "$probe"\n',
        encoding="utf-8",
        newline="\n",
    )
    out = subprocess.run(
        [BASH, "split.sh"], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, f"bash failed: {out.stderr}"
    parts = out.stdout.split("\n")
    assert len(parts) >= 3, f"expected three lines, got {out.stdout!r}"
    return {"name": parts[0], "port": parts[1], "probe": parts[2]}


def _code_only(text: str) -> str:
    """Strip comments — these assertions are about behaviour, not prose.

    Without this, documenting a removed footgun in a comment re-triggers the check
    that forbids it.
    """
    return "\n".join(re.sub(r"(?<!\S)#.*$", "", ln) for ln in text.splitlines())


@pytest.mark.parametrize("script", SCRIPTS)
def test_every_entry_has_three_fields(script: str) -> None:
    for entry in _service_entries(_script(script)):
        assert entry.count(":") == 2, f"{script}: {entry!r} is not name:port:probe-path"


@pytest.mark.parametrize("script", SCRIPTS)
def test_ports_are_numeric(script: str, tmp_path: Path) -> None:
    """The regression guard. Under `${svc##*:}` the port comes back as `/health`."""
    text = _script(script)
    block = _parse_block(text)
    for entry in _service_entries(text):
        got = _split_via_bash(block, entry, tmp_path)
        assert got["port"].isdigit(), (
            f"{script}: {entry!r} split to port={got['port']!r} — the extraction is "
            f"returning a non-port. `${{svc##*:}}` on a three-field entry yields the "
            f"probe path; split by position instead."
        )
        assert got["port"] in EXPECTED_PORTS, (
            f"{script}: {entry!r} probes port {got['port']}, which docker-compose.yml "
            f"does not publish"
        )


@pytest.mark.parametrize("script", SCRIPTS)
def test_probe_paths_are_absolute(script: str, tmp_path: Path) -> None:
    text = _script(script)
    block = _parse_block(text)
    for entry in _service_entries(text):
        got = _split_via_bash(block, entry, tmp_path)
        assert got["probe"].startswith("/"), f"{script}: {entry!r} -> probe={got['probe']!r}"


@pytest.mark.parametrize("script", SCRIPTS)
def test_names_survive_the_split(script: str, tmp_path: Path) -> None:
    """Names contain spaces ("Cortex API"); they must not be truncated or absorb a field."""
    text = _script(script)
    block = _parse_block(text)
    for entry in _service_entries(text):
        got = _split_via_bash(block, entry, tmp_path)
        assert got["name"] == entry.split(":")[0]
        assert ":" not in got["name"]


@pytest.mark.parametrize("script", SCRIPTS)
def test_no_tcp_only_liveness_fallback(script: str) -> None:
    """`</dev/tcp/host/port` is satisfied by any listening socket.

    update.sh used it as a last-resort fallback, so an nginx that was up and 500ing on
    every request — precisely what a missing .htpasswd produces — reported [OK] and the
    update was declared successful. A liveness probe that cannot observe the response
    cannot distinguish serving from listening.
    """
    assert "/dev/tcp/" not in _code_only(_script(script)), (
        f"{script}: TCP-connect fallback reintroduced — it passes for a service that "
        f"accepts connections and then errors on every request"
    )


@pytest.mark.parametrize("script", SCRIPTS)
def test_accepts_401_and_405(script: str) -> None:
    """Both are proof of life, and rejecting either produces a permanent false timeout.

    401: nginx serving the dashboard behind basic auth answers every unauthenticated
    request this way, including the probe.
    405: cortex-mcp mounts /mcp and no /health at all; GET /mcp is method-not-allowed,
    which proves the route is mounted.
    """
    text = _script(script)
    m = re.search(r"case \"\$code\" in(.*?)esac", text, re.S)
    assert m, f"{script}: no status-code case block — is it still using `curl -sf`?"
    accepted = m.group(1)
    assert "401" in accepted, f"{script}: 401 not accepted; the dashboard probe will never pass"
    assert "405" in accepted, f"{script}: 405 not accepted; the cortex-mcp probe will never pass"
