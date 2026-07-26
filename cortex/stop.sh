#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "Stopping FirekeepCortex services..."
docker compose down

echo "FirekeepCortex stopped."
echo "Data volumes preserved. Use 'docker compose down -v' to remove volumes."
