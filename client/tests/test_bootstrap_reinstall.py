"""The bootstrap's idempotent fast path + conditional FIREKEEP_RUNTIME targeting (both scripts).

Static text assertions: the full bootstrap needs a release + a machine, but the
re-render-when-current and runtime-passthrough contract lives in the script text and
must stay in lock-step across install.sh and install.ps1.
"""
from pathlib import Path

BOOT = Path(__file__).resolve().parent.parent / "bootstrap"


def test_ps1_idempotent_fast_path():
    ps = (BOOT / "install.ps1").read_text()
    assert "firekeep_client.__version__" in ps          # version-checked skip
    assert "FIREKEEP_FORCE_REINSTALL" in ps             # force-full override
    assert "no venv rebuild" in ps


def test_sh_idempotent_fast_path():
    sh = (BOOT / "install.sh").read_text()
    assert "firekeep_client.__version__" in sh
    assert "FIREKEEP_FORCE_REINSTALL" in sh


def test_version_probe_is_isolated_from_the_working_directory():
    """The fast path is only safe if it reads the INSTALLED version.

    `python -c` puts the current working directory on sys.path[0]. Probing without
    -I from a checkout's client/ directory imports the SOURCE TREE, and when its
    version happens to equal the release being installed, the fast path skips the
    install entirely and prints "already at <V>" -- a silent no-op reported as
    success. MEASURED: a venv holding 0.1.33 reported 0.1.34 that way, and
    `firekeep update` did nothing twice in a row.

    -I additionally drops PYTHONPATH and user site-packages, the other two ways the
    caller's environment can shadow what is installed.
    """
    for name in ("install.sh", "install.ps1"):
        probes = [line for line in (BOOT / name).read_text().splitlines()
                  if "firekeep_client.__version__" in line and "python" in line]
        assert probes, f"{name}: no version probe found"
        for line in probes:
            assert " -I -c " in line, (
                f"{name}: version probe must run python with -I, else the working "
                f"directory can shadow the venv:\n{line.strip()}"
            )


def test_sh_existing_install_handoff_is_non_interactive_even_when_forced():
    """Force reinstall controls rebuilding, not whether existing config is reused."""
    sh = (BOOT / "install.sh").read_text()
    detection = 'if [ -x "${FIREKEEP_BIN}" ]; then'
    fast_path = ('if [ "${installed}" = "${V}" ] && '
                 '[ -z "${FIREKEEP_FORCE_REINSTALL:-}" ]; then')

    assert detection in sh
    assert fast_path in sh
    assert sh.index(detection) < sh.index(fast_path)
    assert '[ -n "${installed}" ] || [ -n "${FIREKEEP_JOIN:-}" ]' in sh
    assert 'NON_INTERACTIVE_ARG=""' in sh
    # Both join/non-join TTY handoffs on the fast and full-install paths.
    assert sh.count("${NON_INTERACTIVE_ARG}") == 4


def test_runtime_override_is_conditional():
    """FIREKEEP_RUNTIME targets a repair/re-render; unset lets the CLI's all-adapter
    default apply. Keep both bootstraps in lock-step without duplicating that default."""
    for name in ("install.sh", "install.ps1"):
        text = (BOOT / name).read_text()
        assert "FIREKEEP_RUNTIME" in text
        assert "--runtime all" not in text
        assert "FIREKEEP_RUNTIME:-all" not in text      # the earlier default-all pattern is gone
    ps = (BOOT / "install.ps1").read_text()
    assert "@RuntimeArgs" in ps                       # splatted (empty when unset)
    assert "@('--runtime', $env:FIREKEEP_RUNTIME)" in ps
    sh = (BOOT / "install.sh").read_text()
    assert 'RUNTIME_ARG="--runtime ${FIREKEEP_RUNTIME}"' in sh
