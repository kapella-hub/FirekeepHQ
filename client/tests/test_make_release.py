import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import make_release  # noqa: E402


def _scripts(tmp_path):
    sh = tmp_path / "install.sh"
    sh.write_bytes(b"#!/bin/sh\n")
    ps1 = tmp_path / "install.ps1"
    ps1.write_bytes(b"# ps\n")
    return sh, ps1


def test_build_manifest_has_exactly_the_three_fields_with_a_consumer(tmp_path):
    """wheel_url and sha256 are DELETED from the manifest: install.sh reconstructs the wheel
    URL itself from a versioned BASE, and the wheel's integrity now comes from the versioned
    SHA256SUMS the bootstrap already parses — a sha256 field that looks verified while
    nothing reads it is worse than no field at all; that is how C2 hid in plain sight."""
    sh, ps1 = _scripts(tmp_path)
    m = make_release.build_manifest("1.2.3", sh, ps1)
    assert set(m.keys()) == {"version", "bootstrap_sha256", "bootstrap_ps1_sha256"}
    assert m["version"] == "1.2.3"


def test_build_manifest_publishes_the_bootstrap_hashes(tmp_path):
    """`firekeep update` verifies the bootstrap script before executing it, and
    updater.fetch_manifest() REJECTS a manifest without these — so a release that omits them
    is a release no client can update from."""
    sh, ps1 = _scripts(tmp_path)
    m = make_release.build_manifest("1.2.3", sh, ps1)
    assert m["bootstrap_sha256"] == hashlib.sha256(b"#!/bin/sh\n").hexdigest()
    assert m["bootstrap_ps1_sha256"] == hashlib.sha256(b"# ps\n").hexdigest()


def test_write_sums_format_matches_what_the_bootstrap_greps(tmp_path):
    """install.sh does `grep " uv-<target>$"` then `cut -d' ' -f1`, so the format is a
    contract: '<hex>  <basename>'. Two spaces, basename only, no directory."""
    a = tmp_path / "uv-x86_64-unknown-linux-gnu"
    a.write_bytes(b"uv")
    dest = make_release.write_sums([a], tmp_path / "SHA256SUMS")
    line = dest.read_text().strip()
    assert line == f"{hashlib.sha256(b'uv').hexdigest()}  uv-x86_64-unknown-linux-gnu"
    assert b"\r\n" not in dest.read_bytes()


def _populate_dist_dir(tmp_path, version="1.2.3", wheel_content=b"xyz",
                        uv_targets=("uv-x86_64-unknown-linux-gnu",
                                    "uv-aarch64-apple-darwin",
                                    "uv-x86_64-pc-windows-msvc.exe")):
    """Build a realistic CI output dir: one wheel, both bootstrap scripts, N uv binaries."""
    wheel = tmp_path / f"firekeep_client-{version}-py3-none-any.whl"
    wheel.write_bytes(wheel_content)
    sh, ps1 = _scripts(tmp_path)
    uv_paths = []
    for i, name in enumerate(uv_targets):
        p = tmp_path / name
        p.write_bytes(f"uv-binary-{i}".encode())
        uv_paths.append(p)
    return wheel, sh, ps1, uv_paths


def test_main_happy_path_writes_a_complete_manifest_and_sums(tmp_path):
    """This is the exact call CI makes on every release tag. If argv handling, the wheel glob,
    or the sums filter regresses, this is where it would be caught — before the artifacts are
    published and teammates' installers start fetching them."""
    wheel, sh, ps1, uv_paths = _populate_dist_dir(tmp_path)
    # A valid release dir now also carries the always-on symdex wheel (guarded in main()).
    symdex = tmp_path / "firekeep_symdex-0.2.13-py3-none-any.whl"
    symdex.write_bytes(b"symdex")

    rc = make_release.main(["make_release.py", "1.2.3", str(tmp_path)])

    assert rc == 0

    # --- latest.json: exactly the three fields with a consumer, correct values ---
    manifest_path = tmp_path / "latest.json"
    assert manifest_path.is_file()
    assert b"\r\n" not in manifest_path.read_bytes()
    manifest = json.loads(manifest_path.read_text())
    assert set(manifest.keys()) == {"version", "bootstrap_sha256", "bootstrap_ps1_sha256"}
    assert manifest["version"] == "1.2.3"
    assert manifest["bootstrap_sha256"] == hashlib.sha256(sh.read_bytes()).hexdigest()
    assert manifest["bootstrap_ps1_sha256"] == hashlib.sha256(ps1.read_bytes()).hexdigest()

    # --- SHA256SUMS: this is now the wheel's ONLY integrity check (latest.json carries no
    # per-wheel hash) — a line for every uv binary AND the wheel, none silently dropped ---
    sums_path = tmp_path / "SHA256SUMS"
    assert sums_path.is_file()
    lines = sums_path.read_text().splitlines()
    expected_names = {p.name for p in uv_paths} | {wheel.name, symdex.name}
    assert len(lines) == len(expected_names)

    # Exact line format: "<hex><two spaces><basename>", no directory component. This is a
    # hard contract — install.sh greps " uv-<target>$" and cuts field 1; install.ps1 uses
    # Select-String on the same shape. A single space or a leading path breaks every install.
    seen_names = set()
    for line in lines:
        assert "  " in line
        hexpart, _, name = line.partition("  ")
        assert len(hexpart) == 64 and all(c in "0123456789abcdef" for c in hexpart)
        assert "/" not in name and "\\" not in name
        assert name in expected_names
        seen_names.add(name)
    assert seen_names == expected_names

    # Verify the digests themselves aren't just well-formed but correct, and that install.sh
    # and install.ps1 (non-uv, non-wheel files) are NOT present in SHA256SUMS.
    by_name = {}
    for line in lines:
        hexpart, _, name = line.partition("  ")
        by_name[name] = hexpart
    for p in uv_paths:
        assert by_name[p.name] == hashlib.sha256(p.read_bytes()).hexdigest()
    assert by_name[wheel.name] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert by_name[symdex.name] == hashlib.sha256(symdex.read_bytes()).hexdigest()
    assert sh.name not in by_name
    assert ps1.name not in by_name


def test_main_fails_loudly_when_no_wheel_is_present(tmp_path):
    """A dist dir with zero wheels means the build step silently failed upstream (or produced
    the wrong artifact name). If this doesn't raise, `main()` would go on to write a manifest
    that IndexErrors instead — or worse, a stale wheel from a previous run gets picked up."""
    _scripts(tmp_path)
    with pytest.raises(SystemExit, match="found 0"):
        make_release.main(["make_release.py", "1.2.3", str(tmp_path)])
    assert not (tmp_path / "latest.json").exists()


def test_main_fails_loudly_when_more_than_one_wheel_is_present(tmp_path):
    """Two wheels in the output dir means an ambiguous release — CI must never silently pick
    one; a leftover wheel from a previous local build getting swept into the release dir
    should hard-fail, not ship whichever `glob()` happens to return first."""
    (tmp_path / "firekeep_client-1.2.3-py3-none-any.whl").write_bytes(b"a")
    (tmp_path / "firekeep_client-1.2.2-py3-none-any.whl").write_bytes(b"b")
    _scripts(tmp_path)
    with pytest.raises(SystemExit, match="found 2"):
        make_release.main(["make_release.py", "1.2.3", str(tmp_path)])
    assert not (tmp_path / "latest.json").exists()


def test_main_fails_loudly_when_install_sh_is_missing(tmp_path):
    """updater.fetch_manifest() rejects a manifest missing bootstrap_sha256, so a release built
    without install.sh in the output dir must fail here at build time — not ship a manifest
    that every client then refuses, or worse, one that silently omits the field."""
    wheel = tmp_path / "firekeep_client-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"xyz")
    (tmp_path / "install.ps1").write_bytes(b"# ps\n")
    with pytest.raises(SystemExit, match="install.sh"):
        make_release.main(["make_release.py", "1.2.3", str(tmp_path)])
    assert not (tmp_path / "latest.json").exists()


def test_main_fails_loudly_when_install_ps1_is_missing(tmp_path):
    """Same contract as install.sh, for the Windows bootstrap: without it, Windows clients'
    `firekeep update` has no bootstrap_ps1_sha256 to verify against, so the build must fail now."""
    wheel = tmp_path / "firekeep_client-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"xyz")
    (tmp_path / "install.sh").write_bytes(b"#!/bin/sh\n")
    with pytest.raises(SystemExit, match="install.ps1"):
        make_release.main(["make_release.py", "1.2.3", str(tmp_path)])
    assert not (tmp_path / "latest.json").exists()


def test_main_fails_loudly_on_a_version_tag_mismatch(tmp_path):
    """The tag and the built wheel must not drift — a release whose manifest says 1.2.3 but
    whose wheel is 1.2.2 installs the wrong code and nothing downstream would ever notice.
    build_manifest() no longer takes the wheel at all (per the new signature), so this check
    now has to live in main() itself, ahead of the manifest build."""
    wheel = tmp_path / "firekeep_client-1.2.2-py3-none-any.whl"
    wheel.write_bytes(b"xyz")
    _scripts(tmp_path)
    with pytest.raises(SystemExit, match="1.2.3"):
        make_release.main(["make_release.py", "1.2.3", str(tmp_path)])
    assert not (tmp_path / "latest.json").exists()


def test_symdex_wheel_included_in_sums(tmp_path):
    """Symdex is an always-on part of the distribution; the bootstrap reads its wheel name from
    SHA256SUMS and fetches it. The existing sums glob already picks up any `.whl`, so a present
    symdex wheel must be checksummed alongside the client wheel. Its version is independent of
    the client tag (0.2.13 here against a 1.2.3 release)."""
    _populate_dist_dir(tmp_path)
    (tmp_path / "firekeep_symdex-0.2.13-py3-none-any.whl").write_bytes(b"symdex")
    make_release.main(["make_release.py", "1.2.3", str(tmp_path)])
    sums = (tmp_path / "SHA256SUMS").read_text()
    assert "firekeep_symdex-0.2.13-py3-none-any.whl" in sums


def test_missing_symdex_wheel_fails_loud(tmp_path):
    """A release dir with no symdex wheel would ship a release the installer cannot complete —
    the bootstrap fetches the symdex wheel by the name it finds in SHA256SUMS. Presence +
    uniqueness is validated at build time here (NOT a match to the client `version`), so a
    missing wheel must hard-fail before any manifest is written."""
    _populate_dist_dir(tmp_path)
    assert not list(tmp_path.glob("firekeep_symdex-*.whl"))
    with pytest.raises(SystemExit, match="firekeep_symdex"):
        make_release.main(["make_release.py", "1.2.3", str(tmp_path)])
    assert not (tmp_path / "latest.json").exists()


# --- --dist-base baking (board 2026-07-14: zero-config one-liner) --------------

def _scripts_with_placeholder(tmp_path):
    sh = tmp_path / "install.sh"
    sh.write_bytes(b'#!/bin/sh\nDIST_BASE_DEFAULT="__FIREKEEP_DIST_BASE_DEFAULT__"\n')
    ps1 = tmp_path / "install.ps1"
    ps1.write_bytes(b"# ps\n$DistBaseDefault = '__FIREKEEP_DIST_BASE_DEFAULT__'\n")
    return sh, ps1


def test_dist_base_is_baked_before_hashing(tmp_path):
    """The published bootstrap carries its own release URL, and latest.json's
    bootstrap hashes are computed from the BAKED bytes — firekeep update verifies
    the fetched script against those hashes, so hashing the un-baked copy would
    break every update."""
    _populate_dist_dir(tmp_path)
    for p in (tmp_path / "install.sh", tmp_path / "install.ps1"):
        p.unlink()
    sh, ps1 = _scripts_with_placeholder(tmp_path)
    symdex = tmp_path / "firekeep_symdex-0.2.13-py3-none-any.whl"
    symdex.write_bytes(b"symdex")

    rc = make_release.main(["make_release.py", "1.2.3", str(tmp_path),
                            "--dist-base", "https://reg.example/firekeep-client/"])
    assert rc == 0

    baked = sh.read_text()
    assert "__FIREKEEP_DIST_BASE_DEFAULT__" not in baked
    assert 'DIST_BASE_DEFAULT="https://reg.example/firekeep-client"' in baked  # trailing / stripped
    assert "__FIREKEEP_DIST_BASE_DEFAULT__" not in ps1.read_text()

    manifest = json.loads((tmp_path / "latest.json").read_text())
    assert manifest["bootstrap_sha256"] == hashlib.sha256(sh.read_bytes()).hexdigest()
    assert manifest["bootstrap_ps1_sha256"] == hashlib.sha256(ps1.read_bytes()).hexdigest()


def test_dist_base_normalizes_bootstraps_to_lf_on_windows(tmp_path):
    """A locally assembled Windows release must still be installable by POSIX sh.

    Windows text-mode writes previously put CRLF into both the baked install.sh
    and SHA256SUMS. Debian then parsed ``set -eu\r`` as an illegal option and,
    independently, the checksum grep could not match an artifact before ``\r``.
    """
    _populate_dist_dir(tmp_path)
    for p in (tmp_path / "install.sh", tmp_path / "install.ps1"):
        p.unlink()
    sh = tmp_path / "install.sh"
    sh.write_bytes(b'#!/bin/sh\r\nDIST_BASE_DEFAULT="__FIREKEEP_DIST_BASE_DEFAULT__"\r\n')
    ps1 = tmp_path / "install.ps1"
    ps1.write_bytes(b"# ps\r\n$DistBaseDefault = '__FIREKEEP_DIST_BASE_DEFAULT__'\r\n")
    (tmp_path / "firekeep_symdex-0.2.13-py3-none-any.whl").write_bytes(b"symdex")

    make_release.main([
        "make_release.py", "1.2.3", str(tmp_path),
        "--dist-base", "https://reg.example/firekeep-client",
    ])

    for path in (sh, ps1, tmp_path / "latest.json", tmp_path / "SHA256SUMS"):
        assert b"\r\n" not in path.read_bytes(), path.name


def test_dist_base_requires_the_placeholder(tmp_path):
    """Baking against a bootstrap without the placeholder means the repo copy and
    make_release have drifted — fail the release loudly, never publish a
    bootstrap that silently ignores the intended default."""
    _populate_dist_dir(tmp_path)  # writes scripts WITHOUT the placeholder
    symdex = tmp_path / "firekeep_symdex-0.2.13-py3-none-any.whl"
    symdex.write_bytes(b"symdex")
    with pytest.raises(SystemExit, match="placeholder"):
        make_release.main(["make_release.py", "1.2.3", str(tmp_path),
                           "--dist-base", "https://reg.example"])


def test_without_dist_base_nothing_is_baked(tmp_path):
    _populate_dist_dir(tmp_path)
    for p in (tmp_path / "install.sh", tmp_path / "install.ps1"):
        p.unlink()
    sh, ps1 = _scripts_with_placeholder(tmp_path)
    symdex = tmp_path / "firekeep_symdex-0.2.13-py3-none-any.whl"
    symdex.write_bytes(b"symdex")
    rc = make_release.main(["make_release.py", "1.2.3", str(tmp_path)])
    assert rc == 0
    assert "__FIREKEEP_DIST_BASE_DEFAULT__" in sh.read_text()
