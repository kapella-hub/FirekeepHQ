"""The bootstrap's idempotent fast path + conditional FIREKEEP_RUNTIME targeting (both scripts).

Static text assertions: the full bootstrap needs a release + a machine, but the
re-render-when-current and runtime-passthrough contract lives in the script text and
must stay in lock-step across install.sh and install.ps1.
"""
from pathlib import Path

BOOT = Path(__file__).resolve().parent.parent / "bootstrap"


def test_ps1_idempotent_fast_path():
    """Side-by-side venvs: the fast path is a health probe of venvs/<V> ITSELF
    (its own python reports $V), and its action is a flip of the `current`
    junction plus a re-render — zero downloads. One rule covers three cases:
    idempotent re-run, crash-between-flip-and-wizard healing, and instant
    rollback (`firekeep update --to <prev>` while venvs/<prev> survives GC)."""
    ps = (BOOT / "install.ps1").read_text()
    assert "firekeep_client.__version__" in ps          # version-checked skip
    assert "FIREKEEP_FORCE_REINSTALL" in ps             # force-full override
    assert "is already provisioned" in ps               # the healthy-venv skip message
    fast_msg = ps.index("is already provisioned")
    fast_flip = ps.index("Set-CurrentJunction $TargetVenv")
    assert fast_msg < fast_flip, (
        "the fast path's action must be the junction flip — selecting the "
        "already-provisioned venv is what makes re-runs and rollbacks instant"
    )


def test_sh_idempotent_fast_path():
    sh = (BOOT / "install.sh").read_text()
    assert "firekeep_client.__version__" in sh
    assert "FIREKEEP_FORCE_REINSTALL" in sh
    assert "is already provisioned" in sh
    # Fast path AND the post-wheels flip both route through the one helper: the
    # alias is the only thing that ever moves, whichever path an install takes.
    assert sh.count('point_current "${TARGET_VENV}"') == 2


def test_version_probe_is_isolated_from_the_working_directory():
    """The fast path is only safe if it reads the INSTALLED version.

    `python -c` puts the current working directory on sys.path[0]. Probing without
    -I from a checkout's client/ directory imports the SOURCE TREE, and when its
    version happens to equal the release being installed, the fast path skips the
    install entirely and prints "already provisioned" -- a silent no-op reported
    as success. MEASURED: a venv holding 0.1.33 reported 0.1.34 that way, and
    `firekeep update` did nothing twice in a row.

    -I additionally drops PYTHONPATH and user site-packages, the other two ways the
    caller's environment can shadow what is installed.

    Since 0.1.35 the probes live in the shared helpers venv_version() (sh) and
    Get-VenvVersion (ps1) -- one probe per script, reused for install detection,
    the fast-path health check, and the legacy-venv fallback. The ps1 probe line
    invokes `& $Py`, not a literal 'python', so the match here is on the import
    itself; the venv's-own-interpreter property is asserted separately below.
    """
    for name in ("install.sh", "install.ps1"):
        text = (BOOT / name).read_text()
        probes = [line for line in text.splitlines()
                  if "firekeep_client.__version__" in line]
        assert probes, f"{name}: no version probe found"
        for line in probes:
            assert " -I -c " in line, (
                f"{name}: version probe must run python with -I, else the working "
                f"directory can shadow the venv:\n{line.strip()}"
            )
    # The probe must run the venv's OWN interpreter — probing any other python
    # reports some other environment's version and re-opens the silent no-op.
    sh = (BOOT / "install.sh").read_text()
    assert '"$1/bin/python" -I -c' in sh, (
        "install.sh: venv_version() no longer probes through the venv's own python"
    )
    ps = (BOOT / "install.ps1").read_text()
    assert "Join-Path $VenvPath 'Scripts\\python.exe'" in ps, (
        "install.ps1: Get-VenvVersion no longer resolves the venv's own python.exe"
    )


def test_sh_existing_install_handoff_is_non_interactive_even_when_forced():
    """Force reinstall controls rebuilding, not whether existing config is reused.

    Detection probes `current` (the layout's truth since 0.1.35) with the legacy
    single venv as the pre-0.1.35 fallback; the fast path is a separate health
    probe of venvs/<V> itself, so a forced or version-changing full install still
    inherits the non-interactive hand-off decided here."""
    sh = (BOOT / "install.sh").read_text()
    detection = 'installed="$(venv_version "${CURRENT}")"'
    legacy_fallback = '[ -n "${installed}" ] || installed="$(venv_version "${LEGACY_VENV}")"'
    # venv_complete in the condition is load-bearing: an install killed between
    # the client wheel and the symdex wheel leaves a venv whose python happily
    # reports ${V} — without the completeness probe the fast path would flip
    # `current` to that half-installed venv forever, and nothing would ever
    # route back through the full provision that repairs it.
    fast_path = ('if [ "$(venv_version "${TARGET_VENV}")" = "${V}" ] && '
                 'venv_complete "${TARGET_VENV}" && '
                 '[ -z "${FIREKEEP_FORCE_REINSTALL:-}" ]; then')

    assert detection in sh
    assert legacy_fallback in sh, (
        "a pre-0.1.35 install (legacy ~/.firekeep/venv, no `current` yet) must "
        "still be detected as installed, or its update re-prompts for credentials"
    )
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
