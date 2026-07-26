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


def test_runtime_is_conditional_never_hardcoded_all():
    """FIREKEEP_RUNTIME is forwarded as --runtime ONLY when set; an UNSET runtime must reach the
    wizard so it can prompt "Install for which agent?". So neither script may hardcode
    `--runtime all` or default the env to 'all'."""
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
