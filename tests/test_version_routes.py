"""Every service must answer 'what version are you running?'.

Route-level tests live here rather than per-service because the point is that
all four agree on one contract.
"""
import pytest

SERVICES = ["bridge", "relay", "sentinel"]


@pytest.mark.parametrize("service", SERVICES)
def test_service_module_registers_a_version_route(service):
    """The route must exist and be named, matching the /health precedent."""
    source = open(f"{service}/app/mcp_server.py", encoding="utf-8").read()
    assert '"/version"' in source, f"{service} has no /version route"
    assert 'name="version"' in source, f"{service}'s /version route is unnamed"


@pytest.mark.parametrize("service", SERVICES)
def test_service_reports_its_own_name(service):
    """A bundle collecting three /version payloads must be unambiguous."""
    source = open(f"{service}/app/mcp_server.py", encoding="utf-8").read()
    assert f'get_version_info("{service}")' in source


@pytest.mark.parametrize("service", SERVICES)
def test_dockerfile_declares_build_provenance_args(service):
    source = open(f"{service}/Dockerfile", encoding="utf-8").read()
    for arg in ("GIT_SHA", "BUILD_TIME", "APP_VERSION"):
        assert f"ARG {arg}" in source, f"{service}/Dockerfile lacks ARG {arg}"
        assert arg in source.split("ARG APP_VERSION")[-1] or f"ENV {arg}" in source \
            or f"{arg}=${{{arg}}}" in source, f"{service}/Dockerfile never ENVs {arg}"


@pytest.mark.parametrize("service", SERVICES)
def test_compose_passes_build_provenance(service):
    """A Dockerfile ARG nobody passes is a default nobody overrides."""
    import yaml
    compose = yaml.safe_load(open("docker-compose.yml", encoding="utf-8"))
    args = compose["services"][service]["build"]["args"]
    for arg in ("GIT_SHA", "BUILD_TIME", "APP_VERSION"):
        assert arg in args, f"compose does not pass {arg} to {service}"


def test_installer_exports_app_version():
    """install.sh already exports GIT_SHA and BUILD_TIME; APP_VERSION was missing,
    so every image would have reported the Dockerfile's hardcoded default."""
    for script in ("install.sh", "update.sh"):
        source = open(script, encoding="utf-8").read()
        assert "export APP_VERSION=" in source, f"{script} does not export APP_VERSION"
