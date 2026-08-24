#!/usr/bin/env bash
# Unit test for the security-posture helpers in deploy/lib.sh, and for the
# claims install.sh / update.sh build on top of them.
#
# Why this file exists
# --------------------
# Audit blocker 7 turned auth ON by default and bound the published ports to
# 127.0.0.1. Both are read from .env at runtime, which means the installer's
# closing summary can now DISAGREE with what the stack is actually doing:
#   - it printed "Auth: ..." from what the flags asked for, not from .env;
#   - it printed http://<VPS_IP>:8040 and "ports are exposed on 0.0.0.0"
#     unconditionally, both of which are false on a loopback-bound install.
# A security summary that is wrong half the time is one operators learn to
# skip, so the derivation is a unit under test rather than inline echoes.
#
# Needs no Docker and no network (unlike the other two files here) — it drives
# the real bash functions against temp env files.
#
# Run from the repo root:
#   bash deploy/tests/test_auth_posture.sh
set -euo pipefail
cd "$(dirname "$0")/../.."

# shellcheck source=../lib.sh
source deploy/lib.sh

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

FAILS=0
pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; FAILS=$((FAILS + 1)); }

check() {  # $1=label  $2=expected  $3=actual
    if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (expected '$2', got '$3')"; fi
}

envfile() {  # $1=name  $2...=lines -> echoes the path
    local path="$TMP/$1"
    shift
    : > "$path"
    local line
    for line in "$@"; do printf '%s\n' "$line" >> "$path"; done
    printf '%s\n' "$path"
}

# --- auth_enforced ----------------------------------------------------------
# The contract it has to mirror is TWO layers: docker-compose.yml's
# ${AUTH_ENABLED:-true} (where unset AND empty both fall to the default) and
# pydantic's bool parsing in auth/config.py (0/off/f/false/n/no are false).
echo "auth_enforced:"

E="$(envfile auth_true 'AUTH_ENABLED=true')"
auth_enforced "$E" && pass "AUTH_ENABLED=true -> enforced" || fail "AUTH_ENABLED=true -> enforced"

E="$(envfile auth_false 'AUTH_ENABLED=false')"
auth_enforced "$E" && fail "AUTH_ENABLED=false -> disabled" || pass "AUTH_ENABLED=false -> disabled"

# The compose default. An .env written before this setting existed has no
# line at all, and MUST report enforced — reporting "disabled" here would tell
# every upgrading operator their stack is open when it is not.
E="$(envfile auth_absent 'VPS_IP=10.0.0.1')"
auth_enforced "$E" && pass "absent -> enforced (compose \${AUTH_ENABLED:-true})" \
    || fail "absent -> enforced (compose \${AUTH_ENABLED:-true})"

# `:-` treats empty exactly like unset, so a bare `AUTH_ENABLED=` is enforced.
# Reading this as "disabled" is the subtle one: it looks false-y in shell.
E="$(envfile auth_empty 'AUTH_ENABLED=')"
auth_enforced "$E" && pass "empty -> enforced (\${:-} treats empty as unset)" \
    || fail "empty -> enforced (\${:-} treats empty as unset)"

for v in FALSE False 0 no NO off f n; do
    E="$(envfile "auth_$v" "AUTH_ENABLED=$v")"
    auth_enforced "$E" && fail "AUTH_ENABLED=$v -> disabled" || pass "AUTH_ENABLED=$v -> disabled"
done

for v in TRUE True 1 yes on t y; do
    E="$(envfile "authy_$v" "AUTH_ENABLED=$v")"
    auth_enforced "$E" && pass "AUTH_ENABLED=$v -> enforced" || fail "AUTH_ENABLED=$v -> enforced"
done

# Last-wins, matching how compose parses a duplicated key. If this read the
# FIRST match, an operator who appended AUTH_ENABLED=false to the bottom of
# .env would be told auth is enforced while the stack runs wide open.
E="$(envfile auth_dup 'AUTH_ENABLED=true' 'AUTH_ENABLED=false')"
auth_enforced "$E" && fail "duplicate key -> last wins (disabled)" \
    || pass "duplicate key -> last wins (disabled)"

# --- effective_bind_addr ----------------------------------------------------
echo "effective_bind_addr:"

E="$(envfile bind_absent 'VPS_IP=10.0.0.1')"
check "absent -> 127.0.0.1 (compose default)" "127.0.0.1" "$(effective_bind_addr "$E")"

E="$(envfile bind_empty 'BIND_ADDR=')"
check "empty -> 127.0.0.1" "127.0.0.1" "$(effective_bind_addr "$E")"

E="$(envfile bind_public 'BIND_ADDR=0.0.0.0')"
check "0.0.0.0 -> 0.0.0.0" "0.0.0.0" "$(effective_bind_addr "$E")"

E="$(envfile bind_lan 'BIND_ADDR=192.168.1.20')"
check "LAN address preserved" "192.168.1.20" "$(effective_bind_addr "$E")"

check "missing file -> 127.0.0.1" "127.0.0.1" "$(effective_bind_addr "$TMP/nope")"

# --- bind_addr_is_public ----------------------------------------------------
echo "bind_addr_is_public:"

for a in 0.0.0.0 192.168.1.20 10.0.0.4 203.0.113.7 ::; do
    bind_addr_is_public "$a" && pass "$a -> public" || fail "$a -> public"
done
for a in 127.0.0.1 localhost ::1; do
    bind_addr_is_public "$a" && fail "$a -> loopback" || pass "$a -> loopback"
done

# --- no_auth_requested ------------------------------------------------------
# The flag has to be exact. A near-miss silently installing an UNAUTHENTICATED
# stack (or silently ignoring a genuine opt-out) are both unacceptable.
echo "no_auth_requested:"

no_auth_requested --insecure-no-auth && pass "--insecure-no-auth -> true" \
    || fail "--insecure-no-auth -> true"
no_auth_requested --office --insecure-no-auth && pass "recognised among other flags" \
    || fail "recognised among other flags"
no_auth_requested && fail "no args -> false" || pass "no args -> false"
no_auth_requested --office && fail "--office alone -> false" || pass "--office alone -> false"
# --no-auth is deliberately NOT the flag: it is too easy to copy out of a forum
# post without reading what it does.
no_auth_requested --no-auth && fail "--no-auth is not the flag" || pass "--no-auth is not the flag"
no_auth_requested --insecure-no-auth=1 && fail "no substring match" || pass "no substring match"

# --- install.sh's closing summary, EXECUTED --------------------------------
# Not a grep for `auth_enforced .env`. A substring check is satisfied by ANY
# occurrence, so deleting the derivation from the branch that matters while
# leaving it in a later one keeps such a check green — verified by planting
# exactly that defect, which a string check did not catch.
#
# So extract the real summary block out of install.sh and RUN it, the same
# technique (and for the same reason) as tests/test_install_health_probe.py:
# a re-implementation would be written with the same wrong assumption as the
# code. It runs in a sandbox dir containing a crafted .env, so the block's
# literal `.env` references resolve to the fixture.
REPO_ROOT="$PWD"
sed -n '/^# --- Print status ---/,$p' install.sh > "$TMP/summary-block.sh"
[ -s "$TMP/summary-block.sh" ] || { echo "could not extract install.sh's summary block"; exit 1; }

# A recognisable stand-in for the key bootstrap-keys.sh minted, so assertions
# can demand the VALUE itself appears — not merely a heading above it.
FAKE_ADMIN_KEY="nxs_$(printf 'a%.0s' $(seq 48))"

run_summary() {  # $1... = .env lines -> echoes the rendered summary
    local sandbox="$TMP/summary.$RANDOM"
    mkdir -p "$sandbox"
    local line
    : > "$sandbox/.env"
    for line in "$@"; do printf '%s\n' "$line" >> "$sandbox/.env"; done
    {
        printf 'source %q\n' "$REPO_ROOT/deploy/lib.sh"
        # The two values install.sh computes earlier in the run.
        printf 'DASHBOARD_CREDS="(test)"\n'
        printf 'ADMIN_KEY=%q\n' "$FAKE_ADMIN_KEY"
        cat "$TMP/summary-block.sh"
    } > "$sandbox/run.sh"
    ( cd "$sandbox" && bash run.sh 2>&1 )
}

echo "install.sh summary (executed):"

OUT="$(run_summary 'VPS_IP=203.0.113.7' 'AUTH_ENABLED=true' 'BIND_ADDR=127.0.0.1')"
case "$OUT" in *'Auth:          ENFORCED'*) pass "auth on  -> reports ENFORCED" ;;
    *) fail "auth on  -> reports ENFORCED" ;; esac
case "$OUT" in *'ADMIN API KEY'*) pass "auth on  -> prints the admin key heading" ;;
    *) fail "auth on  -> prints the admin key heading" ;; esac
# The heading alone is NOT the check. Asserting only the heading let a planted
# defect that replaced the key with "(see the top of this run)" pass — which
# is precisely the failure this whole block exists to prevent, since the key
# scrolls away during the model pull. Demand the VALUE.
case "$OUT" in *"$FAKE_ADMIN_KEY"*) pass "auth on  -> re-surfaces the admin key VALUE" ;;
    *) fail "auth on  -> re-surfaces the admin key VALUE" ;; esac
# It must be immediately usable, not just displayed.
case "$OUT" in *"curl -H \"X-API-Key: $FAKE_ADMIN_KEY\""*)
    pass "auth on  -> gives a runnable first authenticated call" ;;
    *) fail "auth on  -> gives a runnable first authenticated call" ;; esac
case "$OUT" in *'Open Dashboard -> Devices -> Add device'*'firekeep-admin invite --agent'*)
    pass "auth on  -> shows the current device-enrollment paths" ;;
    *) fail "auth on  -> shows the current device-enrollment paths" ;; esac
# The false claim this replaces. Loopback ports are NOT exposed off-box.
case "$OUT" in *'SECURITY: service ports'*) fail "loopback -> must NOT print the exposure warning" ;;
    *) pass "loopback -> no bogus exposure warning" ;; esac
case "$OUT" in *'loopback only'*) pass "loopback -> says so on the dashboard URL" ;;
    *) fail "loopback -> says so on the dashboard URL" ;; esac
# A URL the operator cannot reach is a claim they disprove in one click.
case "$OUT" in *'http://203.0.113.7:8040'*) fail "loopback -> must not advertise the public URL" ;;
    *) pass "loopback -> advertises localhost, not the VPS IP" ;; esac
case "$OUT" in *'ssh -L 8040:127.0.0.1:8040'*) pass "loopback -> offers the tunnel instead" ;;
    *) fail "loopback -> offers the tunnel instead" ;; esac

OUT="$(run_summary 'VPS_IP=203.0.113.7' 'AUTH_ENABLED=true' 'BIND_ADDR=0.0.0.0')"
case "$OUT" in *'SECURITY: service ports 8040-8100 are published on 0.0.0.0'*)
    pass "public   -> prints the exposure warning" ;;
    *) fail "public   -> prints the exposure warning" ;; esac
case "$OUT" in *'http://203.0.113.7:8040'*) pass "public   -> advertises the reachable URL" ;;
    *) fail "public   -> advertises the reachable URL" ;; esac

# The dangerous combination, and the one that actually leaked secrets.
OUT="$(run_summary 'VPS_IP=203.0.113.7' 'AUTH_ENABLED=false' 'BIND_ADDR=0.0.0.0')"
case "$OUT" in *'Auth:'*'DISABLED'*) pass "auth off -> reports DISABLED" ;;
    *) fail "auth off -> reports DISABLED" ;; esac
case "$OUT" in *'ADMIN API KEY'*) fail "auth off -> must not print an admin key block" ;;
    *) pass "auth off -> no admin key block (there is no admin)" ;; esac
case "$OUT" in *'BOTH auth disabled AND non-loopback'*)
    pass "auth off + public -> escalates to the combined warning" ;;
    *) fail "auth off + public -> escalates to the combined warning" ;; esac

# Absent lines = the compose defaults. Must render the SECURE posture, because
# this is what an .env written before either setting existed looks like.
OUT="$(run_summary 'VPS_IP=203.0.113.7')"
case "$OUT" in *'Auth:          ENFORCED'*) pass "defaults -> ENFORCED" ;;
    *) fail "defaults -> ENFORCED" ;; esac
case "$OUT" in *'loopback only'*) pass "defaults -> loopback" ;;
    *) fail "defaults -> loopback" ;; esac

# An idempotent re-run mints nothing, so there is no plaintext to reprint. The
# summary must say the key is UNRECOVERABLE rather than imply it can be re-read.
run_summary_no_admin() {
    local sandbox="$TMP/summary-noadmin.$RANDOM"
    mkdir -p "$sandbox"
    printf 'VPS_IP=203.0.113.7\nAUTH_ENABLED=true\n' > "$sandbox/.env"
    {
        printf 'source %q\n' "$REPO_ROOT/deploy/lib.sh"
        printf 'DASHBOARD_CREDS="(test)"\n'
        printf 'ADMIN_KEY=""\n'
        cat "$TMP/summary-block.sh"
    } > "$sandbox/run.sh"
    ( cd "$sandbox" && bash run.sh 2>&1 )
}
OUT="$(run_summary_no_admin)"
case "$OUT" in *'not recoverable'*|*'NOT recoverable'*)
    pass "re-run   -> admits the admin key is unrecoverable" ;;
    *) fail "re-run   -> admits the admin key is unrecoverable" ;; esac
case "$OUT" in *'bash deploy/bootstrap-keys.sh'*) pass "re-run   -> gives the re-mint path" ;;
    *) fail "re-run   -> gives the re-mint path" ;; esac

# --- install.sh's admin-key CAPTURE, executed against real bootstrap output -
# The summary can only reprint what the capture extracted. That extraction is
# a lone grep against bootstrap-keys.sh's stdout — a coupling between two
# files with nothing but a regex holding it together, and if it silently
# returns empty the summary quietly switches to the "already provisioned"
# branch on a FRESH install and the operator never sees the only key that can
# administer their stack.
#
# Extracted from install.sh rather than retyped, for the same reason as above.
echo "install.sh admin-key capture (executed):"

CAPTURE="$(grep -E '^ADMIN_KEY="\$\(printf' install.sh)"
[ -n "$CAPTURE" ] || { echo "could not find the ADMIN_KEY capture line in install.sh"; exit 1; }

run_capture() {  # stdin = simulated bootstrap output -> echoes captured key
    local out
    out="$(cat)"
    BOOTSTRAP_OUT="$out" bash -c "
        set -euo pipefail
        $CAPTURE
        printf '%s' \"\$ADMIN_KEY\""
}

# Shape taken from deploy/bootstrap-keys.sh's actual fresh-run output.
GOT="$(run_capture <<EOF
[MINTED] FIREKEEP_INTERNAL_KEY  (agent_id=firekeep-internal scopes=["memory:write","session:read","eval:read","eval:write"])
[MINTED] DASHBOARD_API_KEY  (agent_id=firekeep-dashboard scopes=["*"])
[MINTED] RELAY_INTERNAL_API_KEY  (agent_id=firekeep-relay scopes=["session:write"])
[MINTED] FIREKEEP_BRIDGE_KEY  (agent_id=firekeep-bridge scopes=["memory:write","session:read","eval:read","eval:write","eval:grade"])

============================================================
  ADMIN API KEY — shown ONCE, not written to disk.
  Store it in your password manager now:

    ${FAKE_ADMIN_KEY}

  Use it with deploy/firekeep-admin to issue teammate keys.
============================================================

bootstrap-keys: done (5 key(s) minted)
EOF
)"
check "fresh run -> captures the admin key" "$FAKE_ADMIN_KEY" "$GOT"

# Idempotent re-run: nothing minted, no plaintext anywhere. Must come back
# EMPTY so the summary takes the honest "not recoverable" branch — and must
# not fail the script under `set -euo pipefail` when grep matches nothing.
GOT="$(run_capture <<'EOF'
[OK] FIREKEEP_INTERNAL_KEY already provisioned
[OK] DASHBOARD_API_KEY already provisioned
[OK] RELAY_INTERNAL_API_KEY already provisioned
[OK] FIREKEEP_BRIDGE_KEY already provisioned
[OK] admin key already provisioned (key_id 0123456789abcdef)

bootstrap-keys: done (0 key(s) minted)
EOF
)"
check "re-run    -> captures nothing (no plaintext exists)" "" "$GOT"

# The capture must not mistake a key NAME for a key value.
GOT="$(run_capture <<'EOF'
[MINTED] FIREKEEP_INTERNAL_KEY  (agent_id=firekeep-internal scopes=["memory:write"])
bootstrap-keys: done (1 key(s) minted)
EOF
)"
check "names only -> captures nothing" "" "$GOT"

# --- update.sh's migration guard, EXECUTED ---------------------------------
# update.sh is the ONLY path an already-vulnerable deployment takes. If this
# block is wrong the remedy reaches new installs only — and a pre-BIND_ADDR
# .env silently loses all remote reachability on a routine `git pull`.
sed -n '/^# --- Security-default migration guard/,/^# --- Rebuild ---/p' update.sh \
    | sed '$d' > "$TMP/migration-block.sh"
[ -s "$TMP/migration-block.sh" ] || { echo "could not extract update.sh's migration block"; exit 1; }

# The sandbox is built by the CALLER, not inside the runner: the runner is
# invoked in a command substitution, whose subshell cannot export a path back
# out — and these assertions have to inspect the .env the block WROTE, not
# only what it printed.
MIGRATE_N=0
new_migration_env() {  # $1... = .env lines -> echoes the sandbox path
    MIGRATE_N=$((MIGRATE_N + 1))
    local sandbox="$TMP/migrate.$MIGRATE_N"
    mkdir -p "$sandbox/deploy"
    cp "$REPO_ROOT/deploy/lib.sh" "$sandbox/deploy/lib.sh"
    local line
    : > "$sandbox/.env"
    for line in "$@"; do printf '%s\n' "$line" >> "$sandbox/.env"; done
    # $0 drives the block's `source "$(dirname "$0")/deploy/lib.sh"`.
    cp "$TMP/migration-block.sh" "$sandbox/update-fragment.sh"
    printf '%s\n' "$sandbox"
}

run_migration() {  # $1 = sandbox path -> echoes the block's output
    ( cd "$1" && bash update-fragment.sh 2>&1 )
}

echo "update.sh migration guard (executed):"

SB="$(new_migration_env 'VPS_IP=10.0.0.4' 'AUTH_ENABLED=false')"
OUT="$(run_migration "$SB")"
case "$OUT" in *'WARNING: AUTH IS DISABLED'*)
    pass "existing AUTH_ENABLED=false -> loud warning (remedy has NOT reached it)" ;;
    *) fail "existing AUTH_ENABLED=false -> loud warning (remedy has NOT reached it)" ;; esac

# No AUTH_ENABLED line: compose's default turns enforcement ON at this restart.
# Silently is not acceptable — unkeyed callers are about to start getting 401.
SB="$(new_migration_env 'VPS_IP=10.0.0.4')"
OUT="$(run_migration "$SB")"
case "$OUT" in *'AUTH IS NOW ENFORCED'*)
    pass "pre-auth .env -> announces enforcement turning on" ;;
    *) fail "pre-auth .env -> announces enforcement turning on" ;; esac
# ...and the same run must preserve reachability rather than silently
# rebinding six published ports to loopback mid-update.
case "$OUT" in *'BIND_ADDR=0.0.0.0 was added'*)
    pass "pre-BIND_ADDR .env -> announces the preserved binding" ;;
    *) fail "pre-BIND_ADDR .env -> announces the preserved binding" ;; esac
if grep -q '^BIND_ADDR=0.0.0.0$' "$SB/.env"; then
    pass "pre-BIND_ADDR .env -> actually writes BIND_ADDR=0.0.0.0"
else
    fail "pre-BIND_ADDR .env -> actually writes BIND_ADDR=0.0.0.0"
fi
# Idempotent: a second update must not append the line again.
run_migration "$SB" > /dev/null
if [ "$(grep -c '^BIND_ADDR=' "$SB/.env")" -eq 1 ]; then
    pass "pre-BIND_ADDR .env -> second run does not duplicate the line"
else
    fail "pre-BIND_ADDR .env -> second run duplicated BIND_ADDR"
fi

# An OFFICE deployment also has no BIND_ADDR line, but loopback is the
# intended posture there (docker-compose.office.yml rebinds every port to
# 127.0.0.1 behind Caddy :443). Writing 0.0.0.0 would preserve reachability
# nobody uses.
SB="$(new_migration_env 'VPS_IP=10.0.0.4' 'AUTH_ENABLED=true' 'FIREKEEP_OFFICE_MODE=true')"
OUT="$(run_migration "$SB")"
if grep -q '^BIND_ADDR=' "$SB/.env"; then
    fail "office deploy -> must NOT write BIND_ADDR"
else
    pass "office deploy -> BIND_ADDR left unset"
fi
case "$OUT" in *'Office deploy'*) pass "office deploy -> says why it left it alone" ;;
    *) fail "office deploy -> says why it left it alone" ;; esac

# A post-fix .env already carries the line: it must be left ALONE, or every
# update would quietly undo a deliberate lockdown.
SB="$(new_migration_env 'VPS_IP=10.0.0.4' 'AUTH_ENABLED=true' 'BIND_ADDR=127.0.0.1')"
OUT="$(run_migration "$SB")"
if [ "$(grep -c '^BIND_ADDR=' "$SB/.env")" -eq 1 ] && grep -q '^BIND_ADDR=127.0.0.1$' "$SB/.env"; then
    pass "configured BIND_ADDR -> left untouched"
else
    fail "configured BIND_ADDR -> left untouched (got: $(grep '^BIND_ADDR=' "$SB/.env" | tr '\n' ' '))"
fi
case "$OUT" in *'loopback only'*) pass "configured loopback -> reported, not rewritten" ;;
    *) fail "configured loopback -> reported, not rewritten" ;; esac
case "$OUT" in *'AUTH IS NOW ENFORCED'*|*'AUTH IS DISABLED'*)
    fail "already-correct .env -> must not raise a migration banner" ;;
    *) pass "already-correct .env -> quiet [OK] lines only" ;; esac

# --- install.sh's --insecure-no-auth block, EXECUTED ------------------------
# Run against a real copy of the shipped .env.example, not a hand-made
# fixture: the whole block turns on a sed matching the AUTH_ENABLED line that
# file actually contains, so a fixture would test my assumption about
# .env.example rather than .env.example itself.
echo "install.sh --insecure-no-auth (executed):"

sed -n '/^    # --- Deliberate auth opt-out/,/^    fi$/p' install.sh > "$TMP/optout-block.sh"
[ -s "$TMP/optout-block.sh" ] || { echo "could not extract install.sh's opt-out block"; exit 1; }

run_optout() {  # $1... = args passed to install.sh -> echoes sandbox path
    local sandbox="$TMP/optout.$RANDOM"
    mkdir -p "$sandbox"
    cp "$REPO_ROOT/.env.example" "$sandbox/.env"
    {
        printf 'source %q\n' "$REPO_ROOT/deploy/lib.sh"
        cat "$TMP/optout-block.sh"
    } > "$sandbox/run.sh"
    ( cd "$sandbox" && bash run.sh "$@" > stdout.txt 2> stderr.txt )
    printf '%s\n' "$sandbox"
}

# Shipped default, no flag: the file must come through untouched and enforcing.
SB="$(run_optout)"
if auth_enforced "$SB/.env"; then
    pass "no flag  -> .env stays AUTH_ENABLED=true"
else
    fail "no flag  -> .env stays AUTH_ENABLED=true"
fi
[ ! -s "$SB/stderr.txt" ] && pass "no flag  -> silent" || fail "no flag  -> silent"

# With the flag: enforcement genuinely off, announced on stderr, and exactly
# ONE AUTH_ENABLED line left (a duplicate would make the effective value
# depend on parse order).
SB="$(run_optout --insecure-no-auth)"
if auth_enforced "$SB/.env"; then
    fail "--insecure-no-auth -> .env actually disables auth"
else
    pass "--insecure-no-auth -> .env actually disables auth"
fi
check "--insecure-no-auth -> exactly one AUTH_ENABLED line" \
    "1" "$(grep -c '^AUTH_ENABLED=' "$SB/.env")"
case "$(cat "$SB/stderr.txt")" in
    *'AUTH DISABLED BY REQUEST'*) pass "--insecure-no-auth -> announced loudly on stderr" ;;
    *) fail "--insecure-no-auth -> announced loudly on stderr" ;;
esac
# The banner has to name what it exposes, or it is just noise -- and it has to
# name the RIGHT things. This assertion used to require the string
# "/vault/secrets", which was correct until audit blocker 7 was fixed and is now
# exactly backwards: with auth off, cortex/app/main.py refuses to mount the vault
# and auth routers, so /vault/* and /auth/* are the only surfaces that are
# CLOSED (503). A warning that points at the two closed doors while /memory/*
# and the whole MCP surface stand open misdirects the reader precisely when they
# are paying attention.
BANNER="$(cat "$SB/stderr.txt")"
case "$BANNER" in
    *'/memory/learn'*|*'/memory/recall'*) pass "--insecure-no-auth -> names surfaces that are actually open" ;;
    *) fail "--insecure-no-auth -> names surfaces that are actually open" ;;
esac
# ...and must not present the unmounted admin surface as an exposure.
if printf '%s' "$BANNER" | grep -qE '(reads your stored secrets|mints new API keys)'; then
    fail "--insecure-no-auth -> does not describe /vault or /auth/keys as exposed"
else
    pass "--insecure-no-auth -> does not describe /vault or /auth/keys as exposed"
fi
# Nothing else in .env may be collateral damage.
if grep -q '^BIND_ADDR=127.0.0.1$' "$SB/.env"; then
    pass "--insecure-no-auth -> leaves the rest of .env intact"
else
    fail "--insecure-no-auth -> leaves the rest of .env intact"
fi

# A hand-edited .env with no AUTH_ENABLED line at all must still end up off,
# not silently enforcing because the sed found nothing to replace.
SB2="$TMP/optout-noline"
mkdir -p "$SB2"
printf 'VPS_IP=10.0.0.4\n' > "$SB2/.env"
{ printf 'source %q\n' "$REPO_ROOT/deploy/lib.sh"; cat "$TMP/optout-block.sh"; } > "$SB2/run.sh"
( cd "$SB2" && bash run.sh --insecure-no-auth > /dev/null 2>&1 )
if auth_enforced "$SB2/.env"; then
    fail "--insecure-no-auth on an .env with no AUTH_ENABLED line -> appends it"
else
    pass "--insecure-no-auth on an .env with no AUTH_ENABLED line -> appends it"
fi

# --- bootstrap-keys.sh must mint Relay's outbound key -----------------------
# compose has always read RELAY_INTERNAL_API_KEY; nothing minted it. Harmless
# while auth was off, a silent 401 once it is on.
echo "bootstrap-keys:"
BOOTSTRAP="$(cat deploy/bootstrap-keys.sh)"
case "$BOOTSTRAP" in
    *'ensure_env_key RELAY_INTERNAL_API_KEY'*) pass "mints RELAY_INTERNAL_API_KEY" ;;
    *) fail "mints RELAY_INTERNAL_API_KEY" ;;
esac
# Least privilege: Bridge gates POST /sessions/{agent_id}/context on
# session:write and that is Relay's only outbound call. "*" here would hand
# Relay vault reads and key minting.
case "$BOOTSTRAP" in
    *'ensure_env_key RELAY_INTERNAL_API_KEY firekeep-relay '"'"'["session:write"]'"'"*)
        pass "relay key is scoped to session:write only" ;;
    *) fail "relay key is scoped to session:write only" ;;
esac

# --- bootstrap-keys.sh must mint Bridge's dedicated eval:grade key ----------
# Task 5: eval:grade is service-only and reaches exactly one credential,
# minted via ensure_env_key (never a hand-rolled echo — see the constraint
# recorded at bootstrap-keys.sh:192-200 about install.sh's single-nxs_-token
# admin-key capture).
case "$BOOTSTRAP" in
    *'ensure_env_key FIREKEEP_BRIDGE_KEY'*) pass "mints FIREKEEP_BRIDGE_KEY" ;;
    *) fail "mints FIREKEEP_BRIDGE_KEY" ;;
esac
case "$BOOTSTRAP" in
    *'ensure_env_key FIREKEEP_BRIDGE_KEY firekeep-bridge '"'"'["memory:write","session:read","eval:read","eval:write","eval:grade"]'"'"*)
        pass "bridge key carries exactly the internal scopes plus eval:grade" ;;
    *) fail "bridge key carries exactly the internal scopes plus eval:grade" ;;
esac

# --- vault_status_line must not claim a control that is not serving ----------
# Audit Major 20 was "the installer reports Vault: Enabled even when it skipped
# key generation". It was fixed by checking VAULT_KEY -- and came back in a new
# form, because a keyed vault on an auth-off install is ALSO not serving: the
# router is unmounted and every /vault/* path answers 503.
VS="$TMP/vaultstatus"
mkdir -p "$VS"
printf 'VAULT_KEY=abc123
AUTH_ENABLED=false
' > "$VS/.env"
OUT="$(bash -c "source '$REPO_ROOT/deploy/lib.sh'; vault_status_line '$VS/.env'")"
case "$OUT" in
    *'NOT SERVED'*) pass "vault_status_line: keyed + auth off -> not claimed as Enabled" ;;
    *) fail "vault_status_line: keyed + auth off -> not claimed as Enabled (got: $OUT)" ;;
esac
printf 'VAULT_KEY=abc123
AUTH_ENABLED=true
' > "$VS/.env"
OUT="$(bash -c "source '$REPO_ROOT/deploy/lib.sh'; vault_status_line '$VS/.env'")"
case "$OUT" in
    *'Enabled'*) pass "vault_status_line: keyed + auth on -> Enabled" ;;
    *) fail "vault_status_line: keyed + auth on -> Enabled (got: $OUT)" ;;
esac

# --- the exposure warning must not recommend a firewall that cannot work -----
# ufw never governed these ports: Docker's published-port DNAT lands in the
# DOCKER chain, traversed before ufw's rules. This advice was printed for months
# and acted on once, over a still-exposed wallet key.
if grep -q 'ufw allow from' "$REPO_ROOT/install.sh"; then
    fail "install.sh: no 'ufw allow' advice (Docker bypasses ufw)"
else
    pass "install.sh: no 'ufw allow' advice (Docker bypasses ufw)"
fi
if grep -q 'DOCKER-USER' "$REPO_ROOT/install.sh"; then
    pass "install.sh: points at DOCKER-USER, the chain that IS consulted"
else
    fail "install.sh: points at DOCKER-USER, the chain that IS consulted"
fi

echo ""
if [ "$FAILS" -eq 0 ]; then
    echo "PASS: auth posture helpers + call sites"
else
    echo "FAIL: $FAILS assertion(s) failed"
    exit 1
fi
