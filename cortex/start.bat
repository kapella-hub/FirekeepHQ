@echo off
cd /d "%~dp0"

if not exist .env (
    echo No .env file found. Copying from .env.example...
    copy .env.example .env
    echo Please edit .env with your configuration, then re-run this script.
    exit /b 1
)

echo Starting FirekeepCortex services...
docker compose up --build -d

echo.
echo Waiting for services to become healthy...
docker compose ps

echo.
echo FirekeepCortex is starting up.
echo   Services may take 30-60s to become healthy.
echo   Use 'docker compose logs -f api' to follow API logs.
echo.
echo   API:    http://localhost:8000
echo   Health: http://localhost:8000/health
echo   MCP:    http://localhost:8080/mcp
echo   Neo4j:  http://localhost:7474
echo   Qdrant: http://localhost:6333/dashboard
echo.
echo Use 'stop.bat' to shut down.
