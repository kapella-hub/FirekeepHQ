#!/usr/bin/env bash
set -euo pipefail

# Server teardown for THIS Firekeep deployment. Removes the containers, the
# networks AND the data volumes — Neo4j graph, Qdrant vectors, Redis state — so
# every memory the team ever wrote is deleted. Ships in the release bundle
# beside install.sh/update.sh; the client kit is removed separately with
# `firekeep uninstall`.
#
# cd to this script's own directory so `docker compose` always targets the
# deployment this script sits in, regardless of where it was invoked from — the
# bundle is unpacked and run in place.
cd "$(dirname "$0")"

# Confirmation is required unless the operator opts out explicitly, exactly one
# of two ways: the --yes flag or FIREKEEP_UNINSTALL_YES=1 (for an unattended
# teardown). A bare run always prompts.
YES=0
for arg in "$@"; do
    case "$arg" in
        --yes|-y) YES=1 ;;
        *) echo "Unknown argument: $arg"; echo "Usage: $0 [--yes]"; exit 1 ;;
    esac
done
[ "${FIREKEEP_UNINSTALL_YES:-}" = "1" ] && YES=1

echo "============================================"
echo "  Firekeep Uninstall"
echo "============================================"
echo ""
echo "############################################################"
echo "# DATA LOSS WARNING"
echo "#"
echo "# This removes the Firekeep stack AND its data volumes:"
echo "#   neo4j_data   — the knowledge graph"
echo "#   qdrant_data  — every vector embedding"
echo "#   redis_data   — sessions, queues, relay state, auth keys"
echo "#   ollama_data  — downloaded models (large, but re-pullable)"
echo "#"
echo "# ALL TEAM MEMORY IS DELETED, and this cannot be undone."
echo "# Take a backup first if there is any chance you want it back:"
echo "#   bash deploy/backup.sh"
echo "############################################################"
echo ""

if [ "$YES" -ne 1 ]; then
    printf 'Type "yes" to delete this deployment and ALL its data: '
    # `|| true`: under `set -e` a closed/EOF stdin makes read exit non-zero and
    # would abort here with no message. Treat EOF as "not yes" so a
    # non-interactive run refuses cleanly instead of dying — refusing is the
    # safe default when no confirmation could be given.
    reply=""
    read -r reply || true
    if [ "$reply" != "yes" ]; then
        echo ""
        echo "Aborted. Nothing was removed."
        echo "  (Pass --yes or set FIREKEEP_UNINSTALL_YES=1 for an unattended teardown.)"
        exit 1
    fi
fi

echo ""
echo "Removing containers, networks and data volumes..."
docker compose down -v --remove-orphans

echo ""
echo "[OK] Stack and all data volumes removed."
echo ""
echo "This deployment directory can now be deleted:"
echo "  ${PWD}"
echo ""
echo "The client kit on your workstation is separate — remove it with:"
echo "  firekeep uninstall"
