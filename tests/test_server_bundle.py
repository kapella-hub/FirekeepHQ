from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

from deploy.build_server_bundle import BUNDLE_FILES, build_bundle, is_newer_release


REPO = Path(__file__).resolve().parents[1]


def test_server_bundle_contains_deployment_surface_without_service_source(tmp_path) -> None:
    archive, manifest_path = build_bundle(REPO, tmp_path, "v1.2.3")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest == {
        "file": "firekeep-server-v1.2.3.tar.gz",
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "version": "v1.2.3",
    }
    with tarfile.open(archive, "r:gz") as tar:
        names = set(tar.getnames())
        env = tar.extractfile("firekeep-server-v1.2.3/.env.example")
        assert env is not None
        assert "IMAGE_TAG=v1.2.3" in env.read().decode()
        for name in names:
            if name.endswith(".sh"):
                script = tar.extractfile(name)
                assert script is not None
                assert b"\r\n" not in script.read(), f"{name} is not runnable on Linux"
    root = "firekeep-server-v1.2.3"
    assert {f"{root}/{name}" for name in BUNDLE_FILES} <= names
    assert f"{root}/SERVER_BUNDLE.json" in names
    assert not any(
        name.startswith(f"{root}/{service}/")
        for name in names
        for service in ("auth", "bridge", "cortex", "relay", "sentinel", "vault")
    )


def test_server_release_workflow_publishes_bundle_to_public_dist() -> None:
    workflow = (REPO / ".github/workflows/server-release.yml").read_text(encoding="utf-8")
    assert "deploy/build_server_bundle.py" in workflow
    assert "kapella-hub/firekeep-dist" in workflow
    assert 'gh/server/${TAG}' in workflow
    assert "gh/server/latest" in workflow


def test_source_free_update_routes_through_verified_client_bundle() -> None:
    script = (REPO / "update.sh").read_text(encoding="utf-8")
    assert "SERVER_BUNDLE.json" in script
    assert 'bash deploy/backup.sh' in script
    assert 'firekeep init --server-dir "$(pwd)" --version "$TO_VERSION"' in script


def test_server_latest_pointer_uses_semver_not_lexical_order() -> None:
    assert is_newer_release("v1.10.0", "v1.9.9")
    assert is_newer_release("v1.0.0", "v1.0.0-rc.2")
    assert is_newer_release("v1.0.0-rc.10", "v1.0.0-rc.2")
    assert not is_newer_release("v1.0.0-rc.2", "v1.0.0")
