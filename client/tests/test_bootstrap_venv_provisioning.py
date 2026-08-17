"""Side-by-side venvs: provision at the FINAL path, flip only the alias.

The constraint, learned twice
-----------------------------
**A venv is not relocatable.** ``uv venv`` bakes its own absolute path into
``pyvenv.cfg`` and into every console script's interpreter line. 0.1.26 tried
build-beside-and-rename (provision ``${VENV}.new``, ``mv`` it over the live
tree) because it read as strictly better than the in-place ``--clear`` rebuild
— and the e2e bootstrap gate killed it precisely::

    install.sh: 267: .../venv/bin/firekeep: not found   (exit 127)

The renamed tree's executables still pointed at ``${VENV}.new/bin/python``,
which no longer existed. The in-place rebuild it reverted to had its own cost:
a 30-120s window where ``~/.firekeep/venv`` did not exist at all and every
lifecycle hook on every live session failed with "No such file or directory".

The side-by-side layout (0.1.35) is the design that satisfies the constraint
instead of fighting it: each version's venv is provisioned AT its final path
``venvs/<version>`` and **never moved**, so every baked absolute path stays
true forever; the only thing that ever changes is the ``current`` alias
(POSIX symlink flipped atomically via ``os.replace``; Windows NTFS junction
flipped via ``cmd /c rmdir`` + ``New-Item -ItemType Junction``).

What is guarded here
--------------------
1. ``uv venv`` targets the final versioned path in BOTH scripts, and a
   provisioned venv is never renamed/moved to a different name it would then
   be used under. The GC's rename-to-``.gc``-then-delete is explicitly
   allowed: it only ever renames venvs being DESTROYED, where the baked paths
   are about to stop existing anyway — the rename is a liveness probe, not a
   relocation.
2. The flip primitive is the ALIAS, not the venv: ``os.replace`` via the
   target venv's own python on POSIX (atomic — no window with no ``current``),
   junction rmdir+recreate on Windows (a ~ms window; hooks fail open).
3. install.ps1 never runs ``Remove-Item -Recurse`` against ``$Current``: a
   recursive delete that follows the reparse point guts the TARGET venv
   (ancient 5.1 builds recursed into it — probed live). Only ``.gc`` corpses
   may be recursively deleted.
4. The flip happens only AFTER every bundled wheel (client + symdex + docdex)
   is verified and installed, so an install that dies early leaves ``current``
   — and every live session — exactly as it was.
5. The constraint stays DOCUMENTED in the scripts. The original "POSIX unlink
   is safe" rationale survived long enough to be believed because nothing
   recorded why it was wrong; the rename design reads as strictly better and
   WILL be proposed again.
"""
from __future__ import annotations

from pathlib import Path

BOOT = Path(__file__).resolve().parents[1] / "bootstrap"
SH = (BOOT / "install.sh").read_text(encoding="utf-8")
PS1 = (BOOT / "install.ps1").read_text(encoding="utf-8")


def _sh_code_lines() -> list[str]:
    return [ln.strip() for ln in SH.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _ps1_code_lines() -> list[str]:
    return [ln.strip() for ln in PS1.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


class TestProvisioningIsAtTheFinalVersionedPath:
    def test_sh_uv_venv_targets_the_versioned_path(self):
        """The venv is created where it will live forever. Any staging path here
        reintroduces the rename design the module docstring documents as broken."""
        venvs = [ln for ln in _sh_code_lines() if "uv" in ln and " venv " in ln]
        assert venvs, "install.sh: no `uv venv` invocation found"
        for ln in venvs:
            assert '"${TARGET_VENV}"' in ln, (
                f"install.sh: uv venv no longer targets the final versioned path "
                f"${{TARGET_VENV}} (venvs/<V>): {ln}"
            )

    def test_ps1_uv_venv_targets_the_versioned_path(self):
        venvs = [ln for ln in _ps1_code_lines() if "& $Uv venv" in ln]
        assert venvs, "install.ps1: no `uv venv` invocation found"
        for ln in venvs:
            assert "$TargetVenv" in ln, (
                f"install.ps1: uv venv no longer targets the final versioned path "
                f"$TargetVenv (venvs\\<V>): {ln}"
            )

    def test_sh_never_renames_a_venv_it_will_use(self):
        """A venv moved with `mv` keeps the OLD absolute path in pyvenv.cfg and in
        every console script, so the tree lands in place with all its executables
        broken (observed: `venv/bin/firekeep: not found`, exit 127). The ONLY
        rename a venv may undergo is the GC's rename-to-.gc probe, which renames
        venvs being DESTROYED — never one that will be used afterwards."""
        venv_tokens = ('"${TARGET_VENV}"', '"${VENVS}"', '"${LEGACY_VENV}"', '"${dir}"')
        bad = [ln for ln in _sh_code_lines()
               if "mv " in ln and any(tok in ln for tok in venv_tokens)
               and ".gc" not in ln]
        assert not bad, (
            "a venv is being renamed to a name it would then be USED under. A venv "
            "is NOT relocatable — uv bakes its absolute path into pyvenv.cfg and "
            "every console script. Only the GC's rename-to-.gc-then-delete probe "
            "may rename a venv, because that venv is being destroyed:\n  "
            + "\n  ".join(bad)
        )
        # The venv being installed is never an mv source at all — not even to .gc:
        # GC excludes it by name, and everything else routes through the alias.
        assert 'mv "${TARGET_VENV}"' not in SH, (
            "install.sh moves the venv it just provisioned — the 0.1.26 rename "
            "design, reverted because a venv is not relocatable"
        )

    def test_ps1_never_renames_a_venv_it_will_use(self):
        """Same invariant, PowerShell primitives: Rename-Item may only produce the
        GC's .gc probe name, and Move-Item must never touch a venv or the alias."""
        renames = [ln for ln in _ps1_code_lines() if "Rename-Item" in ln]
        for ln in renames:
            assert "$Probe" in ln, (
                f"install.ps1: Rename-Item on something other than the GC's .gc "
                f"probe — a renamed venv's baked paths all break: {ln}"
            )
        venv_tokens = ("$TargetVenv", "$Venvs", "$Current", "$LegacyVenv", "$Dir")
        bad = [ln for ln in _ps1_code_lines()
               if "Move-Item" in ln and any(tok in ln for tok in venv_tokens)]
        assert not bad, (
            "install.ps1 Move-Item touches a venv path or the current alias — a "
            "venv is not relocatable and the alias is flipped via rmdir+Junction, "
            "never moved:\n  " + "\n  ".join(bad)
        )


class TestTheFlipIsTheAliasNotTheVenv:
    def test_sh_flip_is_an_atomic_os_replace(self):
        """POSIX: symlink to a temp name, then rename(2) over the old link via the
        target venv's own python. `mv` is NOT usable — POSIX mv follows a
        symlink-to-directory destination and moves the temp link INSIDE the venv
        (GNU mv -T fixes that; macOS has no -T). os.replace is rename(2), so
        there is never a moment with no `current` at all."""
        assert "point_current()" in SH, "install.sh lost its point_current helper"
        assert 'ln -s "$1" "${tmp}"' in SH, (
            "the flip no longer stages the new symlink at a temp name — a direct "
            "`ln -sf` unlinks then re-links, opening a no-current window"
        )
        assert "import os, sys; os.replace(sys.argv[1], sys.argv[2])" in SH, (
            "the flip no longer renames atomically via os.replace — mv follows a "
            "symlink-to-directory destination and cannot replace the link"
        )
        assert not [ln for ln in _sh_code_lines()
                    if "mv " in ln and '"${CURRENT}"' in ln], (
            "install.sh flips `current` with mv — on macOS (no mv -T) that moves "
            "the temp link INSIDE the target venv instead of replacing the alias"
        )

    def test_ps1_flip_is_junction_rmdir_plus_new_item(self):
        """Windows: NTFS junction (works non-elevated, unlike a directory symlink),
        removed via `cmd /c rmdir` — the one primitive that deletes exactly the
        LINK NODE on every PowerShell build — then recreated at the new target."""
        assert "function Set-CurrentJunction" in PS1, (
            "install.ps1 lost its Set-CurrentJunction helper"
        )
        assert "New-Item -ItemType Junction" in PS1, (
            "the current alias is no longer an NTFS junction — a directory "
            "symlink needs admin/developer mode and fails for real teammates"
        )
        assert 'cmd /c rmdir "$Current"' in PS1, (
            "the old link node is no longer removed with `cmd /c rmdir` — the "
            "only removal primitive that is link-node-only on every PS build"
        )

    def test_ps1_never_recursively_deletes_the_current_link(self):
        """Probed live: Remove-Item -Recurse on current PS builds happens to stop
        at the reparse point, but ancient 5.1 builds recursed THROUGH it and
        deleted the target venv's files. The alias must only ever be removed
        with `cmd /c rmdir`; recursive deletes are reserved for .gc corpses."""
        bad = [ln for ln in _ps1_code_lines()
               if "Remove-Item" in ln and "$Current" in ln]
        assert not bad, (
            "install.ps1 runs Remove-Item against the `current` junction — on "
            "old 5.1 builds that recurses through the reparse point and guts the "
            "target venv:\n  " + "\n  ".join(bad)
        )
        recursive = [ln for ln in _ps1_code_lines()
                     if "Remove-Item" in ln and "-Recurse" in ln]
        assert recursive, "install.ps1: the GC's delete step is missing"
        for ln in recursive:
            assert "$Probe" in ln, (
                f"install.ps1: Remove-Item -Recurse on something other than a "
                f"renamed .gc corpse — a held venv dies mid-delete and is left "
                f"gutted under its real name: {ln}"
            )


class TestTheFlipHappensOnlyAfterTheWheels:
    """An install that dies in any earlier step must leave `current` — and every
    live session resolving through it — exactly as it was. Text-order assertions,
    same style as the verify-before-venv orderings in test_bootstrap_ps1.py.
    Both scripts also have an EARLIER flip on the idempotent fast path (no wheels
    are installed there — the venv already passed its health probe), so these
    anchor on the LAST flip, the one that concludes a full provision. `rindex`
    on the install line is what keeps them honest as bundled wheels are added:
    the flip must follow the LAST install, not merely the first."""

    def test_sh_flip_follows_every_wheel_install(self):
        flip = SH.rindex('point_current "${TARGET_VENV}"')
        last_install = SH.rindex("pip install")
        assert last_install < flip, (
            "install.sh flips `current` before the wheels are installed — a "
            "half-provisioned venv must never become what sessions launch"
        )

    def test_sh_runnable_check_sits_between_the_wheels_and_the_flip(self):
        """Provisioning can succeed while producing something unusable (wrong-
        platform wheel, truncated interpreter). Checking at the venv's REAL path
        BEFORE the flip converts a later exit 127 into a diagnosis at the point
        of failure — and keeps a broken build from ever becoming `current`."""
        check = SH.index('if [ ! -x "${TARGET_VENV}/bin/firekeep" ]; then')
        flip = SH.rindex('point_current "${TARGET_VENV}"')
        last_install = SH.rindex("pip install")
        assert last_install < check < flip, (
            "the runnable-firekeep check must run after the wheel installs and "
            "before the flip it protects"
        )

    def test_sh_flip_precedes_the_wizard_handoff(self):
        flip = SH.rindex('point_current "${TARGET_VENV}"')
        handoff = SH.index("# --- 8. hand off to the wizard")
        assert flip < handoff, (
            "the wizard runs through ${CURRENT}/bin/firekeep — flipping after "
            "the hand-off would hand off to the OLD version (or to nothing)"
        )

    def test_ps1_flip_follows_every_wheel_install(self):
        flip = PS1.rindex("Set-CurrentJunction $TargetVenv")
        last_install = PS1.rindex("pip install")
        assert last_install < flip, (
            "install.ps1 flips `current` before the wheels are installed — a "
            "half-provisioned venv must never become what sessions launch"
        )

    def test_ps1_runnable_check_sits_between_the_wheels_and_the_flip(self):
        """install.ps1 parity with the sh check above: a wheel that installs but
        provides no exe must be diagnosed at the point of failure, never flipped
        into `current` to surface as a confusing wizard hand-off error. (This
        check was missing on the ps1 side when the layout first landed — the sh
        ordering existed and the mirror did not.)"""
        check = PS1.index("Join-Path $TargetVenv 'Scripts\\firekeep.exe'")
        flip = PS1.rindex("Set-CurrentJunction $TargetVenv")
        last_install = PS1.rindex("pip install")
        assert last_install < check < flip, (
            "the runnable-firekeep.exe check must run after the wheel installs "
            "and before the junction flip it protects"
        )


class TestTheConstraintStaysDocumented:
    """The original 'POSIX unlink is safe' rationale survived long enough to be
    believed because nothing recorded why it was wrong. The rename design reads
    as strictly better and will be proposed again; the scripts must carry the
    finding, its mechanism, and the residual window."""

    def test_the_relocatability_finding_is_recorded_in_both_scripts(self):
        """Checks the EXPLANATION, not just a word. The predecessor of this test
        was once gutted to `assert "relocatable" in src`, which mutation testing
        exposed as a non-check (the word also appears in remedy suggestions).
        The claim AND the mechanism that makes it true must both be stated."""
        for name, text in (("install.sh", SH), ("install.ps1", PS1)):
            low = text.lower()
            assert "not relocatable" in low, (
                f"{name}: the finding that a venv cannot be moved is no longer "
                "stated, so the rename design will be proposed again and re-broken"
            )
            assert "pyvenv.cfg" in low, (
                f"{name}: the MECHANISM is unstated. Without it the finding reads "
                "as a hunch, and the next person reasonably retries the rename."
            )

    def test_sh_keeps_the_exit_127_evidence(self):
        """The measured failure is the part that convinces: the e2e gate's exact
        error line is what stops 'surely a rename is fine' at code review."""
        assert "not found" in SH and "127" in SH, (
            "install.sh no longer records how the 0.1.26 rename attempt actually "
            "failed (`.../venv/bin/firekeep: not found`, exit 127)"
        )

    def test_sh_documents_the_atomic_no_window_property(self):
        """POSIX has NO exposure window by construction (os.replace is rename(2)).
        That property is load-bearing — it is why install.sh needs no fail-open
        caveat — and must be stated where the flip lives."""
        assert "no window" in SH.lower(), (
            "install.sh no longer states that the symlink flip leaves no moment "
            "with no `current` — the property that retired the 30-120s hook outage"
        )

    def test_ps1_documents_the_flip_window_and_that_hooks_fail_open(self):
        """Windows keeps a real (~ms) window: rmdir + mklink is two ops. Why that
        is tolerable — hooks fail open, retried spawns succeed — must be stated,
        or the next reader 'fixes' it with a recursive delete or a rename."""
        low = PS1.lower()
        assert "window" in low, (
            "install.ps1 no longer documents the two-op flip window"
        )
        assert "fail open" in low, (
            "install.ps1 does not say that hooks fail open, which is the reason "
            "the flip window costs a retried spawn rather than a broken session"
        )


class TestReviewHardening:
    """Guards added by the 0.1.35 pre-release adversarial review — each one is a
    confirmed failure mode of the first side-by-side draft, not a hypothetical."""

    def test_all_downloads_precede_the_provision_in_both_scripts(self):
        """When venvs/<V> is the venv `current` points at (forced reinstall or
        repair of the running version), --clear destroys the selected venv — so
        every network fetch and every checksum must complete FIRST. The first
        draft fetched the symdex wheel AFTER provisioning: a download failure
        there stranded `current` on a gutted venv with nothing to fall back to.
        Docdex is bundled the same way and inherits the same ordering — one
        bundled wheel fetched below the clear is enough to reopen the hole."""
        sh_provision = SH.index('uv" venv "${TARGET_VENV}"')
        for wheel in ("symdex", "docdex"):
            assert SH.index(f'fetch "${{VBASE}}/${{{wheel}_wheel}}"') < sh_provision, (
                f"install.sh fetches the {wheel} wheel after `uv venv --clear` — a "
                "network failure would strand `current` on a gutted venv"
            )
        ps_provision = PS1.index("venv $TargetVenv --python $PythonVersion")
        for wheel in ("Symdex", "Docdex"):
            fetch = f'Invoke-WebRequest -UseBasicParsing -Uri "$VBase/${wheel}Wheel"'
            assert PS1.index(fetch) < ps_provision, (
                f"install.ps1 fetches the {wheel.lower()} wheel after `uv venv --clear` "
                "— a network failure would strand `current` on a gutted venv"
            )

    def test_ps1_in_use_guard_excludes_its_own_ancestry(self):
        """`firekeep update` WAITS on the bootstrap (foreground child), so the
        parent firekeep.exe — running from the very venv being force-reinstalled,
        via `current` — is always alive during the holder scan. Without ancestry
        exclusion the guard refuses blaming '1x firekeep' sessions that do not
        exist, and repairing the running version becomes impossible on Windows.
        (The old detached spawn excluded the updater by exiting first; the
        waiting design must exclude it by PID.)"""
        assert "$SelfChain" in PS1 and "ParentProcessId" in PS1, (
            "install.ps1's in-use guard no longer walks and excludes its own "
            "process ancestry — the waiting `firekeep update` parent will be "
            "counted as a holder of the venv it is trying to reinstall"
        )
        assert PS1.index("$SelfChain") < PS1.index("$Holders = @(Get-Process"), (
            "the ancestry walk must happen before the holder scan that uses it"
        )

    def test_ps1_holder_prefixes_carry_a_trailing_separator(self):
        r"""StartsWith('...\venvs\0.1.3') also matches processes under
        venvs\0.1.35 — the prefix must end with a separator to match only the
        venv actually being cleared."""
        assert '@("$TargetVenv\\")' in PS1, (
            "install.ps1's holder prefix lost its trailing backslash — "
            "venvs\0.1.3 would match venvs\0.1.35's processes"
        )

    def test_sh_gc_gates_every_candidate_on_liveness(self):
        """POSIX rename succeeds while a directory is held (the rename-probe is
        Windows physics), so the lsof gate must cover VERSIONED venvs too — a
        session alive across two updates still needs its venv for gateway
        backend respawns and its pinned sys.path. And lsof's exit contract is
        ambiguous (nonzero = none-found AND lsof-failed alike): only exit 1
        with empty stdout AND empty stderr may be read as 'unheld'; anything
        else keeps the venv. Reading lsof failure as 'unheld' is how a live
        session's venv gets deleted under it."""
        assert 'if venv_in_use "${dir}"' in SH, (
            "install.sh's versioned-venv GC lost its liveness gate"
        )
        assert 'venv_in_use "${LEGACY_VENV}"' in SH, (
            "install.sh's legacy-venv GC lost its liveness gate"
        )
        # The unambiguous-lsof contract, all three legs.
        assert '[ -n "${lsof_out}" ] && return 0' in SH
        assert '[ "${lsof_rc}" -eq 1 ] && [ -z "${lsof_errtext}" ] && return 1' in SH
        assert "command -v lsof >/dev/null 2>&1 || return 0" in SH, (
            "no lsof must mean KEEP (venvs are cheap, broken live sessions are not)"
        )

    def test_legacy_gc_requires_a_full_render_in_both_scripts(self):
        """With FIREKEEP_RUNTIME set the wizard re-renders ONE adapter; the
        other three runtimes' configs still embed absolute ~/.firekeep/venv
        paths, so deleting the legacy venv on a targeted render breaks every
        runtime the render did not touch."""
        assert '[ -d "${LEGACY_VENV}" ] && [ -z "${FIREKEEP_RUNTIME:-}" ]' in SH, (
            "install.sh GCs the legacy venv even on a single-runtime render"
        )
        assert "if (-not $env:FIREKEEP_RUNTIME) {" in PS1, (
            "install.ps1 GCs the legacy venv even on a single-runtime render"
        )
