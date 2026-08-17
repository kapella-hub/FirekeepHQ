"""Docdex is bundled exactly like symdex: fetched to a local file, checksum-verified
against the release SHA256SUMS, then installed BY PATH.

Twin of test_bootstrap_symdex.py, and deliberately a separate file: the two wheels
version independently and a future change that drops one must fail on its own test
rather than quietly weakening a shared one. `pip install firekeep-docdex` by NAME is
the thing these forbid — that name may belong to a third party on PyPI, and
`uv pip install <url>` does no hash checking at all (the hole C2 lived in).
"""
from pathlib import Path

BOOT = Path(__file__).resolve().parent.parent / "bootstrap"


def test_install_sh_installs_docdex_wheel_by_path_not_name():
    sh = (BOOT / "install.sh").read_text()
    assert "firekeep_docdex-" in sh                       # reads the wheel name from SHA256SUMS
    assert "verify_against_sums" in sh and sh.count("uv") and "pip install" in sh
    assert "pip install firekeep-docdex" not in sh         # NEVER by name


def test_install_ps1_installs_docdex_wheel_by_path_not_name():
    ps = (BOOT / "install.ps1").read_text()
    assert "firekeep_docdex-" in ps
    assert "Verify-AgainstSums" in ps and "pip install" in ps
    assert "pip install firekeep-docdex" not in ps


def test_both_bootstraps_verify_the_docdex_wheel_before_installing_it():
    """The wheel becomes code that runs on this machine — it must go through the SAME
    verifier as uv and the client wheel, on a LOCAL file, before `uv pip install` sees it."""
    # The install expression is the COMBINED one-resolution form deliberately:
    # a separate `pip install docdex.whl` step re-resolves docdex's
    # `firekeep-client>=…` dependency from the INDEX under --reinstall and
    # silently replaces the local client wheel with PyPI's newest (the 1.0.0
    # release shipped 0.1.48 that way). Asserting the combined line here keeps
    # anyone from "simplifying" it back into three steps.
    sh = (BOOT / "install.sh").read_text()
    assert 'verify_against_sums "${BIN}/${docdex_wheel}" "${docdex_wheel}"' in sh
    combined_sh = ('pip install --python "${TARGET_VENV}" --reinstall '
                   '"${BIN}/${wheel_name}" "${BIN}/${symdex_wheel}" "${BIN}/${docdex_wheel}"')
    assert combined_sh in sh
    assert sh.index('verify_against_sums "${BIN}/${docdex_wheel}"') < sh.rindex(combined_sh)
    ps = (BOOT / "install.ps1").read_text()
    assert "Verify-AgainstSums $DocdexPath $DocdexWheel" in ps
    combined_ps = ("& $Uv pip install --python $TargetVenv --reinstall "
                   "$WheelPath $SymdexPath $DocdexPath")
    assert combined_ps in ps
    assert ps.index("Verify-AgainstSums $DocdexPath $DocdexWheel") < ps.rindex(combined_ps)


def test_both_bootstraps_die_when_the_release_lists_no_docdex_wheel():
    """A release without a docdex wheel is one the installer cannot complete — the
    bootstrap reads the exact bundled name out of SHA256SUMS, so a missing entry must
    be named as an incomplete release, never silently skipped into a half-kit."""
    for name in ("install.sh", "install.ps1"):
        text = (BOOT / name).read_text()
        incomplete = [
            line for line in text.splitlines()
            if "release is incomplete" in line and "firekeep_docdex" in line
        ]
        assert incomplete, f"{name}: a docdex-less release must die, loudly"


def test_the_completeness_probe_covers_docdex_in_both_bootstraps():
    """The fast path's health probe must prove the venv is COMPLETE. An install killed
    between the client wheel and the docdex wheel leaves a venv whose python happily
    reports the target version; accepting it flips `current` to a half-installed venv
    AND keeps taking the fast path forever, so nothing ever routes back through the
    full provision that repairs it. Every always-installed wheel belongs in the probe."""
    sh = (BOOT / "install.sh").read_text()
    assert "import firekeep_client, firekeep_symdex, firekeep_docdex" in sh
    ps = (BOOT / "install.ps1").read_text()
    assert "import firekeep_client, firekeep_symdex, firekeep_docdex" in ps
