"""Build the source-free deployment bundle consumed by ``firekeep init``.

The application code ships in public container images.  This archive contains
only the compose/configuration surface needed to run those images; it must not
grow into a second source distribution by accident.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path


RELEASE_TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")

BUNDLE_FILES = (
    ".env.example",
    "docker-compose.yml",
    "docker-compose.office.yml",
    "install.sh",
    "update.sh",
    "start.sh",
    "stop.sh",
    "LICENSE",
    "NOTICE",
    "deploy/lib.sh",
    "deploy/bootstrap-keys.sh",
    "deploy/firekeep-admin",
    "deploy/backup.sh",
    "deploy/restore.sh",
    "deploy/support-bundle.sh",
    "deploy/Caddyfile",
    "dashboard/index.html",
    "dashboard/nginx.conf.template",
    "dashboard/brand/favicon.svg",
    "dashboard/brand/lockup.svg",
    "dashboard/brand/mark-ember.svg",
    "dashboard/brand/mark.svg",
    "dashboard/brand/README.md",
    "docs/DEPLOYMENT.md",
    "docs/DEPLOYMENT-OFFICE.md",
    "docs/LICENSING.md",
    "docs/THIRD-PARTY-DATASTORES.md",
)


def _validate_tag(version: str) -> str:
    if not RELEASE_TAG.fullmatch(version):
        raise ValueError(f"invalid server release tag {version!r}")
    return version


def _semver(version: str) -> tuple[tuple[int, int, int], tuple[tuple[int, int | str], ...] | None]:
    version = _validate_tag(version)
    core, separator, prerelease = version[1:].partition("-")
    numbers = tuple(int(part) for part in core.split("."))
    identifiers: tuple[tuple[int, int | str], ...] | None = None
    if separator:
        identifiers = tuple(
            (0, int(part)) if part.isdigit() else (1, part)
            for part in prerelease.split(".")
        )
    return numbers, identifiers  # type: ignore[return-value]


def is_newer_release(candidate: str, current: str) -> bool:
    """SemVer ordering, including the rule that a final beats its prerelease."""
    candidate_core, candidate_pre = _semver(candidate)
    current_core, current_pre = _semver(current)
    if candidate_core != current_core:
        return candidate_core > current_core
    if candidate_pre is None or current_pre is None:
        return candidate_pre is None and current_pre is not None
    return candidate_pre > current_pre


def build_bundle(repo: Path, output: Path, version: str) -> tuple[Path, Path]:
    """Create the tarball and its strict JSON manifest."""
    version = _validate_tag(version)
    repo = repo.resolve()
    output.mkdir(parents=True, exist_ok=True)
    missing = [name for name in BUNDLE_FILES if not (repo / name).is_file()]
    if missing:
        raise FileNotFoundError(f"server bundle inputs are missing: {', '.join(missing)}")

    root = f"firekeep-server-{version}"
    archive = output / f"{root}.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        for relative in BUNDLE_FILES:
            source = repo / relative
            if relative != ".env.example":
                # Normalise CRLF -> LF. The bundle is extracted and executed on a
                # customer's LINUX host, where `#!/usr/bin/env bash\r` is not a
                # valid shebang — the script dies with a bare "bad interpreter"
                # naming a path that looks correct.
                #
                # Git's Windows autocrlf converts on CHECKOUT, so the repository
                # blobs are LF (verified) while the working tree this reads from
                # is CRLF. CI builds the real release on ubuntu-latest and is
                # unaffected, which is exactly why this is worth fixing rather
                # than muting: the one build that produces a broken bundle is a
                # developer's local one, and it is broken in a way nothing on
                # their machine can execute to discover.
                #
                # Applied to every bundled file, not just *.sh: the bundle also
                # carries compose YAML, which is LF by convention, and there is
                # no bundled file for which CRLF is correct. Byte-identical on a
                # checkout that is already LF.
                data = source.read_bytes().replace(b"\r\n", b"\n")
                info = tarfile.TarInfo(f"{root}/{relative}")
                info.size = len(data)
                info.mode = source.stat().st_mode & 0o777
                tar.addfile(info, io.BytesIO(data))
                continue
            env = re.sub(
                r"(?m)^IMAGE_TAG=.*$",
                f"IMAGE_TAG={version}",
                source.read_text(encoding="utf-8"),
            ).encode().replace(b"\r\n", b"\n")
            info = tarfile.TarInfo(f"{root}/{relative}")
            info.size = len(env)
            info.mode = source.stat().st_mode & 0o777
            tar.addfile(info, io.BytesIO(env))

        marker = (
            json.dumps({"version": version, "distribution": "container-images"}, indent=2)
            + "\n"
        ).encode()
        info = tarfile.TarInfo(f"{root}/SERVER_BUNDLE.json")
        info.size = len(marker)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(marker))

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = output / "server.json"
    manifest.write_text(
        json.dumps(
            {"version": version, "file": archive.name, "sha256": digest},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return archive, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Firekeep server deployment bundle")
    parser.add_argument("version", help="server release tag, for example v0.1.0")
    parser.add_argument("output", type=Path)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    archive, manifest = build_bundle(args.repo, args.output, args.version)
    print(archive)
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
