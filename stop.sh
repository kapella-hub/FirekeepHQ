#!/usr/bin/env bash
set -euo pipefail

CLEAN=false
for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN=true ;;
        *) echo "Unknown argument: $arg"; echo "Usage: $0 [--clean]"; exit 1 ;;
    esac
done

echo "============================================"
echo "  Firekeep Stop"
echo "============================================"
echo ""

if [ "$CLEAN" = true ]; then
    echo "Mode: full teardown (containers + networks removed, volumes preserved)"
    echo ""
    docker compose down
else
    echo "Mode: graceful stop (containers stopped, state preserved)"
    echo "      Use --clean to remove containers and networks."
    echo ""
    docker compose stop
fi

echo ""
echo "Done."
