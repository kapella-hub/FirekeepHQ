#!/bin/sh
# Firekeep client bootstrap (macOS / Linux). Fetches uv, provisions a standalone CPython
# into ~/.firekeep/venvs/<version>, installs the client wheel (fetched to a local file and
# checksum-verified against a versioned SHA256SUMS, never installed straight from a URL),
# flips the ~/.firekeep/current symlink to the new venv, then hands off to the wizard.
#
# SIDE-BY-SIDE VENVS (client 0.1.35). Each version's venv is provisioned AT its final
# path venvs/<version> and never moved — a uv venv is not relocatable (pyvenv.cfg and
# every console-script interpreter line bake the absolute path; the 0.1.26 rename
# attempt died on exactly that, see the history note at the provisioning step).
# `current` is a symlink every rendered surface routes through, flipped ATOMICALLY via
# rename(2). Updating therefore never deletes the venv live sessions exec hooks from —
# retiring the 30-120s window where ~/.firekeep/venv did not exist mid-rebuild and
# every lifecycle hook on every live session failed with "No such file or directory".
#
# Requires nothing on the machine but a shell and curl/wget. Deliberately POSIX sh (not
# bash): macOS ships bash 3.2, and Alpine/minimal Linux images ship no bash at all.
set -eu

FIREKEEP_HOME="${HOME}/.firekeep"
VENVS="${FIREKEEP_HOME}/venvs"
CURRENT="${FIREKEEP_HOME}/current"
LEGACY_VENV="${FIREKEEP_HOME}/venv"
BIN="${FIREKEEP_HOME}/bin"
PYTHON_VERSION=3.12

die() { echo "firekeep: $*" >&2; exit 1; }

# Baked at release time by make_release.py --dist-base: the PUBLISHED copy of
# this script carries its own release URL, so the one-liner needs no env var
# (`curl .../install.sh | sh`). The env var still overrides, and the repo copy
# keeps the placeholder so a raw-checkout run fails loudly as before. The probe
# string is split so make_release's token substitution can't rewrite the check.
DIST_BASE_DEFAULT="__FIREKEEP_DIST_BASE_DEFAULT__"
placeholder="__FIREKEEP_DIST_BASE_""DEFAULT__"
if [ -z "${FIREKEEP_DIST_BASE:-}" ] && [ "${DIST_BASE_DEFAULT}" != "${placeholder}" ]; then
    FIREKEEP_DIST_BASE="${DIST_BASE_DEFAULT}"
fi
[ -n "${FIREKEEP_DIST_BASE:-}" ] || die "FIREKEEP_DIST_BASE is not set — this script must be \
fetched from a release (curl -fsSL <base>/latest/install.sh | FIREKEEP_DIST_BASE=<base> sh)"
BASE="${FIREKEEP_DIST_BASE%/}"

# --- TLS trust for corporate networks (real-machine failure, 2026-07-13) -----
# uv trusts its own bundled roots by default; UV_NATIVE_TLS=1 switches every uv
# invocation below (venv provisioning AND both pip installs) to the OS trust store,
# where MDM-managed machines carry the corporate interception CA alongside the public
# roots. A set SSL_CERT_FILE is WORSE than nothing here: rustls treats it as the
# EXCLUSIVE trust store (native store ignored), and the corporate-CA-only files that
# proxy workarounds install break every NON-intercepted host (PyPI). Neutralize it,
# loudly, with an escape hatch for the rare full-bundle case.
export UV_NATIVE_TLS=1
if [ -n "${SSL_CERT_FILE:-}" ] && [ -z "${FIREKEEP_KEEP_SSL_CERT_FILE:-}" ]; then
    echo "firekeep: SSL_CERT_FILE is set; ignoring it for this install and using the OS \
trust store (set FIREKEEP_KEEP_SSL_CERT_FILE=1 to keep it)" >&2
    unset SSL_CERT_FILE
fi

fetch() {
    # $1 = url, $2 = dest. curl first (present on macOS), wget fallback (minimal Linux).
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1" -o "$2" || die "download failed: $1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$2" "$1" || die "download failed: $1"
    else
        die "neither curl nor wget is available"
    fi
}

fetch_opt() {
    # Best-effort fetch: $1 = url, $2 = dest. Returns nonzero (never dies) when the
    # artifact is absent — the .minisig path uses this, because a missing signature is
    # a warning under the current rollout, while a missing REQUIRED artifact stays die().
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1" -o "$2" 2>/dev/null || { rm -f "$2"; return 1; }
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$2" "$1" 2>/dev/null || { rm -f "$2"; return 1; }
    else
        return 1
    fi
}

sha256_of() {
    # Assign, then check: `sha256sum "$1" | cut -f1` would mask a sha256sum failure behind
    # cut's exit 0, yielding an empty digest and a nonsense "expected X, got " message.
    if command -v sha256sum >/dev/null 2>&1; then
        out="$(sha256sum "$1")" || die "sha256sum failed on $1"
    elif command -v shasum >/dev/null 2>&1; then
        out="$(shasum -a 256 "$1")" || die "shasum failed on $1"
    else
        die "no sha256 tool (sha256sum/shasum) — refusing to run an unverified binary"
    fi
    printf '%s' "${out%% *}"
}

# --- side-by-side layout helpers ---------------------------------------------

# Probe the version a venv actually holds ('' when absent/broken).
# -I (isolated) is load-bearing, not tidiness -- see the matching note in
# install.ps1. `python -c` puts the CURRENT WORKING DIRECTORY on sys.path[0], so
# running this from a checkout's client/ directory imports the SOURCE TREE and
# reports its version rather than the venv's. When that misread equals ${V}, the
# fast path below skips the install -- a silent no-op reported as success.
# -I also drops PYTHONPATH and user site-packages.
venv_version() {
    [ -x "$1/bin/python" ] || { echo ""; return 0; }
    "$1/bin/python" -I -c 'import firekeep_client; print(firekeep_client.__version__)' 2>/dev/null || true
}

# The fast path's health probe must prove the venv is COMPLETE, not merely that
# the client package imports. An install killed between the client wheel and
# either dex wheel leaves a venv whose python happily reports ${V} — accepting
# that would flip `current` to a half-installed venv and keep taking the fast
# path on every later run, so the breakage would never route back through the
# full provision that repairs it. bin/firekeep + every bundled package's import
# is the same bar the full path's 7d runnable check enforces before it flips.
# Every wheel this script always installs belongs here; a wheel added to the
# install steps but not to this probe is a half-install the fast path accepts.
venv_complete() {
    [ -x "$1/bin/firekeep" ] || return 1
    "$1/bin/python" -I -c 'import firekeep_client, firekeep_symdex, firekeep_docdex' 2>/dev/null || return 1
    return 0
}

# Liveness gate for GC. Returns 0 (held or unknowable -> KEEP) unless lsof
# gives a DEFINITIVE empty answer. The subtlety is lsof's exit contract:
# nonzero means "no files listed" AND "lsof itself failed" alike — a
# permission-limited /proc or an unstattable subtree exits nonzero having seen
# nothing, and reading that as "unheld" is how a live session's venv gets
# deleted under it. Definitive absence = exit 1 AND empty stdout AND empty
# stderr (-w suppresses routine warnings so surviving stderr is a real error).
# No lsof at all -> keep, forever if need be; venvs are cheap, broken live
# sessions are not.
venv_in_use() {
    command -v lsof >/dev/null 2>&1 || return 0
    lsof_err="${BIN}/.lsof.err.$$"
    lsof_out="$(lsof -w +D "$1" 2>"${lsof_err}")" ; lsof_rc=$?
    lsof_errtext="$(cat "${lsof_err}" 2>/dev/null)"; rm -f "${lsof_err}"
    [ -n "${lsof_out}" ] && return 0
    [ "${lsof_rc}" -eq 1 ] && [ -z "${lsof_errtext}" ] && return 1
    return 0
}

# Point `current` at a venv, ATOMICALLY: symlink to a temp name, then rename(2)
# over the old link via the target venv's own python (os.replace). mv is not
# usable here — POSIX mv follows a symlink-to-directory destination and moves
# the temp link INSIDE the venv instead of replacing the link (GNU mv -T fixes
# that, and macOS has no -T). There is no window with no `current` at all.
point_current() {
    if command -v readlink >/dev/null 2>&1 && [ -L "${CURRENT}" ] \
        && [ "$(readlink "${CURRENT}")" = "$1" ]; then
        return 0  # already pointing there — nothing to flip
    fi
    tmp="${FIREKEEP_HOME}/.current.tmp.$$"
    rm -f "${tmp}"
    ln -s "$1" "${tmp}" || die "could not create the current symlink"
    "$1/bin/python" -I -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' \
        "${tmp}" "${CURRENT}" || { rm -f "${tmp}"; die "could not flip current -> $1"; }
    echo "firekeep: current -> venvs/$(basename "$1")"
}

# Garbage-collect venvs nothing should use anymore: every versioned venv except
# the one being installed and the newest OTHER version (kept as an instant
# rollback target), plus leftovers of interrupted GCs, plus the legacy
# pre-0.1.35 ~/.firekeep/venv. Rename first, then delete: a crash mid-delete
# leaves a .gc dir a future run re-sweeps, never a half-alive venv under its
# real name.
#
# EVERY candidate is lsof-gated on POSIX. Windows gets liveness for free — the
# rename-probe fails while any file beneath is open — but POSIX rename succeeds
# regardless, and a session alive across TWO updates still needs its versioned
# venv: its gateway respawns backends from the RESOLVED real path
# (gateway.py resolves sys.executable) and pin_import_paths froze sys.path
# there. Deleting it breaks that session's next backend respawn and every
# not-yet-loaded import. The keep-newest-other policy bounds accumulation; the
# lsof gate is what makes the delete safe.
#
# The LEGACY venv adds one more constraint: pre-0.1.35 rendered configs exec
# hooks from it by absolute path, so it is only GC-able after a FULL adapter
# re-render — with FIREKEEP_RUNTIME set, the wizard re-rendered ONE runtime and
# the other three still embed ~/.firekeep/venv paths.
remove_stale_venvs() {
    keep_new="$(basename "${TARGET_VENV}")"
    keep_prev=""
    if [ -d "${VENVS}" ]; then
        keep_prev="$(ls -1 "${VENVS}" 2>/dev/null | grep -v "^${keep_new}\$" \
            | grep -v '\.gc$' | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)"
    fi
    if [ -d "${VENVS}" ]; then
        for dir in "${VENVS}"/*; do
            [ -d "${dir}" ] || continue
            name="$(basename "${dir}")"
            [ "${name}" = "${keep_new}" ] && continue
            [ -n "${keep_prev}" ] && [ "${name}" = "${keep_prev}" ] && continue
            case "${name}" in
                *.gc) rm -rf "${dir}" 2>/dev/null || true; continue ;;
            esac
            if venv_in_use "${dir}"; then
                echo "firekeep: kept ${name} - still in use by open agent sessions; a future update will remove it"
                continue
            fi
            if mv "${dir}" "${dir}.gc" 2>/dev/null; then
                rm -rf "${dir}.gc" 2>/dev/null || true
                echo "firekeep: removed old venv ${name}"
            else
                echo "firekeep: kept ${name} - a future update will remove it"
            fi
        done
    fi
    if [ -d "${LEGACY_VENV}" ] && [ -z "${FIREKEEP_RUNTIME:-}" ]; then
        if venv_in_use "${LEGACY_VENV}"; then
            echo "firekeep: kept the legacy ~/.firekeep/venv - sessions opened before this update may still run from it; a future update will remove it"
        elif mv "${LEGACY_VENV}" "${LEGACY_VENV}.gc" 2>/dev/null; then
            rm -rf "${LEGACY_VENV}.gc" 2>/dev/null || true
            echo "firekeep: removed the legacy single venv (~/.firekeep/venv)"
        fi
    fi
    rm -rf "${LEGACY_VENV}.gc" 2>/dev/null || true
}

# --- 1. platform -------------------------------------------------------------
os="$(uname -s)"; arch="$(uname -m)"
# Alpine and other musl distros cannot run the GNU uv binary — the missing
# glibc ELF interpreter surfaces as the misleading "uv: not found" AFTER a
# successful download. musl's ldd identifies itself; the loader-file check
# covers minimal images with no ldd at all.
libc=gnu
if [ "${os}" = "Linux" ]; then
    if command -v ldd >/dev/null 2>&1 && ldd --version 2>&1 | grep -qi musl; then
        libc=musl
    elif [ -e "/lib/ld-musl-${arch}.so.1" ]; then
        libc=musl
    fi
fi
case "${os}-${arch}" in
    Darwin-arm64)  target=aarch64-apple-darwin ;;
    Darwin-x86_64) target=x86_64-apple-darwin ;;
    Linux-x86_64)  target="x86_64-unknown-linux-${libc}" ;;
    Linux-aarch64) target="aarch64-unknown-linux-${libc}" ;;
    *) die "unsupported platform ${os}-${arch}" ;;
esac

mkdir -p "${BIN}"

# --- 2. resolve the version to install ---------------------------------------
# BASE is VERSION-AGNOSTIC: that is what makes latest.json a real moving pointer instead of
# a self-referential one. Every version keeps its own directory under BASE, so a pinned
# FIREKEEP_VERSION (the --to rollback path) still resolves to that version's own artifacts
# rather than 404ing.
if [ -n "${FIREKEEP_VERSION:-}" ]; then
    V="${FIREKEEP_VERSION}"
else
    fetch "${BASE}/latest/latest.json" "${BIN}/latest.json"
    V="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "${BIN}/latest.json")"
    [ -n "${V}" ] || die "latest.json has no version"
fi
VBASE="${BASE}/${V}"
wheel_name="firekeep_client-${V}-py3-none-any.whl"
TARGET_VENV="${VENVS}/${V}"
# FIREKEEP_RUNTIME targets ONE agent (claude|codex|kiro|opencode), forwarded as --runtime.
# UNSET -> pass nothing; the client installs every shipped adapter by default.
RUNTIME_ARG=""
[ -n "${FIREKEEP_RUNTIME:-}" ] && RUNTIME_ARG="--runtime ${FIREKEEP_RUNTIME}"
# The wizard hand-off runs through `current` — the same alias every rendered
# surface uses — so what we hand to is provably what sessions will launch.
FIREKEEP_BIN="${CURRENT}/bin/firekeep"

# Detect an existing install independently of the fast path. Version-changing updates and
# FIREKEEP_FORCE_REINSTALL both provision below, but their final hand-off must reuse the
# existing connection instead of prompting for it again. Join codes are zero-prompt too.
# `current` is the layout's truth; the legacy single venv marks a pre-0.1.35 install
# mid-migration.
installed="$(venv_version "${CURRENT}")"
[ -n "${installed}" ] || installed="$(venv_version "${LEGACY_VENV}")"
NON_INTERACTIVE_ARG=""
if [ -n "${installed}" ] || [ -n "${FIREKEEP_JOIN:-}" ]; then
    NON_INTERACTIVE_ARG="--non-interactive"
fi

# --- idempotent fast path: a healthy venvs/<V> already exists -> flip + re-render ---
# One rule covers three cases (mirrors install.ps1): a re-run while already current
# (e.g. re-targeting a runtime via FIREKEEP_RUNTIME), a crash between flip and wizard
# (re-run heals), and an instant ROLLBACK (`firekeep update --to <prev>` while
# venvs/<prev> survives GC — zero downloads, just a flip). "Healthy" means the venv's
# OWN python reports ${V}; a partial venv fails the probe and takes the full provision
# path, which rebuilds it with --clear. FIREKEEP_FORCE_REINSTALL forces the full path
# without changing the non-interactive hand-off.
if [ "$(venv_version "${TARGET_VENV}")" = "${V}" ] && venv_complete "${TARGET_VENV}" && [ -z "${FIREKEEP_FORCE_REINSTALL:-}" ]; then
    echo "firekeep: venvs/${V} is already provisioned - selecting it and re-rendering \
adapters. Set FIREKEEP_FORCE_REINSTALL=1 to force a full reinstall." >&2
    point_current "${TARGET_VENV}"
    # `|| wizard_exit=$?` keeps a failing wizard from killing the script under
    # `set -e` before the GC sweep runs; its exit code is still propagated.
    wizard_exit=0
    if ( : < /dev/tty ) 2>/dev/null; then
        if [ -n "${FIREKEEP_JOIN:-}" ]; then
            "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} --join "${FIREKEEP_JOIN}" ${NON_INTERACTIVE_ARG} < /dev/tty || wizard_exit=$?
        else
            "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} ${NON_INTERACTIVE_ARG} < /dev/tty || wizard_exit=$?
        fi
    else
        if [ -n "${FIREKEEP_JOIN:-}" ]; then
            "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} --join "${FIREKEEP_JOIN}" --non-interactive || wizard_exit=$?
        else
            "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} --non-interactive || wizard_exit=$?
        fi
    fi
    remove_stale_venvs
    exit ${wizard_exit}
fi

# --- 3. this version's SHA256SUMS ---------------------------------------------
# On the `firekeep update` path the CLIENT has already fetched this version's
# SHA256SUMS and verified its minisign signature against the key pinned in the
# installed client — and it hands the VERIFIED bytes through FIREKEEP_SUMS_FILE.
# Using that file, and NOT fetching again, is what closes the two-fetch split: a
# host serving different bytes to the client's fetch (urllib) and this one (curl)
# could otherwise install attacker artifacts with exit 0 even after the client
# "verified" the release. Honoured only alongside FIREKEEP_VERSION — the shape
# only the client's hand-off produces — so a manual `curl | sh` run (which sets
# neither) fetches exactly as before. Set-but-unusable is fatal, never a silent
# fallback to the network: the fallback IS the vulnerability.
SUMS_HANDED=""
if [ -n "${FIREKEEP_SUMS_FILE:-}" ] && [ -n "${FIREKEEP_VERSION:-}" ]; then
    [ -r "${FIREKEEP_SUMS_FILE}" ] || die "FIREKEEP_SUMS_FILE is set but not readable: ${FIREKEEP_SUMS_FILE}"
    cp "${FIREKEEP_SUMS_FILE}" "${BIN}/SHA256SUMS" || die "cannot copy FIREKEEP_SUMS_FILE into place"
    SUMS_HANDED=1
    echo "firekeep: using signature-verified SHA256SUMS handed by firekeep update (no re-fetch)"
else
    fetch "${VBASE}/SHA256SUMS" "${BIN}/SHA256SUMS"
fi

# --- 3b. BEST-EFFORT minisign verification of SHA256SUMS ---------------------
# Honest scope (docs/RELEASE-SIGNING.md): this script was itself fetched from the
# release host, so on a FIRST install this check cannot defeat a compromised host —
# that trust-on-first-use is stated in the threat model, not papered over. It earns
# its keep on the `firekeep update` re-exec path, where the client verified THIS script
# against a signed SHA256SUMS before running it, making the baked key trustworthy —
# and it lets a cautious first-time installer pin the key out of band via
# FIREKEEP_SIGNING_PUB. Best-effort by design: no minisign binary, no baked/provided
# key, or no published .minisig -> skip/warn, never break a bare machine. A PRESENT
# signature that fails to verify is fatal — invalid is tampering, absence is history.
# The placeholder probe is split like the dist-base one so make_release's token
# substitution can't rewrite the comparison itself.
# Skipped entirely under a handed FIREKEEP_SUMS_FILE: the client already did the
# authoritative verification against ITS pinned key, and "under a handed file, do
# not fetch at all" is the contract — fetching the .minisig here would reopen a
# network round trip on a path whose whole point is that it makes none.
SIGNING_PUB_DEFAULT="__FIREKEEP_SIGNING_PUB_DEFAULT__"
sig_placeholder="__FIREKEEP_SIGNING_PUB_""DEFAULT__"
[ "${SIGNING_PUB_DEFAULT}" = "${sig_placeholder}" ] && SIGNING_PUB_DEFAULT=""
SIGNING_PUB="${FIREKEEP_SIGNING_PUB:-${SIGNING_PUB_DEFAULT}}"
if [ -z "${SUMS_HANDED}" ] && [ -n "${SIGNING_PUB}" ] && command -v minisign >/dev/null 2>&1; then
    if fetch_opt "${VBASE}/SHA256SUMS.minisig" "${BIN}/SHA256SUMS.minisig"; then
        minisign -Vq -m "${BIN}/SHA256SUMS" -x "${BIN}/SHA256SUMS.minisig" -P "${SIGNING_PUB}"             || die "SHA256SUMS signature verification FAILED — refusing to install (possible release-host compromise)"
        echo "firekeep: SHA256SUMS signature verified (minisign)"
    else
        echo "firekeep: WARNING: release ${V} is not signed (no SHA256SUMS.minisig); relying on TLS + checksums" >&2
    fi
fi

verify_against_sums() {
    # $1 = local file, $2 = basename to look up. Assign THEN cut: `grep | cut` returns cut's
    # status (0 on empty input), so a missing entry would sail past `|| die` with want="" and
    # surface as "checksum mismatch: expected , got <hash>" — a missing entry must stay
    # distinguishable from a tampered one. Shared by uv AND the wheel: a second, subtly
    # different verifier is how the wheel got skipped once already.
    sums_line="$(grep " $2\$" "${BIN}/SHA256SUMS")" || die "no SHA256SUMS entry for $2"
    want="$(printf '%s\n' "${sums_line}" | cut -d' ' -f1)"
    got="$(sha256_of "$1")"
    [ "${want}" = "${got}" ] || { rm -f "$1"; die "checksum mismatch for $2: expected ${want}, got ${got}"; }
}

# --- 4. uv, checksum-verified BEFORE we execute it ---------------------------
# This binary is fetched over unauthenticated HTTP inside the office network and then run.
# The checksum is the only thing between a teammate and someone else's code.
echo "firekeep: fetching uv (${target})"
fetch "${VBASE}/uv-${target}" "${BIN}/uv.tmp"
verify_against_sums "${BIN}/uv.tmp" "uv-${target}"
mv "${BIN}/uv.tmp" "${BIN}/uv"
chmod +x "${BIN}/uv"

# --- 5. the wheel, fetched then checksum-verified BEFORE it is installed ----
# Never by name: `firekeep-client` on PyPI is owned by a third party, so resolving the name
# against any index could install foreign code. Deps (mcp, httpx) resolve from PyPI normally.
# `uv pip install <url>` does NO hash checking at all — that was the hole C2 lived in. Fetch
# to a local file and verify it with the SAME helper as uv, BEFORE the venv even exists, so a
# tampered wheel never reaches `uv pip install` and never gets the chance to become the
# PreToolUse hook that runs before every Edit on this machine.
echo "firekeep: fetching ${wheel_name}"
fetch "${VBASE}/${wheel_name}" "${BIN}/${wheel_name}"
verify_against_sums "${BIN}/${wheel_name}" "${wheel_name}"

# --- 5b. the symdex wheel, fetched and verified BEFORE anything destructive --
# Deliberately hoisted above provisioning: when venvs/${V} is the venv `current`
# points at (forced reinstall / repair of the running version), the --clear
# below destroys it — so every network fetch and every checksum must already
# have succeeded by then. A download or verification failure must never strand
# `current` on a gutted venv. SHA256SUMS lists the exact bundled wheel name AND
# its hash; symdex versions independently of the client.
symdex_wheel="$(grep -oE 'firekeep_symdex-[0-9][^ ]*\.whl' "${BIN}/SHA256SUMS" | head -1)"
[ -n "${symdex_wheel}" ] || die "SHA256SUMS lists no firekeep_symdex wheel — release is incomplete"
echo "firekeep: fetching ${symdex_wheel}"
fetch "${VBASE}/${symdex_wheel}" "${BIN}/${symdex_wheel}"
verify_against_sums "${BIN}/${symdex_wheel}" "${symdex_wheel}"

# --- 5c. the docdex wheel, same hoist and the same reasoning as 5b -----------
# Docdex (the documents dex) is bundled exactly like symdex: name and hash read
# from SHA256SUMS, fetched to a local file, verified — all of it above the
# --clear, because ONE bundled wheel fetched below that line is enough to strand
# `current` on a gutted venv. It versions independently of the client AND of
# symdex, so the release names the exact wheel and this script never guesses it.
docdex_wheel="$(grep -oE 'firekeep_docdex-[0-9][^ ]*\.whl' "${BIN}/SHA256SUMS" | head -1)"
[ -n "${docdex_wheel}" ] || die "SHA256SUMS lists no firekeep_docdex wheel — release is incomplete"
echo "firekeep: fetching ${docdex_wheel}"
fetch "${VBASE}/${docdex_wheel}" "${BIN}/${docdex_wheel}"
verify_against_sums "${BIN}/${docdex_wheel}" "${docdex_wheel}"

# --- 6. standalone CPython + venv AT ITS FINAL VERSIONED PATH ----------------
#
# HISTORY, and the constraint this layout is built around. 0.1.26 tried
# provisioning into "${VENV}.new" and renaming it over the live tree, to avoid
# deleting a venv a running session execs from. It looked strictly better and it
# does not work -- A VENV IS NOT RELOCATABLE. `uv venv` bakes its own absolute
# path into pyvenv.cfg and into every console script's interpreter line, so after
# `mv ${VENV}.new ${VENV}` the scripts still point at ${VENV}.new/bin/python,
# which no longer exists. The e2e bootstrap gate caught it exactly there:
#     install.sh: 267: .../venv/bin/firekeep: not found   (exit 127)
#
# The side-by-side layout is the design that squares that circle: venvs/${V} IS
# the final path — the venv is created here and never moved, so every baked
# absolute path stays true forever, and the thing that changes is only the
# `current` symlink (flipped atomically after the wheels install). The old
# 30-120s window where ${VENV} did not exist and every lifecycle hook on every
# live session failed with "No such file or directory" is gone: the previous
# venv remains fully intact until GC proves nothing needs it.
#
# --clear covers re-provisioning a partial venvs/${V} left by an interrupted
# install (the fast path's health probe deliberately fails those into this
# path). A fresh dir has nothing to clear.
#
# HONEST RESIDUAL: when venvs/${V} is the venv `current` points at (forced
# reinstall, or repairing an unhealthy current version), --clear IS destructive
# to the selected venv. That is why every download and checksum happens above
# this line — the only failures left after the clear are local (uv provision,
# pip). If one still dies, `current` points at a gutted venv until a re-run
# heals it: the fast path's health probe fails the gutted venv straight back
# into this full path. Confined to the explicit repair flow; a normal
# version-changing update never clears anything `current` selects.
#
# --python-preference only-managed is load-bearing on Windows (default discovery
# walks PATH into the dangling Store python3.exe alias) and keeps the contract
# honest here: a "standalone CPython" must never silently bind to whatever pyenv
# shim discovery finds first.
echo "firekeep: provisioning Python ${PYTHON_VERSION} into venvs/${V}"
mkdir -p "${VENVS}"
"${BIN}/uv" venv "${TARGET_VENV}" --python "${PYTHON_VERSION}" --python-preference only-managed --clear     || die "could not provision Python ${PYTHON_VERSION}"

# --- 7. install ALL wheels BY LOCAL FILE PATH, in ONE resolution -------------
# One `uv pip install` for the client + every dex wheel, never one per wheel.
# This is load-bearing, found the hard way on the 1.0.0 release: docdex's wheel
# declares `firekeep-client>=0.1.48`, and `--reinstall` reinstalls the ENTIRE
# resolution set — so a separate docdex step re-resolved firekeep-client from
# the INDEX and silently replaced the local wheel just installed in step 7 with
# whatever PyPI's newest happened to be (a fresh 1.0.0 install shipped 0.1.48).
# With all three local files in one request, each local wheel IS the resolution
# for its own name; only genuine third-party deps come from the index.
#
# Every dex wheel ships with every install; REGISTRATION (~/.firekeep/dexes.json)
# is what decides whether a dex does anything. Gating the INSTALL instead would
# put a second, unverified download path in front of a user who later opts in —
# the signed supply chain is the thing that must not become optional.
echo "firekeep: installing ${wheel_name} + ${symdex_wheel} + ${docdex_wheel}"
"${BIN}/uv" pip install --python "${TARGET_VENV}" --reinstall "${BIN}/${wheel_name}" "${BIN}/${symdex_wheel}" "${BIN}/${docdex_wheel}"     || die "wheel install failed"

# --- 7d. the install must have produced a runnable firekeep -------------------
# Provisioning can succeed while producing something unusable (a wheel for the
# wrong platform, a truncated interpreter). Checked at the venv's REAL path and
# BEFORE the flip: a broken build must never become `current`, and a session
# started mid-install must never resolve to it. The wizard hand-off is the very
# next act, so checking now converts a confusing exit 127 into a diagnosis at
# the point of failure.
if [ ! -x "${TARGET_VENV}/bin/firekeep" ]; then
    die "install completed but ${TARGET_VENV}/bin/firekeep is not executable — the wheel did not provide it"
fi

# --- 7e. flip `current` — the new venv is complete and verified ---------------
# Only now does anything observable change. An install that died in any earlier
# step left `current` (and every live session) exactly as it was.
point_current "${TARGET_VENV}"

# --- 8. hand off to the wizard ----------------------------------------------
# THE curl|sh TRAP: piping this script to `sh` makes the SCRIPT stdin, so the wizard's
# sys.stdin.isatty() is False, every prompt is skipped, and agent_id lands as CHANGEME.
# Re-attach the real terminal explicitly.
#
# The guard TRIES THE OPEN rather than testing the path. `[ -e /dev/tty ] && [ -r /dev/tty ]`
# is NOT a controlling-terminal test: those use access(), and /dev/tty is crw-rw-rw- so it
# exists and is readable even for a process with no controlling terminal. Opening it is what
# fails there (ENXIO) — cron, `docker run` without -t, systemd, many CI runners. A path test
# would take the interactive branch in exactly the headless case it exists to catch, and the
# redirect would then die with a raw shell I/O error instead of falling back cleanly.
#
# The probe must be a SUBSHELL `( : < /dev/tty )`, not a brace group: `:` is a POSIX
# special builtin, and a redirection error on a special builtin makes strictly-POSIX shells
# (dash — i.e. Debian/Ubuntu sh) exit the WHOLE script, status 2, message eaten by the
# 2>/dev/null. Bash-as-sh (macOS) shrugs it off, which is how the brace form survived
# local testing. In a subshell only the subshell dies, so the probe just returns false.
# `|| wizard_exit=$?` keeps a failing wizard from killing the script under
# `set -e` before the GC sweep runs; its exit code is still propagated.
wizard_exit=0
if ( : < /dev/tty ) 2>/dev/null; then
    if [ -n "${FIREKEEP_JOIN:-}" ]; then
        "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} --join "${FIREKEEP_JOIN}" ${NON_INTERACTIVE_ARG} < /dev/tty || wizard_exit=$?
    else
        "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} ${NON_INTERACTIVE_ARG} < /dev/tty || wizard_exit=$?
    fi
else
    echo "firekeep: no terminal available — writing a default config (not prompting)" >&2
    if [ -n "${FIREKEEP_JOIN:-}" ]; then
        "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} --join "${FIREKEEP_JOIN}" --non-interactive || wizard_exit=$?
    else
        "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} --non-interactive || wizard_exit=$?
    fi
fi

# --- 9. GC venvs nothing needs anymore ---------------------------------------
# After the wizard so a failed hand-off still leaves the machine on the new,
# fully-installed version with everything else untouched.
remove_stale_venvs
exit ${wizard_exit}
