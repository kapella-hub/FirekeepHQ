"""Verified download and safe extraction for ``firekeep init`` server bundles."""

from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from firekeep_client import updater
from firekeep_client.transport import TransportError, get_json


DEFAULT_DIST_BASE = "https://kapella-hub.github.io/firekeep-dist"
_RELEASE_TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED = ("install.sh", "docker-compose.yml", ".env.example", "SERVER_BUNDLE.json")


class ServerInitError(Exception):
    """A public bundle could not be obtained or safely unpacked."""


@dataclass(frozen=True)
class ServerManifest:
    version: str
    file: str
    sha256: str


def _manifest_path(version: str | None) -> str:
    if version is not None and not _RELEASE_TAG.fullmatch(version):
        raise ServerInitError(f"invalid server version {version!r}; expected vMAJOR.MINOR.PATCH")
    return f"server/{version or 'latest'}/server.json"


def fetch_manifest(
    base: str, *, version: str | None = None, timeout: float = 10.0
) -> ServerManifest:
    url = f"{base.rstrip('/')}/{_manifest_path(version)}"
    try:
        data = get_json(url, headers={}, timeout=timeout, verify=True)
    except (TransportError, OSError) as exc:
        raise ServerInitError(f"cannot reach the server release manifest at {url}: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {"version", "file", "sha256"}:
        raise ServerInitError(f"malformed server release manifest at {url}")
    if not all(isinstance(data[name], str) for name in ("version", "file", "sha256")):
        raise ServerInitError(f"malformed server release manifest at {url}")
    release = ServerManifest(data["version"], data["file"], data["sha256"].lower())
    expected_file = f"firekeep-server-{release.version}.tar.gz"
    if (
        not _RELEASE_TAG.fullmatch(release.version)
        or release.file != expected_file
        or not _SHA256.fullmatch(release.sha256)
        or (version is not None and release.version != version)
    ):
        raise ServerInitError(f"invalid server release manifest at {url}")
    return release


def _safe_extract(archive: Path, destination: Path, expected_root: str) -> Path:
    """Extract regular files/directories only, with no traversal or link entries."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if not members:
            raise ServerInitError("server bundle is empty")
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or "\\" in member.name
                or "\x00" in member.name
                or not path.parts
                or path.parts[0] != expected_root
                or any(part in {"", ".", ".."} for part in path.parts)
                or not (member.isdir() or member.isfile())
            ):
                raise ServerInitError(f"unsafe entry in server bundle: {member.name!r}")

        for member in members:
            relative = PurePosixPath(member.name)
            target = destination.joinpath(*relative.parts).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ServerInitError(f"unsafe entry in server bundle: {member.name!r}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise ServerInitError(f"cannot read server bundle entry: {member.name!r}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            try:
                target.chmod(member.mode & 0o777)
            except OSError:
                pass
    return destination / expected_root


def _is_bundle(root: Path) -> bool:
    return all((root / name).is_file() for name in _REQUIRED)


def _bundle_version(root: Path) -> str | None:
    try:
        marker = json.loads((root / "SERVER_BUNDLE.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = marker.get("version") if isinstance(marker, dict) else None
    return version if isinstance(version, str) else None


def previous_bundle_path(root: Path) -> Path:
    version = _bundle_version(root) or "unknown"
    return root.with_name(f".{root.name}.previous-{version}")


def _set_image_tag(env_file: Path, version: str) -> None:
    lines = env_file.read_text(encoding="utf-8").splitlines()
    replaced = False
    result: list[str] = []
    for line in lines:
        if line.startswith("IMAGE_TAG="):
            result.append(f"IMAGE_TAG={version}")
            replaced = True
        else:
            result.append(line)
    if not replaced:
        result.append(f"IMAGE_TAG={version}")
    env_file.write_text("\n".join(result) + "\n", encoding="utf-8")


def _carry_runtime_state(current: Path, replacement: Path, version: str) -> None:
    for relative in (".env", "dashboard/.htpasswd", "dashboard/.htpasswd.cred"):
        source = current / relative
        if source.is_file():
            target = replacement / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    if (replacement / ".env").is_file():
        _set_image_tag(replacement / ".env", version)


def _publish_replacement(current: Path, replacement: Path, version: str) -> None:
    backup = previous_bundle_path(current)
    if backup.exists():
        raise ServerInitError(
            f"previous server bundle backup already exists: {backup}; archive or remove it first"
        )
    _carry_runtime_state(current, replacement, version)
    os.replace(current, backup)
    try:
        os.replace(replacement, current)
    except OSError:
        os.replace(backup, current)
        raise
    old_backups = backup / "backups"
    if old_backups.is_dir() and not (current / "backups").exists():
        try:
            os.replace(old_backups, current / "backups")
        except OSError:
            # The deployment is already valid. Historical backups remain at the
            # printed/recoverable previous-bundle path rather than failing update.
            pass


def download_bundle(
    base: str,
    destination: Path,
    *,
    version: str | None = None,
    timeout: float = 120.0,
) -> Path:
    """Download into a temporary directory and publish only a complete bundle."""
    destination = destination.expanduser().resolve()
    replacing = False
    if destination.exists():
        if _is_bundle(destination) and (
            version is None or _bundle_version(destination) == version
        ):
            return destination
        if _is_bundle(destination) and version is not None:
            replacing = True
        elif any(destination.iterdir()):
            raise ServerInitError(
                f"server directory is not empty and is not a Firekeep bundle: {destination}"
            )
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = fetch_manifest(base, version=version)
    archive_url = f"{base.rstrip('/')}/server/{manifest.version}/{manifest.file}"

    with tempfile.TemporaryDirectory(prefix=".firekeep-server-", dir=destination.parent) as raw:
        temporary = Path(raw)
        archive = updater.download(
            archive_url,
            temporary / manifest.file,
            sha256=manifest.sha256,
            timeout=timeout,
        )
        try:
            extracted = _safe_extract(
                archive, temporary / "unpacked", f"firekeep-server-{manifest.version}"
            )
        except (OSError, tarfile.TarError) as exc:
            raise ServerInitError(f"cannot unpack the verified server bundle: {exc}") from exc
        if not _is_bundle(extracted):
            raise ServerInitError("downloaded server bundle is incomplete")
        try:
            marker = json.loads(
                (extracted / "SERVER_BUNDLE.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ServerInitError("server bundle marker is malformed") from exc
        if marker.get("version") != manifest.version:
            raise ServerInitError("server bundle version does not match its manifest")
        if replacing:
            _publish_replacement(destination, extracted, manifest.version)
        else:
            if destination.exists():
                destination.rmdir()
            os.replace(extracted, destination)
    return destination
