#!/usr/bin/env python3
"""Build a REAL Firekeep dist from the working tree, laid out exactly like the
published one at https://firekeep.ai/ (which proxies kapella-hub.github.io/firekeep-dist).

Why this exists: every install bug the product has ever shipped lives in the seam
between "what the release job builds" and "what a stranger's machine does with it".
CI tests that seam only for artifacts that are already public, so a FIX cannot be
tested before it ships. This builds the same artifacts from uncommitted code, so the
lab can run the published one-liner against a dist containing changes that exist
nowhere but this checkout.

Layout produced (mirrors .github/workflows/release.yml's `gh/` tree):

    <out>/latest/latest.json            <- version pointer the bootstrap reads first
    <out>/latest/install.sh             <- the published one-liner, dist-base BAKED
    <out>/latest/install.ps1
    <out>/<version>/SHA256SUMS
    <out>/<version>/uv-<target>         <- one per platform under test
    <out>/<version>/firekeep_client-<v>-py3-none-any.whl
    <out>/<version>/firekeep_symdex-<v>-py3-none-any.whl
    <out>/<version>/firekeep_docdex-<v>-py3-none-any.whl
    <out>/<version>/install.sh          <- same baked bytes, for `firekeep update`
    <out>/<version>/install.ps1
    <out>/server/latest/server.json     <- what `firekeep init` reads
    <out>/server/<tag>/firekeep-server-<tag>.tar.gz

Usage:
    python scripts/installlab/dist.py --dist-base http://dist:8000
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LAB = REPO / "scripts" / "installlab"
CACHE = LAB / ".cache"
DEFAULT_OUT = LAB / ".dist"

# Kept in step with .github/workflows/release.yml's UV_VERSION. A drift here would
# test the lab's uv rather than the release's, which is the one thing this file
# must never quietly do.
UV_VERSION = "0.8.17"

# The platforms the lab can actually exercise. Darwin is buildable but untestable
# in Docker, so it is opt-in (--targets) rather than a 60MB download every run.
DEFAULT_TARGETS = (
    "x86_64-unknown-linux-gnu",
    "x86_64-unknown-linux-musl",
    "aarch64-unknown-linux-gnu",
    "aarch64-unknown-linux-musl",
    "x86_64-pc-windows-msvc",
)

# `firekeep init` validates this against ^v\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$ and
# refuses anything else, so the lab tag has to be a real prerelease tag.
#
# It is ALSO the IMAGE_TAG the bundle's .env gets (build_server_bundle rewrites
# it), which makes the choice load-bearing rather than cosmetic:
#
#   v0.0.0-lab (default)  the bundle carries this checkout's install.sh but
#                         names images nobody published, so `install.sh --pull`
#                         stops at "cannot read ghcr.io/...:v0.0.0-lab". Right
#                         for exercising everything UP TO the pull -- the
#                         prompts, .env generation, the preflight checks --
#                         without waiting on gigabytes.
#   a published tag       e.g. --server-tag v0.4.4. The bundle still carries
#                         THIS checkout's install.sh, but pulls the real
#                         published images, so the whole install runs to a
#                         live stack. That is the actual customer path.
SERVER_TAG = "v0.0.0-lab"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    printable = " ".join(str(c) for c in cmd)
    print(f"lab: $ {printable}", flush=True)
    result = subprocess.run(cmd, **kw)
    if result.returncode != 0:
        raise SystemExit(f"lab: command failed ({result.returncode}): {printable}")
    return result


def project_version(pyproject: Path) -> str:
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"lab: no version in {pyproject}")


def build_wheels(staging: Path) -> tuple[Path, Path, Path]:
    """Build every bundled wheel from the working tree with uv.

    All three ship in a release: the client plus both dex wheels, which
    make_release.py refuses to assemble a release without.
    """
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("lab: uv is not on PATH — install it (https://astral.sh/uv)")
    for project in ("client", "symdex", "docdex"):
        run([uv, "build", "--wheel", "--out-dir", str(staging), str(REPO / project)])
    client = sorted(staging.glob("firekeep_client-*.whl"))
    symdex = sorted(staging.glob("firekeep_symdex-*.whl"))
    docdex = sorted(staging.glob("firekeep_docdex-*.whl"))
    if len(client) != 1 or len(symdex) != 1 or len(docdex) != 1:
        raise SystemExit(
            f"lab: expected exactly one wheel each, got client={[p.name for p in client]} "
            f"symdex={[p.name for p in symdex]} docdex={[p.name for p in docdex]} "
            f"— clear {staging} and retry"
        )
    return client[0], symdex[0], docdex[0]


def fetch_uv(target: str) -> Path:
    """Download and cache one uv binary, extracted exactly as release.yml does."""
    windows = target.endswith("pc-windows-msvc")
    name = f"uv-{target}.exe" if windows else f"uv-{target}"
    cached = CACHE / "uv" / UV_VERSION / name
    if cached.is_file():
        return cached
    cached.parent.mkdir(parents=True, exist_ok=True)
    suffix = "zip" if windows else "tar.gz"
    url = (
        f"https://github.com/astral-sh/uv/releases/download/"
        f"{UV_VERSION}/uv-{target}.{suffix}"
    )
    print(f"lab: fetching {url}", flush=True)
    tmp = cached.with_suffix(cached.suffix + ".download")
    with urllib.request.urlopen(url) as response, tmp.open("wb") as out:  # noqa: S310
        shutil.copyfileobj(response, out)
    if windows:
        with zipfile.ZipFile(tmp) as archive:
            member = next(n for n in archive.namelist() if n.endswith("uv.exe"))
            with archive.open(member) as src, cached.open("wb") as out:
                shutil.copyfileobj(src, out)
    else:
        with tarfile.open(tmp, "r:gz") as archive:
            member = next(m for m in archive.getmembers() if m.name.endswith("/uv"))
            src = archive.extractfile(member)
            if src is None:
                raise SystemExit(f"lab: cannot read uv from {url}")
            with src, cached.open("wb") as out:
                shutil.copyfileobj(src, out)
    tmp.unlink()
    cached.chmod(0o755)
    return cached


def build(
    out: Path,
    dist_base: str,
    targets: tuple[str, ...],
    keep_cache: bool,
    server_tag: str = SERVER_TAG,
) -> str:
    staging = out / ".staging"
    if out.exists():
        # Never rmtree the cache along with the output.
        shutil.rmtree(out)
    staging.mkdir(parents=True)

    client_wheel, symdex_wheel, docdex_wheel = build_wheels(staging)
    version = project_version(REPO / "client" / "pyproject.toml")
    expected = f"firekeep_client-{version}-py3-none-any.whl"
    if client_wheel.name != expected:
        raise SystemExit(f"lab: built {client_wheel.name}, pyproject says {expected}")

    for target in targets:
        binary = fetch_uv(target)
        shutil.copy2(binary, staging / binary.name)

    for script in ("install.sh", "install.ps1"):
        # Copy through in binary mode: install.sh must reach the container with LF
        # endings. On this Windows checkout the working-tree copy is CRLF, and a
        # CRLF `#!/bin/sh` is not a shebang — the container reports a "not found"
        # naming a path that exists. make_release._write_text_lf re-normalises when
        # it bakes the dist base, but only for the two scripts it rewrites, so do
        # not rely on that for anything else copied here.
        text = (REPO / "client" / "bootstrap" / script).read_bytes().replace(b"\r\n", b"\n")
        (staging / script).write_bytes(text)

    sys.path.insert(0, str(REPO / "client" / "scripts"))
    import make_release  # noqa: PLC0415 — path must be set first

    # Exactly the call the release workflow makes, minus signing (FIREKEEP_SIGNING_KEY
    # unset -> the bootstraps keep the placeholder and skip minisign, which is the
    # documented unsigned-release path, not a lab-only shortcut).
    make_release.main(["make_release", "--dist-base", dist_base, version, str(staging)])

    version_dir = out / version
    latest_dir = out / "latest"
    version_dir.mkdir(parents=True)
    latest_dir.mkdir(parents=True)
    for path in sorted(staging.iterdir()):
        if path.name in ("latest.json",):
            shutil.copy2(path, latest_dir / path.name)
            continue
        if path.is_file():
            shutil.copy2(path, version_dir / path.name)
    for script in ("install.sh", "install.ps1"):
        shutil.copy2(staging / script, latest_dir / script)

    # --- server bundle -------------------------------------------------------
    sys.path.insert(0, str(REPO / "deploy"))
    import build_server_bundle  # noqa: PLC0415

    server_dir = out / "server" / server_tag
    build_server_bundle.build_bundle(REPO, server_dir, server_tag)
    server_latest = out / "server" / "latest"
    server_latest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(server_dir / "server.json", server_latest / "server.json")

    if not keep_cache:
        shutil.rmtree(staging)

    print(f"\nlab: dist built at {out}")
    print(f"lab:   client   {version}")
    print(f"lab:   symdex   {symdex_wheel.name}")
    print(f"lab:   docdex   {docdex_wheel.name}")
    print(f"lab:   server   {server_tag}")
    print(f"lab:   base     {dist_base}")
    print(f"lab:   targets  {', '.join(targets)}")
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist-base",
        default="http://dist:8000",
        help="URL the published bootstrap will be baked with (default: the lab network name)",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--targets",
        default=",".join(DEFAULT_TARGETS),
        help="comma-separated uv targets to include",
    )
    parser.add_argument(
        "--keep-staging", action="store_true", help="leave .staging/ for inspection"
    )
    parser.add_argument(
        "--server-tag",
        default=SERVER_TAG,
        help=(
            "server bundle tag, which is also its IMAGE_TAG. Use a PUBLISHED tag "
            "(e.g. v0.4.4) to run the full install against real images; the default "
            f"{SERVER_TAG} stops at the image pull by design."
        ),
    )
    args = parser.parse_args(argv)
    os.chdir(REPO)
    build(
        args.out.resolve(),
        args.dist_base,
        tuple(t.strip() for t in args.targets.split(",") if t.strip()),
        args.keep_staging,
        args.server_tag,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
