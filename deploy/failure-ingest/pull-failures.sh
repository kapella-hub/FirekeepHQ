#!/bin/sh
# Field-failure pull: ping -> fetch sealed segments to a durable inbox ->
# delete remote after verified local write -> ingest.py -> done/.
# Cron: */30 * * * * /opt/firekeep/failure-ingest/pull-failures.sh
set -eu
HOST="u784952002@82.180.175.177"      # the documented Hostinger ssh endpoint
PORT=65002
REMOTE="domains/firekeep.ai/failure-stats"
BASE="/var/lib/firekeep/failure-ingest"
INBOX="${BASE}/inbox"; DONE="${BASE}/done"; mkdir -p "${INBOX}" "${DONE}"

# 1. maintenance ping (spec: seals age-ripe segments under the PHP lock and
#    gives the watchdog an unambiguous signal). Touch the marker only on 200.
if curl -fsS --max-time 10 -H 'Content-Type: application/json' \
     -d '{"events":[]}' https://firekeep.ai/failure-report.php >/dev/null; then
    touch "${BASE}/last-ping-ok"
fi

# 2. fetch each sealed segment fully, verify byte count, delete remote only
#    after the local copy is durable (crash between = harmless refetch).
for seg in $(ssh -p "${PORT}" "${HOST}" "ls ${REMOTE}/failures.*.log 2>/dev/null" || true); do
    name="$(basename "${seg}")"
    scp -P "${PORT}" -q "${HOST}:${seg}" "${INBOX}/${name}.part"
    remote_size="$(ssh -p "${PORT}" "${HOST}" "wc -c < ${seg}")"
    local_size="$(wc -c < "${INBOX}/${name}.part")"
    [ "${remote_size}" = "${local_size}" ] || { rm -f "${INBOX}/${name}.part"; continue; }
    sync "${INBOX}/${name}.part" 2>/dev/null || sync
    mv "${INBOX}/${name}.part" "${INBOX}/${name}"
    ssh -p "${PORT}" "${HOST}" "rm ${REMOTE}/${seg##*/}" || true
done

# 3. re-validate, aggregate, POST to Sentinel; segments move to done/ on 202.
#    Deliberately NOT `exec`: step 4 below needs the shell back afterward.
#    ingest.py's main() never raises on a per-segment POST failure (it logs
#    to stderr and leaves that one segment in inbox/ for the next pull), so
#    this exits 0 under normal partial-failure conditions and `set -e` still
#    lets step 4 run.
python3 /opt/firekeep/failure-ingest/ingest.py \
    --inbox "${INBOX}" --done "${DONE}" \
    --sentinel "http://100.91.3.51:8060" --api-key-env FIREKEEP_INTERNAL_KEY

# 4. retention: done/ is raw local retention only (Sentinel already has the
#    aggregated events) -- keep 14 days of processed segments, then drop them.
find "${DONE}" -mtime +14 -delete
