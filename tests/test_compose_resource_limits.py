"""A cpus limit above the host's core count does not degrade — it fails the install.

Why this file exists
--------------------
The first ever run of the stranger-install smoke test died before a single
service started, on a stock 2-core GitHub runner:

    Container firekeephq-ollama-1  Error response from daemon:
      range of CPUs is from 0.01 to 2.00, as there are only 2 CPUs available

`docker-compose.yml` declared `cpus: "4.0"` for ollama. Every image had already
built and the volumes were created; the run failed at container CREATION.

The trap is that CPU and memory limits behave differently, and only one of them
is fatal:

  * `memory:` above host RAM is ACCEPTED. Docker treats it as a cap and
    over-subscribes happily — which is why 14.4 GB of declared memory limits
    sailed through on a 7.8 GiB runner and nobody noticed for months.
  * `cpus:` above the host core count is REFUSED at create time. The stack does
    not start at all.

So a value that looks merely optimistic is actually a hard install blocker on
any host smaller than the largest single limit. Two cores is the floor worth
supporting — it is the default size of a great many budget VPSes, which is
exactly the buyer profile for self-hosted software.

Note this is a per-container check, not a budget: Docker never compares the SUM
of limits against the host, so the ~8 cores this file permits in aggregate is
fine. Only the largest single value matters.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = ["docker-compose.yml", "docker-compose.office.yml"]

# The smallest host the shipped stack must install on, in cores.
MIN_SUPPORTED_HOST_CORES = 2.0

_SUBST = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::-(?P<default>[\d.]+))?\}$")


# Compose extends YAML with its own merge tags — `!override` and `!reset` — which
# override files use to replace a key outright instead of merging into it
# (docker-compose.office.yml:64 `ports: !override`). yaml.safe_load raises
# ConstructorError on them, so it cannot read the very files this guard checks.
#
# Strip the tags textually rather than registering a custom constructor: the tags
# govern MERGE semantics only and never change the number being asserted on, and
# this keeps the parse on safe_load. A custom Loader is the wrong tool for reading
# untrusted-shaped config — safe_load's refusal to construct arbitrary Python is a
# property worth keeping even when the input is our own file.
_COMPOSE_TAG = re.compile(r"(?<=:)\s+!(?:override|reset)\b")


def _load_compose(path: Path) -> dict:
    return yaml.safe_load(_COMPOSE_TAG.sub("", path.read_text(encoding="utf-8"))) or {}


def _effective_cpus(raw) -> float | None:
    """The value Docker will actually see with no env set.

    Accepts a literal ("2.0") or a compose substitution with a default
    ("${OLLAMA_CPUS:-2.0}"). A substitution with NO default resolves to empty at
    install time, which compose rejects — so that is reported as unparseable
    rather than silently skipped.
    """
    text = str(raw).strip()
    m = _SUBST.match(text)
    if m:
        return float(m.group("default")) if m.group("default") else None
    try:
        return float(text)
    except ValueError:
        return None


def _limits(compose_path: Path):
    data = _load_compose(compose_path)
    for name, svc in (data.get("services") or {}).items():
        if not isinstance(svc, dict):
            continue
        cpus = (((svc.get("deploy") or {}).get("resources") or {})
                .get("limits") or {}).get("cpus")
        if cpus is not None:
            yield name, cpus


@pytest.mark.parametrize("compose_name", COMPOSE_FILES)
def test_no_service_demands_more_cores_than_the_minimum_host(compose_name):
    path = REPO_ROOT / compose_name
    if not path.is_file():
        pytest.skip(f"{compose_name} not present")

    seen = list(_limits(path))
    # A vacuous pass would make this guard worthless the moment the limits are
    # restructured, which is exactly the failure mode it exists to prevent.
    assert seen, f"no cpus limits found in {compose_name} — has the shape changed?"

    for service, raw in seen:
        effective = _effective_cpus(raw)
        assert effective is not None, (
            f"{compose_name}: {service} cpus={raw!r} does not resolve to a number with no "
            f"env set. A bare ${{VAR}} with no default becomes empty and compose rejects it."
        )
        assert effective <= MIN_SUPPORTED_HOST_CORES, (
            f"{compose_name}: {service} demands {effective} cores, above the "
            f"{MIN_SUPPORTED_HOST_CORES}-core minimum host. Docker REFUSES to create a "
            f"container whose cpus exceeds the host core count, so this fails "
            f"`docker compose up` outright rather than degrading. Lower it, or make it "
            f'configurable with a conforming default: cpus: "${{SERVICE_CPUS:-2.0}}"'
        )


def test_ollama_cores_stay_operator_tunable():
    """ollama is the inference engine under every memory operation, so a bigger
    host must be able to give it more than the conservative default without
    editing the shipped compose file."""
    path = REPO_ROOT / "docker-compose.yml"
    cpus = dict(_limits(path)).get("ollama")
    assert cpus is not None, "ollama has no cpus limit"
    assert _SUBST.match(str(cpus).strip()), (
        f"ollama cpus={cpus!r} is hard-coded. It must stay env-substitutable so an "
        f"operator can raise it on a larger host: cpus: \"${{OLLAMA_CPUS:-2.0}}\""
    )
