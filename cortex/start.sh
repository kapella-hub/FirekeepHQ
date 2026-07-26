#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure .env exists
if [ ! -f .env ]; then
    echo "No .env file found. Copying from .env.example..."
    cp .env.example .env
    echo "Please edit .env with your configuration, then re-run this script."
    exit 1
fi

echo "Starting FirekeepCortex services..."
docker compose up --build -d

echo ""
echo "Waiting for services to become healthy..."
docker compose ps

echo ""
echo "FirekeepCortex is starting up."
echo "  Services may take 30-60s to become healthy."
echo "  Use 'docker compose logs -f api' to follow API logs."
echo ""
echo "  API:    http://localhost:8100"
echo "  Health: http://localhost:8100/health"
echo "  MCP:    http://localhost:8080/mcp"
echo "  Neo4j:  http://localhost:7474"
echo "  Qdrant: http://localhost:6333/dashboard"
echo ""
echo "Use './stop.sh' to shut down."
