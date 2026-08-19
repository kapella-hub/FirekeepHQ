#!/usr/bin/env python3
"""Build the release manifest + checksum file. Called by CI; unit-tested here because these
two artifacts are what every install depends on being correct."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
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
SIGNING_PUB_PLACEHOLDER = "__FIREKEEP_SIGNING_PUB_DEFAULT__"
#: CI provides the UNENCRYPTED minisign secret key (file content) through this env var.
#: Absent -> the release is built UNSIGNED, loudly (see main()) — releases must not
#: break before the operator mints keys (docs/RELEASE-SIGNING.md).
SIGNING_KEY_ENV = "FIREKEEP_SIGNING_KEY"


def _load_signing():
    """Import firekeep_client.signing from the checkout this script lives in. The module
    is stdlib-only, so this works in a bare CI runner with no installs."""
    client_dir = str(Path(__file__).resolve().parents[1])
    if client_dir not in sys.path:
        sys.path.insert(0, client_dir)
    from firekeep_client import signing
    return signing


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


def bake_signing_pub(out_dir: Path, pub_b64: str) -> None:
    """Substitute the signing-public-key placeholder into the PUBLISHED bootstrap copies.

    Same discipline as bake_dist_base: runs BEFORE build_manifest/write_sums so the
    baked bytes are what gets hashed AND signed. Only runs when a signing key is
    configured — an unsigned build keeps the placeholder, which the bootstraps compare
    against a split-string reconstruction and treat as "no key" (so an unbaked script
    degrades to the pre-signing behaviour instead of failing).
    `pub_b64` is the bare base64 line: pure [A-Za-z0-9+/=], safe inside both the sh
    double-quoted and the PowerShell single-quoted assignment it lands in."""
    for name in ("install.sh", "install.ps1"):
        path = out_dir / name
        text = path.read_text(encoding="utf-8")
        if SIGNING_PUB_PLACEHOLDER not in text:
            raise SystemExit(f"{path} has no {SIGNING_PUB_PLACEHOLDER} placeholder to bake")
        _write_text_lf(path, text.replace(SIGNING_PUB_PLACEHOLDER, pub_b64))


def sign_release(out_dir: Path, version: str, secret_key_text: str) -> str:
    """Sign SHA256SUMS (minisign detached signature) and publish the public key.

    Writes `SHA256SUMS.minisig` next to the sums and `signing.pub` (the transparency
    copy CI publishes at latest/signing.pub). The trusted comment carries
    `version:<X.Y.Z>`, which the client cross-checks — a valid signature for release A
    served under release B's directory is refused (replay across versions).
    Returns the public key base64 line for baking/logging."""
    signing = _load_signing()
    key = signing.parse_secret_key(secret_key_text)
    pub_text = signing.public_key_text(key.key_id, key.public_key)
    pub_b64 = pub_text.splitlines()[1]
    pinned = signing.PINNED_PUBLIC_KEY.strip()
    if pinned:
        try:
            pinned_b64 = pinned.splitlines()[-1].strip()
        except IndexError:
            pinned_b64 = ""
        if pinned_b64 and pinned_b64 != pub_b64:
            # NOT fatal: during rotation, exactly one release is legitimately signed
            # with the OLD key while pinning the NEW one (docs/RELEASE-SIGNING.md).
            # Outside a rotation this means the CI secret and the repo pin drifted.
            print(
                f"make_release: NOTICE — signing key {signing.key_id_hex(key.key_id)} does "
                f"not match the repo's PINNED_PUBLIC_KEY. Correct during a key rotation; "
                f"otherwise the CI secret and firekeep_client/signing.py have drifted.",
                file=sys.stderr,
            )
    data = (out_dir / "SHA256SUMS").read_bytes()
    trusted = f"timestamp:{int(time.time())}\tfile:SHA256SUMS\tversion:{version}\thashed"
    sig_text = signing.sign(
        data, secret_key_text,
        trusted_comment=trusted,
        untrusted_comment=f"verify with minisign or firekeep_client.signing (key {signing.key_id_hex(key.key_id)})",
    )
    _write_text_lf(out_dir / "SHA256SUMS.minisig", sig_text)
    _write_text_lf(out_dir / "signing.pub", pub_text)
    return pub_b64


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
    signing_key = os.environ.get(SIGNING_KEY_ENV, "").strip()
    pub_b64 = ""
    if signing_key:
        # Derive the public key from the CI secret and bake it into the bootstraps —
        # like the dist base, BEFORE hashing: the baked bytes are what ships, what the
        # manifest hashes, and what the signature covers.
        signing = _load_signing()
        try:
            key = signing.parse_secret_key(signing_key)
        except signing.SignatureError as exc:
            raise SystemExit(f"{SIGNING_KEY_ENV} is set but unusable: {exc}")
        pub_b64 = signing.public_key_text(key.key_id, key.public_key).splitlines()[1]
        bake_signing_pub(out_dir, pub_b64)
    # Every dex wheel is an always-on part of the distribution; the bootstrap reads each
    # name from SHA256SUMS and fetches it, dying "release is incomplete" when one is
    # absent. A missing/duplicate wheel here would ship a release the installer cannot
    # complete. Dex versions are independent of the client tag AND of each other, so this
    # validates presence + uniqueness, NOT a match to `version`. Checked one dex at a
    # time so the failure names the wheel that is actually wrong.
    for dex in ("firekeep_symdex", "firekeep_docdex", "firekeep_maildex"):
        dex_wheels = list(out_dir.glob(f"{dex}-*.whl"))
        if len(dex_wheels) != 1:
            raise SystemExit(
                f"expected exactly one {dex}-*.whl in {out_dir}, found {len(dex_wheels)}"
            )
    manifest = build_manifest(version, install_sh, install_ps1)
    _write_text_lf(out_dir / "latest.json", json.dumps(manifest, indent=2) + "\n")
    # This SHA256SUMS is now the wheel's ONLY integrity check (latest.json carries no
    # per-wheel hash) — every bootstrap verifies the wheel against this file before install.
    # The bootstrap scripts are ALSO listed (since release signing): the signature over
    # this file is what anchors the script `firekeep update` executes — latest.json's
    # bootstrap hashes are unsigned, so the client cross-checks them against the signed
    # entries here (updater.bootstrap_sha256). The bootstraps never fetch these entries
    # themselves; they land in the sums purely to be covered by the signature.
    sums = sorted(
        p for p in out_dir.iterdir()
        if p.name.startswith("uv-") or p.suffix == ".whl" or p.name in ("install.sh", "install.ps1")
    )
    write_sums(sums, out_dir / "SHA256SUMS")
    if signing_key:
        sign_release(out_dir, version, signing_key)
        print(f"release {version}: {len(sums)} artifacts checksummed, SHA256SUMS signed "
              f"(pub {pub_b64})")
    else:
        # Loud, not fatal: releases must keep working before the operator mints keys —
        # but an unsigned release should never look like an oversight in the CI log.
        print(f"release {version}: {len(sums)} artifacts checksummed — UNSIGNED "
              f"({SIGNING_KEY_ENV} is not set; see docs/RELEASE-SIGNING.md)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
