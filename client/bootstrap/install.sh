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
# FIREKEEP_RUNTIME targets ONE agent (claude|codex|kiro|opencode), forwarded as --runtime.
# UNSET -> pass nothing; the client installs every shipped adapter by default.
RUNTIME_ARG=""
[ -n "${FIREKEEP_RUNTIME:-}" ] && RUNTIME_ARG="--runtime ${FIREKEEP_RUNTIME}"
FIREKEEP_BIN="${VENV}/bin/firekeep"

# Detect an existing install independently of the fast path. Version-changing updates and
# FIREKEEP_FORCE_REINSTALL both rebuild below, but their final hand-off must reuse the
# existing connection instead of prompting for it again. Join codes are zero-prompt too.
installed=""
if [ -x "${FIREKEEP_BIN}" ]; then
    # -I (isolated) is load-bearing, not tidiness -- see the matching note in
    # install.ps1. `python -c` puts the CURRENT WORKING DIRECTORY on sys.path[0], so
    # running this from a checkout's client/ directory imports the SOURCE TREE and
    # reports its version rather than the venv's. When that misread equals ${V}, the
    # fast path below skips the install and prints "already at ${V}" -- a silent no-op
    # reported as success. -I also drops PYTHONPATH and user site-packages.
    installed="$("${VENV}/bin/python" -I -c 'import firekeep_client; print(firekeep_client.__version__)' 2>/dev/null || true)"
fi
NON_INTERACTIVE_ARG=""
if [ -n "${installed}" ] || [ -n "${FIREKEEP_JOIN:-}" ]; then
    NON_INTERACTIVE_ARG="--non-interactive"
fi

# --- idempotent fast path: already at ${V} -> re-render only, skip the rebuild ----
# Mirrors install.ps1. On POSIX the rebuild is not lock-blocked (unlink of a running exe
# works), but skipping a needless reprovision is faster and lets FIREKEEP_RUNTIME re-target a
# runtime without rebuilding. The wizard hand-off only RE-RENDERS adapters (never touches the
# venv). FIREKEEP_FORCE_REINSTALL forces the full reinstall path without changing the
# non-interactive update hand-off.
if [ "${installed}" = "${V}" ] && [ -z "${FIREKEEP_FORCE_REINSTALL:-}" ]; then
    echo "firekeep: already at ${V} — re-rendering adapters, no venv rebuild. Set \
FIREKEEP_FORCE_REINSTALL=1 to force a full reinstall." >&2
    if ( : < /dev/tty ) 2>/dev/null; then
        if [ -n "${FIREKEEP_JOIN:-}" ]; then
            "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} --join "${FIREKEEP_JOIN}" ${NON_INTERACTIVE_ARG} < /dev/tty
        else
            "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} ${NON_INTERACTIVE_ARG} < /dev/tty
        fi
    else
        if [ -n "${FIREKEEP_JOIN:-}" ]; then
            "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} --join "${FIREKEEP_JOIN}" --non-interactive
        else
            "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} --non-interactive
        fi
    fi
    exit $?
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
# --- 6. standalone CPython + venv, only now that uv is verified --------------
#
# `--clear` recreates the venv IN PLACE, which is required, and the honest reason
# is narrower than it used to be documented as.
#
# WHAT WAS TRIED AND FAILED: provisioning into "${VENV}.new" and renaming it over
# the live tree, to avoid deleting a venv a running session execs from. It looked
# strictly better and it does not work -- A VENV IS NOT RELOCATABLE. `uv venv`
# bakes its own absolute path into pyvenv.cfg and into every console script's
# interpreter line, so after `mv ${VENV}.new ${VENV}` the scripts still point at
# ${VENV}.new/bin/python, which no longer exists. The e2e bootstrap gate caught it
# exactly there:
#     install.sh: 267: .../venv/bin/firekeep: not found   (exit 127)
# A staged venv would need `uv venv --relocatable` or a post-swap rewrite of every
# script; both are more machinery than the problem justifies here.
#
# SO THE REAL EXPOSURE STANDS, and is worth stating rather than hiding: for the
# 30-120s of provisioning, ${VENV} does not exist, and every lifecycle hook execs
# ${VENV}/bin/python -- PreToolUse (blocking, gates every Edit), PostToolUse,
# UserPromptSubmit, SessionStart, Stop, plus three stdio MCP servers on reconnect.
# Those fail with "No such file or directory" for the duration. The hook cores all
# fail OPEN (a missing interpreter blocks nothing), so the cost is lost telemetry
# and a skipped lease check, not a broken session.
#
# WHAT MITIGATES IT, and why background auto-update is still acceptable:
#   * Windows refuses outright when live processes hold the venv (install.ps1),
#     so it is exposed for zero seconds.
#   * On POSIX the window is bounded by one uv provision + two wheel installs.
#   * `firekeep update` run by hand has always had this behaviour; the background
#     path automates the same operation, it does not add a new hazard.
# Narrowing it further belongs with a proper relocatable-venv change, tracked
# rather than half-done here.
#
# --python-preference only-managed is load-bearing on Windows (default discovery
# walks PATH into the dangling Store python3.exe alias) and keeps the contract
# honest here: a "standalone CPython" must never silently bind to whatever pyenv
# shim discovery finds first.
echo "firekeep: provisioning Python ${PYTHON_VERSION}"
"${BIN}/uv" venv "${VENV}" --python "${PYTHON_VERSION}" --python-preference only-managed --clear     || die "could not provision Python ${PYTHON_VERSION}"

# --- 7. install the wheel BY LOCAL FILE PATH, never a URL --------------------
echo "firekeep: installing ${wheel_name}"
"${BIN}/uv" pip install --python "${VENV}" --reinstall "${BIN}/${wheel_name}"     || die "wheel install failed"

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
"${BIN}/uv" pip install --python "${VENV}" --reinstall "${BIN}/${symdex_wheel}"     || die "symdex wheel install failed"

# --- 7c. the install must have produced a runnable firekeep -------------------
# Provisioning can succeed while producing something unusable (a wheel for the
# wrong platform, a truncated interpreter). Running "${FIREKEEP_BIN}" install is
# the very next act, so checking it now converts a confusing exit 127 from the
# wizard hand-off into a diagnosis at the point of failure.
if [ ! -x "${FIREKEEP_BIN}" ]; then
    die "install completed but ${FIREKEEP_BIN} is not executable — the wheel did not provide it"
fi


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
    if [ -n "${FIREKEEP_JOIN:-}" ]; then
        "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} --join "${FIREKEEP_JOIN}" ${NON_INTERACTIVE_ARG} < /dev/tty
    else
        "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} ${NON_INTERACTIVE_ARG} < /dev/tty
    fi
else
    echo "firekeep: no terminal available — writing a default config (not prompting)" >&2
    if [ -n "${FIREKEEP_JOIN:-}" ]; then
        "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} --join "${FIREKEEP_JOIN}" --non-interactive
    else
        "${FIREKEEP_BIN}" install --dist-base "${BASE}" ${RUNTIME_ARG} --non-interactive
    fi
fi
