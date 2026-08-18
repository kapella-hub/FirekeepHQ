"""Guards the public MCP Registry record against release and positioning drift."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CURRENT_SCHEMA = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
MCP_NAME = "io.github.kapella-hub/firekeep"


def _manifest() -> dict:
    return json.loads((ROOT / "server.json").read_text(encoding="utf-8"))


def _client_version() -> str:
    data = tomllib.loads((ROOT / "client/pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _client_project() -> dict:
    data = tomllib.loads((ROOT / "client/pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]


def test_manifest_uses_current_registry_contract() -> None:
    data = _manifest()
    assert data["$schema"] == CURRENT_SCHEMA
    assert data["name"] == MCP_NAME
    assert data["title"] == "Firekeep"
    assert 1 <= len(data["description"]) <= 100
    assert "status" not in data, "status is owned by the Registry, not the publisher"


def test_registry_and_client_versions_cannot_drift() -> None:
    data = _manifest()
    packages = [p for p in data["packages"] if p.get("identifier") == "firekeep-client"]
    assert len(packages) == 1
    assert data["version"] == _client_version()
    assert packages[0]["version"] == _client_version()


def test_registry_package_launches_the_local_gateway_honestly() -> None:
    package = next(p for p in _manifest()["packages"] if p["identifier"] == "firekeep-client")
    assert package["registryType"] == "pypi"
    assert package["transport"] == {"type": "stdio"}
    assert package["runtimeHint"] == "uvx"
    assert package["packageArguments"][0]["type"] == "positional"
    assert package["packageArguments"][0]["value"] == "gateway"
    assert "self-hosted Keep" in package["packageArguments"][0]["description"]
    assert package["packageArguments"][1] == {
        "type": "named",
        "name": "--runtime",
        "value": "generic",
        "description": (
            "Identify Registry-launched connections as generic MCP clients; "
            "hook-driven lifecycle automation is not implied."
        ),
    }

    scripts = _client_project()["scripts"]
    assert scripts[package["identifier"]] == "firekeep_client.cli:main"


def test_pypi_readme_keeps_the_registry_ownership_marker() -> None:
    readme = (ROOT / "client/README.md").read_text(encoding="utf-8")
    assert f"mcp-name: {MCP_NAME}" in readme
