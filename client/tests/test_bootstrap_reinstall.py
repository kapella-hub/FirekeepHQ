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
