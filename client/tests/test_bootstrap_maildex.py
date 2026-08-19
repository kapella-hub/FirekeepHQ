"""Maildex is bundled exactly like symdex and docdex: fetched to a local file,
checksum-verified against the release SHA256SUMS, then installed BY PATH.

Twin of test_bootstrap_docdex.py, and deliberately a separate file: the dex wheels
version independently and a future change that drops one must fail on its own test
rather than quietly weakening a shared one. `pip install firekeep-maildex` by NAME is
the thing these forbid — that name may belong to a third party on PyPI, and
`uv pip install <url>` does no hash checking at all (the hole C2 lived in).
"""
from pathlib import Path

BOOT = Path(__file__).resolve().parent.parent / "bootstrap"


def test_install_sh_installs_maildex_wheel_by_path_not_name():
    sh = (BOOT / "install.sh").read_text()
    assert "firekeep_maildex-" in sh                       # reads the wheel name from SHA256SUMS
    assert "verify_against_sums" in sh and sh.count("uv") and "pip install" in sh
    assert "pip install firekeep-maildex" not in sh         # NEVER by name


def test_install_ps1_installs_maildex_wheel_by_path_not_name():
    ps = (BOOT / "install.ps1").read_text()
    assert "firekeep_maildex-" in ps
    assert "Verify-AgainstSums" in ps and "pip install" in ps
    assert "pip install firekeep-maildex" not in ps


def test_both_bootstraps_verify_the_maildex_wheel_before_installing_it():
    """The wheel becomes code that runs on this machine — it must go through the SAME
    verifier as uv and the client wheel, on a LOCAL file, before `uv pip install` sees it."""
    # The install expression is the COMBINED one-resolution form deliberately:
    # a separate `pip install maildex.whl` step re-resolves maildex's
    # `firekeep-client>=…` dependency from the INDEX under --reinstall and
    # silently replaces the local client wheel with PyPI's newest (the 1.0.0
    # release shipped 0.1.48 that way, via docdex). Asserting the combined line
    # here keeps anyone from "simplifying" it back into four steps.
    sh = (BOOT / "install.sh").read_text()
    assert 'verify_against_sums "${BIN}/${maildex_wheel}" "${maildex_wheel}"' in sh
    combined_sh = ('pip install --python "${TARGET_VENV}" --reinstall '
                   '"${BIN}/${wheel_name}" "${BIN}/${symdex_wheel}" "${BIN}/${docdex_wheel}" '
                   '"${BIN}/${maildex_wheel}"')
    assert combined_sh in sh
    assert sh.index('verify_against_sums "${BIN}/${maildex_wheel}"') < sh.rindex(combined_sh)
    ps = (BOOT / "install.ps1").read_text()
    assert "Verify-AgainstSums $MaildexPath $MaildexWheel" in ps
    combined_ps = ("& $Uv pip install --python $TargetVenv --reinstall "
                   "$WheelPath $SymdexPath $DocdexPath $MaildexPath")
    assert combined_ps in ps
    assert ps.index("Verify-AgainstSums $MaildexPath $MaildexWheel") < ps.rindex(combined_ps)


def test_both_bootstraps_die_when_the_release_lists_no_maildex_wheel():
    """A release without a maildex wheel is one the installer cannot complete — the
    bootstrap reads the exact bundled name out of SHA256SUMS, so a missing entry must
    be named as an incomplete release, never silently skipped into a half-kit."""
    for name in ("install.sh", "install.ps1"):
        text = (BOOT / name).read_text()
        incomplete = [
            line for line in text.splitlines()
            if "release is incomplete" in line and "firekeep_maildex" in line
        ]
        assert incomplete, f"{name}: a maildex-less release must die, loudly"


def test_the_completeness_probe_covers_maildex_in_both_bootstraps():
    """The fast path's health probe must prove the venv is COMPLETE. An install killed
    between the client wheel and the maildex wheel leaves a venv whose python happily
    reports the target version; accepting it flips `current` to a half-installed venv
    AND keeps taking the fast path forever, so nothing ever routes back through the
    full provision that repairs it. Every always-installed wheel belongs in the probe."""
    probe = "import firekeep_client, firekeep_symdex, firekeep_docdex, firekeep_maildex"
    sh = (BOOT / "install.sh").read_text()
    assert probe in sh
    ps = (BOOT / "install.ps1").read_text()
    assert probe in ps
