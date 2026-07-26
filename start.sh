#!/usr/bin/env bash
set -euo pipefail

echo "============================================"
echo "  Firekeep Start"
echo "============================================"
echo ""

if [ ! -f .env ]; then
    echo "ERROR: .env file not found. Run install.sh first."
    exit 1
fi

echo "Starting all services..."
docker compose up -d

echo ""
echo "Verifying health..."

services=(
    "Cortex API:8100"
    "Cortex MCP:8080"
    "FirekeepBridge:8070"
    "FirekeepSentinel:8060"
    "FirekeepRelay:8050"
    "Dashboard:8040"
)

FAILED=0

for svc in "${services[@]}"; do
    name="${svc%%:*}"
    port="${svc##*:}"
    printf "  %-16s " "$name"
    for i in $(seq 1 30); do
        if curl -sf --max-time 2 "http://localhost:${port}/" &>/dev/null || curl -sf --max-time 2 "http://localhost:${port}/health" &>/dev/null || bash -c "</dev/tcp/localhost/${port}" 2>/dev/null; then
            echo "[OK]"
            break
        fi
        if [ "$i" -eq 30 ]; then
            echo "[TIMEOUT]"
            FAILED=1
        fi
        sleep 2
    done
done

if [ "$FAILED" -eq 1 ]; then
    echo ""
    echo "WARNING: Some services failed to start. Check: docker compose logs"
    exit 1
fi

echo ""
echo "Stack started."
echo ""
docker compose ps
