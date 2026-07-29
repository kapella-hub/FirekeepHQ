#!/bin/sh
# Firekeep client bootstrap (macOS / Linux). Fetches uv, provisions a standalone CPython
# into ~/.firekeep/venv, installs the client wheel (fetched to a local file and checksum-
# verified against a versioned SHA256SUMS, never installed straight from a URL), then hands
# off to the wizard.
#
# Requires nothing on the machine but a shell and curl/wget. Deliberately POSIX sh (not
# bash): macOS ships bash 3.2, and Alpine/minimal Linux images ship no bash at all.
set -eu

FIREKEEP_HOME="${HOME}/.firekeep"
VENV="${FIREKEEP_HOME}/venv"
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

# --- 1. platform -------------------------------------------------------------
os="$(uname -s)"; arch="$(uname -m)"
case "${os}-${arch}" in
    Darwin-arm64)  target=aarch64-apple-darwin ;;
    Darwin-x86_64) target=x86_64-apple-darwin ;;
    Linux-x86_64)  target=x86_64-unknown-linux-gnu ;;
    Linux-aarch64) target=aarch64-unknown-linux-gnu ;;
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
# FIREKEEP_RUNTIME targets ONE agent (claude|codex|kiro), forwarded as --runtime. UNSET -> pass
# nothing so the wizard prompts "Install for which agent?" (defaults to all when headless).
RUNTIME_ARG=""
[ -n "${FIREKEEP_RUNTIME:-}" ] && RUNTIME_ARG="--runtime ${FIREKEEP_RUNTIME}"
FIREKEEP_BIN="${VENV}/bin/firekeep"

# --- idempotent fast path: already at ${V} -> re-render only, skip the rebuild ----
# Mirrors install.ps1. On POSIX the rebuild is not lock-blocked (unlink of a running exe
# works), but skipping a needless reprovision is faster and lets FIREKEEP_RUNTIME re-target a
# runtime without rebuilding. The wizard hand-off only RE-RENDERS adapters (never touches the
# venv). FIREKEEP_FORCE_REINSTALL forces the full reinstall path.
if [ -x "${FIREKEEP_BIN}" ] && [ -z "${FIREKEEP_FORCE_REINSTALL:-}" ]; then
    installed="$("${VENV}/bin/python" -c 'import firekeep_client; print(firekeep_client.__version__)' 2>/dev/null || true)"
    if [ "${installed}" = "${V}" ]; then
        echo "firekeep: already at ${V} — re-rendering adapters, no venv rebuild. Set \
FIREKEEP_FORCE_REINSTALL=1 to force a full reinstall." >&2
        if ( : < /dev/tty ) 2>/dev/null; then
            "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} < /dev/tty
        else
            "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} --non-interactive
        fi
        exit $?
    fi
fi

# --- 3. this version's SHA256SUMS, fetched once ------------------------------
fetch "${VBASE}/SHA256SUMS" "${BIN}/SHA256SUMS"

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

# --- 6. standalone CPython + venv, only now that uv is verified --------------
#
# BUILD BESIDE, THEN SWAP. The whole kit is provisioned into a STAGING directory
# and moved into place with a single rename, so ~/.firekeep/venv is either the old
# tree or the new one and never a partially-built one.
#
# This replaces `uv venv "${VENV}" --clear`, which deleted the live venv and then
# spent 30-120s repopulating it. The rationale for that was "POSIX unlink is safe,
# so a running session keeps working" — and that is only half true. unlink()
# protects processes whose files are ALREADY MAPPED. It does nothing for a NEW
# exec, and every lifecycle hook spawns a fresh "${VENV}/bin/python":
#   PreToolUse (blocking, gates every Edit), PostToolUse, UserPromptSubmit,
#   SessionStart, Stop, plus the three stdio MCP servers on reconnect.
# So for the whole reinstall window, every hook on every live macOS/Linux session
# failed with "No such file or directory" — and background auto-update is ON by
# default, so nobody asked for that window to open.
#
# Windows needed a live-holder guard (install.ps1) because it cannot delete files
# an running process holds at all. Staging retires the ASYMMETRY rather than
# copying the guard: neither platform has to delete a venv that is in use.
#
# --python-preference only-managed is load-bearing on Windows (default discovery
# walks PATH into the dangling Store python3.exe alias) and keeps the contract
# honest here: a "standalone CPython" must never silently bind to whatever pyenv
# shim discovery finds first.
STAGE="${VENV}.new"
rm -rf "${STAGE}" 2>/dev/null || true
echo "firekeep: provisioning Python ${PYTHON_VERSION}"
"${BIN}/uv" venv "${STAGE}" --python "${PYTHON_VERSION}" --python-preference only-managed \
    || die "could not provision Python ${PYTHON_VERSION}"

# --- 7. install the wheel BY LOCAL FILE PATH, never a URL --------------------
echo "firekeep: installing ${wheel_name}"
"${BIN}/uv" pip install --python "${STAGE}" --reinstall "${BIN}/${wheel_name}" \
    || die "wheel install failed"

# --- 7b. symdex wheel: ALWAYS installed, same fetch -> verify -> local-path dance ---
# Symdex is an always-on client MCP server (like firekeep-decision). SHA256SUMS lists the
# exact bundled wheel name AND its hash, so read the name from there rather than guessing
# a version — symdex versions independently of the client.
symdex_wheel="$(grep -oE 'firekeep_symdex-[0-9][^ ]*\.whl' "${BIN}/SHA256SUMS" | head -1)"
[ -n "${symdex_wheel}" ] || die "SHA256SUMS lists no firekeep_symdex wheel — release is incomplete"
echo "firekeep: fetching ${symdex_wheel}"
fetch "${VBASE}/${symdex_wheel}" "${BIN}/${symdex_wheel}"
verify_against_sums "${BIN}/${symdex_wheel}" "${symdex_wheel}"
echo "firekeep: installing ${symdex_wheel}"
"${BIN}/uv" pip install --python "${STAGE}" --reinstall "${BIN}/${symdex_wheel}" \
    || die "symdex wheel install failed"

# --- 7c. SWAP: the staged tree becomes the live one ---------------------------
# Verify the staged kit is USABLE before anything is swapped. Provisioning can
# succeed while producing something unusable (a wheel for the wrong platform, a
# truncated interpreter), and the point of staging is that such a failure leaves
# the working install untouched.
#
# The check is that `firekeep` is EXECUTABLE, not that `firekeep_client` imports.
# That is deliberate and was learned from a red CI run: the import form failed the
# POSIX bootstrap suite because the harness's stub uv creates bin/firekeep and
# deliberately no-ops `pip install`, so nothing was importable and the guard
# aborted every install. The guard was right; the CHECK asserted a property the
# next step does not need.
#
# What the bootstrap does next is run "${VENV}/bin/firekeep install", so a runnable
# firekeep IS the precondition. A staged tree that cannot produce one is exactly
# the failure worth refusing.
if [ ! -x "${STAGE}/bin/firekeep" ]; then
    rm -rf "${STAGE}"
    die "the staged kit has no runnable firekeep - leaving the existing install in place"
fi

# Two renames, not one: POSIX rename(2) is atomic per call, so the window in which
# neither tree is at ${VENV} is the duration of a directory rename — microseconds,
# versus the 30-120s the old --clear left it missing. A hook that lands in that
# window sees ENOENT exactly as before; one that lands anywhere else sees a
# complete venv, which was previously not true for the whole install.
OLD="${VENV}.old.$$"
if [ -d "${VENV}" ]; then
    mv "${VENV}" "${OLD}" || die "could not move the existing venv aside"
fi
if ! mv "${STAGE}" "${VENV}"; then
    # Put the working install back rather than leaving the machine with none.
    [ -d "${OLD}" ] && mv "${OLD}" "${VENV}"
    die "could not move the new venv into place"
fi
# Deleting the old tree AFTER the swap is what makes the window short. Running
# processes still hold their mapped files here (this is where POSIX unlink
# semantics genuinely do apply), so an in-flight session is unaffected.
rm -rf "${OLD}" 2>/dev/null || true

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
if ( : < /dev/tty ) 2>/dev/null; then
    "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} < /dev/tty
else
    echo "firekeep: no terminal available — writing a default config (not prompting)" >&2
    "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} --non-interactive
fi
