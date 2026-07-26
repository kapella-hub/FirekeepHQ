"""A CI step that pipes must not be able to swallow a failure.

The defect: `.github/workflows/install-smoke.yml` ran

    printf ... | bash install.sh 2>&1 | tee install.log

with no `shell:` key. GitHub's default shell for a `run:` step on Linux is
`bash -e {0}` — **without** pipefail — so the step's exit status was `tee`'s, which
is always 0. `install.sh` exits 1 on a failed key bootstrap and on a failed health
gate, and none of that would have failed the job. Every assertion after it would
then have run against a half-built stack and reported on the wrong product.

Two comments already in that file asserted "the step shell is `bash -eo pipefail`".
They were aspirational. That is the shape worth guarding: not the pipe, but the gap
between what a workflow claims about its shell and what it selects.

Only `shell: bash` (or an explicit `set -o pipefail`) gets pipefail. Note that
`shell: bash` and the default are NOT the same thing — that is the whole bug.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))

# Shells that enable pipefail when GitHub invokes them.
PIPEFAIL_SHELLS = {"bash", "pwsh", "powershell"}


def _steps():
    for wf in WORKFLOWS:
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        for job_name, job in (doc.get("jobs") or {}).items():
            default_shell = (
                ((job.get("defaults") or {}).get("run") or {}).get("shell")
                or ((doc.get("defaults") or {}).get("run") or {}).get("shell")
            )
            for i, step in enumerate(job.get("steps") or []):
                if not isinstance(step, dict) or not step.get("run"):
                    continue
                yield wf.name, job_name, i, step, (step.get("shell") or default_shell)


_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def _pipes_between_commands(run: str) -> bool:
    """True if `run` pipes one command into another.

    Three things are NOT shell pipes and must not trip this, or the guard cries
    wolf and gets deleted:

    - `||`, the or-else operator.
    - a `|` inside a comment — prose. This repo has tripped its own guards on
      their own explanations three separate times.
    - a `|` inside a quoted string. `grep -nE "<VPS_IP>|YOUR_VPS_IP_HERE"` is
      regex alternation, and the first version of this file flagged five steps
      on that alone. A guard whose failures are mostly false is worse than no
      guard: the fix people reach for is to silence it.

    Quote-stripping is deliberately naive (no escaped-quote or heredoc handling).
    It only ever causes MISSED pipes, never false alarms, which is the safe
    direction for a check whose credibility is the thing being protected.
    """
    for raw in run.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = _QUOTED.sub("", line)
        line = re.sub(r"\|\|", "", line)
        if "|" in line:
            return True
    return False


@pytest.mark.parametrize(
    "wf,job,idx,step,shell",
    [pytest.param(*s, id=f"{s[0]}:{s[1]}:{s[2]}") for s in _steps()],
)
def test_piping_steps_have_pipefail(wf, job, idx, step, shell) -> None:
    run = step["run"]
    if not _pipes_between_commands(run):
        return
    if "set -o pipefail" in run or "$LASTEXITCODE" in run:
        return
    assert shell in PIPEFAIL_SHELLS, (
        f"{wf} job '{job}' step {idx} ({step.get('name', 'unnamed')!r}) pipes between "
        f"commands but selects shell {shell!r}. The Linux default is `bash -e` with NO "
        f"pipefail, so the step's exit status is the LAST command's and an earlier "
        f"failure is silently discarded. Add `shell: bash` or `set -o pipefail`."
    )


def test_a_workflow_claiming_pipefail_actually_selects_it() -> None:
    """Comments asserting the shell must match the shell.

    Both `|| true` guards in install-smoke.yml are justified in-comment by
    "the step shell is `bash -eo pipefail`". If that stops being true the guards
    become cargo cult, and worse, the claim reassures the next reader.
    """
    for wf, job, idx, step, shell in _steps():
        run = step["run"]
        claims = "pipefail" in run and "set -o pipefail" not in run
        if not claims:
            continue
        assert shell in PIPEFAIL_SHELLS, (
            f"{wf} job '{job}' step {idx} says pipefail in a comment but selects "
            f"shell {shell!r}, which does not enable it"
        )


def test_the_install_step_is_covered() -> None:
    """Guard against the guard going vacuous.

    If install-smoke.yml is renamed or its install step stops piping, every
    assertion above passes by having nothing to check. Name the case explicitly.
    """
    found = [
        (wf, job, idx, shell)
        for wf, job, idx, step, shell in _steps()
        if "bash install.sh" in step["run"]
    ]
    assert found, "no workflow step runs `bash install.sh` — did the smoke job move?"
    for wf, job, idx, shell in found:
        assert shell in PIPEFAIL_SHELLS, f"{wf}:{job}:{idx} runs install.sh under {shell!r}"
