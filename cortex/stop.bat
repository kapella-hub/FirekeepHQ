@echo off
cd /d "%~dp0"

echo Stopping FirekeepCortex services...
docker compose down

echo FirekeepCortex stopped.
echo Data volumes preserved. Use 'docker compose down -v' to remove volumes.
