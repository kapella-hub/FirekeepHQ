"""Provisioning must fail loudly, and its known exposure must stay documented.

What was tried and reverted
---------------------------
`install.sh` provisions with ``uv venv "${VENV}" --clear``, which DELETES the live
venv and takes 30-120s to repopulate it. Every lifecycle hook execs
``${VENV}/bin/python``, so for that window they fail with "No such file or
directory" -- and background auto-update is on by default, so nobody asked for the
window to open.

The obvious fix -- provision into ``${VENV}.new`` and rename it over the live tree
-- was implemented and **reverted**, because **a venv is not relocatable**. ``uv
venv`` bakes its own absolute path into ``pyvenv.cfg`` and into every console
script's interpreter line, so after the rename the scripts still point at
``${VENV}.new/bin/python``. The e2e bootstrap gate caught it precisely::

    install.sh: 267: .../venv/bin/firekeep: not found   (exit 127)

That is worth a test file of its own, because the staged approach reads as
strictly better and will be proposed again.

What is guarded here
--------------------
1. Provisioning is in place, and any future staged attempt must not reintroduce a
   plain rename.
2. The install is checked for a runnable ``firekeep`` BEFORE the wizard hand-off,
   so a bad wheel produces a diagnosis instead of exit 127 from a later line.
3. The residual exposure stays DOCUMENTED. An undocumented known hazard is how
   the original "POSIX unlink is safe" rationale survived long enough to be
   believed.
"""
from __future__ import annotations

from pathlib import Path

BOOTSTRAP = Path(__file__).resolve().parents[1] / "bootstrap" / "install.sh"
SRC = BOOTSTRAP.read_text(encoding="utf-8")


def _code_lines() -> list[str]:
    return [ln.strip() for ln in SRC.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


class TestProvisioningIsInPlace:
    def test_uv_venv_targets_the_live_venv(self):
        """In place, deliberately. See the module docstring for why the staged
        alternative was reverted rather than merely not attempted."""
        venvs = [ln for ln in _code_lines() if " venv " in ln and "uv" in ln]
        assert venvs, "no `uv venv` invocation found"
        for ln in venvs:
            assert '"${VENV}"' in ln, f"uv venv no longer targets ${{VENV}}: {ln}"

    def test_no_bare_rename_of_a_provisioned_venv(self):
        """The reverted approach, blocked by name. A venv moved with `mv` keeps the
        old absolute path in pyvenv.cfg and in every console script, so the tree
        lands in place and its executables are all broken."""
        bad = [ln for ln in _code_lines()
               if ln.startswith("mv ") and "${VENV}" in ln and ".old." not in ln]
        assert not bad, (
            "a provisioned venv is being renamed. A venv is NOT relocatable -- uv "
            "bakes its absolute path into pyvenv.cfg and every console script, so "
            "the renamed tree's executables point at a directory that no longer "
            "exists (observed: `venv/bin/firekeep: not found`, exit 127). Use "
            "`uv venv --relocatable` or rewrite the scripts if this is retried:\n  "
            + "\n  ".join(bad)
        )


class TestTheInstallIsCheckedBeforeTheHandoff:
    def test_a_runnable_firekeep_is_verified(self):
        assert 'if [ ! -x "${FIREKEEP_BIN}" ]; then' in SRC, (
            "nothing verifies the install produced a runnable firekeep, so a bad "
            "wheel surfaces as exit 127 from the wizard hand-off instead of a "
            "diagnosis at the point of failure"
        )

    def test_the_check_precedes_the_handoff(self):
        check = SRC.find('if [ ! -x "${FIREKEEP_BIN}" ]; then')
        handoff = SRC.find("# --- 8. hand off to the wizard")
        assert check != -1 and handoff != -1
        assert check < handoff, "the usability check runs after the hand-off it protects"


class TestTheExposureStaysDocumented:
    """The original bug survived because a comment asserted it was safe. A known
    hazard with no comment is the same failure one step earlier."""

    def test_the_window_is_described(self):
        low = SRC.lower()
        assert "--clear" in SRC and ("does not exist" in low or "no such file" in low), (
            "the provisioning comment no longer explains that ${VENV} is absent "
            "during the install and that hooks fail for the duration"
        )

    def test_the_relocatability_finding_is_recorded(self):
        """Checks the EXPLANATION, not the word.

        Written first as `assert "relocatable" in SRC.lower()`, which mutation
        testing exposed as a non-check: the word also appears in the suggested
        remedy (`uv venv --relocatable`), so gutting the finding itself left the
        test green. The claim is what matters, and the mechanism that makes it
        true -- an absolute path baked into the venv."""
        low = SRC.lower()
        assert "not relocatable" in low, (
            "the finding that a venv cannot be renamed is no longer stated, so the "
            "staged approach will be proposed again and re-broken"
        )
        assert "pyvenv.cfg" in low, (
            "the MECHANISM is unstated. Without it the finding reads as a hunch, "
            "and the next person reasonably retries the rename."
        )

    def test_the_fail_open_mitigation_is_stated(self):
        """Why the exposure is tolerable, not merely that it exists."""
        assert "fail OPEN" in SRC or "fail open" in SRC.lower(), (
            "the comment does not say that hook cores fail open, which is the "
            "reason this window costs telemetry rather than a broken session"
        )
