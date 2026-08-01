#!/usr/bin/env python3
"""Build the release manifest + checksum file. Called by CI; unit-tested here because these
two artifacts are what every install depends on being correct."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_text_lf(path: Path, text: str) -> None:
    """Write release metadata/scripts with Unix newlines on every build host.

    ``install.sh`` and ``SHA256SUMS`` are consumed by POSIX tools that treat a
    trailing carriage return as part of the shell option or artifact name.  A
    Windows release build must therefore produce the same bytes as CI on Linux.
    """
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def build_manifest(version: str, install_sh: Path, install_ps1: Path) -> dict:
    # No base_url, no wheel: latest.json shrinks to exactly what has a consumer. The wheel's
    # integrity comes from SHA256SUMS (versioned, alongside the wheel itself), which the
    # bootstrap already knows how to parse — a `sha256` field here that *looks* verified
    # while nothing reads it is worse than no field at all; that is how C2 hid in plain sight.
    # The bootstrap hashes are what let `firekeep update` verify a script BEFORE executing it.
    # updater.fetch_manifest() rejects a manifest missing them, so an old-format release
    # fails loudly at the client rather than silently degrading to an unverified exec.
    return {
        "version": version,
        "bootstrap_sha256": _sha256(install_sh),
        "bootstrap_ps1_sha256": _sha256(install_ps1),
    }


def write_sums(paths: list[Path], dest: Path) -> Path:
    # Format is a CONTRACT with install.sh, which greps ' <name>$' and cuts field 1:
    #   "<hex><space><space><basename>"
    lines = [f"{_sha256(p)}  {p.name}" for p in paths]
    _write_text_lf(dest, "\n".join(lines) + "\n")
    return dest


DIST_BASE_PLACEHOLDER = "__FIREKEEP_DIST_BASE_DEFAULT__"


def bake_dist_base(out_dir: Path, dist_base: str) -> None:
    """Substitute the dist-base placeholder into the PUBLISHED bootstrap copies.

    Must run BEFORE build_manifest/write_sums — the baked bytes are what ships,
    so they are what gets hashed (firekeep update verifies the fetched script
    against latest.json's bootstrap hashes). The repo copies keep the
    placeholder: a raw-checkout run still demands an explicit FIREKEEP_DIST_BASE.
    """
    base = dist_base.rstrip("/")
    for name in ("install.sh", "install.ps1"):
        path = out_dir / name
        text = path.read_text(encoding="utf-8")
        if DIST_BASE_PLACEHOLDER not in text:
            raise SystemExit(f"{path} has no {DIST_BASE_PLACEHOLDER} placeholder to bake")
        _write_text_lf(path, text.replace(DIST_BASE_PLACEHOLDER, base))


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    dist_base = None
    if "--dist-base" in args:
        i = args.index("--dist-base")
        try:
            dist_base = args[i + 1]
        except IndexError:
            raise SystemExit("--dist-base requires a URL")
        del args[i:i + 2]
    version, out_dir = args[0], Path(args[1])
    wheels = list(out_dir.glob("firekeep_client-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one wheel in {out_dir}, found {len(wheels)}")
    wheel = wheels[0]
    expected = f"firekeep_client-{version}-py3-none-any.whl"
    if wheel.name != expected:
        # Fail the pipeline, loudly: a manifest that advertises 1.2.3 while shipping the
        # 1.2.2 wheel installs the wrong code, and nothing downstream would ever notice.
        raise SystemExit(
            f"version mismatch: tag says {version} (expects {expected}) "
            f"but the built wheel is {wheel.name}"
        )
    install_sh, install_ps1 = out_dir / "install.sh", out_dir / "install.ps1"
    for script in (install_sh, install_ps1):
        if not script.is_file():
            raise SystemExit(f"missing bootstrap script {script} — copy it into {out_dir} first")
    if dist_base:
        # Bake BEFORE hashing: the baked copies are the published artifacts.
        bake_dist_base(out_dir, dist_base)
    symdex_wheels = list(out_dir.glob("firekeep_symdex-*.whl"))
    if len(symdex_wheels) != 1:
        # Symdex is an always-on part of the distribution; the bootstrap reads its name
        # from SHA256SUMS and fetches it. A missing/duplicate wheel would ship a release
        # the installer cannot complete. Its version is independent of the client tag, so
        # this validates presence + uniqueness, NOT a match to `version`.
        raise SystemExit(
            f"expected exactly one firekeep_symdex-*.whl in {out_dir}, found {len(symdex_wheels)}"
        )
    manifest = build_manifest(version, install_sh, install_ps1)
    _write_text_lf(out_dir / "latest.json", json.dumps(manifest, indent=2) + "\n")
    # This SHA256SUMS is now the wheel's ONLY integrity check (latest.json carries no
    # per-wheel hash) — every bootstrap verifies the wheel against this file before install.
    sums = sorted(p for p in out_dir.iterdir() if p.name.startswith("uv-") or p.suffix == ".whl")
    write_sums(sums, out_dir / "SHA256SUMS")
    print(f"release {version}: {len(sums)} artifacts checksummed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
