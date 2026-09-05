import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import make_release  # noqa: E402

from firekeep_client import signing  # noqa: E402


@pytest.fixture(autouse=True)
def _no_ambient_signing_key(monkeypatch):
    """A signing key leaking in from the invoking environment would silently flip
    every unsigned-path assertion below. Each signing test sets its own."""
    monkeypatch.delenv(make_release.SIGNING_KEY_ENV, raising=False)


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


def _dex_wheels(tmp_path, symdex_version="0.2.13", docdex_version="0.1.0",
                maildex_version="0.1.0"):
    """The three always-on dex wheels a valid release dir carries (each guarded in
    main()). Their versions are independent of the client tag and of each other —
    0.2.13 / 0.1.0 / 0.1.0 here against a 1.2.3 release, which is the real shape."""
    symdex = tmp_path / f"firekeep_symdex-{symdex_version}-py3-none-any.whl"
    symdex.write_bytes(b"symdex")
    docdex = tmp_path / f"firekeep_docdex-{docdex_version}-py3-none-any.whl"
    docdex.write_bytes(b"docdex")
    maildex = tmp_path / f"firekeep_maildex-{maildex_version}-py3-none-any.whl"
    maildex.write_bytes(b"maildex")
    return symdex, docdex, maildex


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
    # A valid release dir now also carries the always-on dex wheels (guarded in main()).
    symdex, docdex, maildex = _dex_wheels(tmp_path)

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
    # per-wheel hash) — a line for every uv binary AND the wheel, none silently dropped.
    # The bootstrap scripts are listed too (release signing): the signature over this
    # file is what anchors the script `firekeep update` executes ---
    sums_path = tmp_path / "SHA256SUMS"
    assert sums_path.is_file()
    lines = sums_path.read_text().splitlines()
    expected_names = {p.name for p in uv_paths} | {
        wheel.name, symdex.name, docdex.name, maildex.name, sh.name, ps1.name}
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

    # Verify the digests themselves aren't just well-formed but correct. install.sh and
    # install.ps1 ARE present (since release signing) and must agree byte-for-byte with
    # latest.json's bootstrap hashes — updater.bootstrap_sha256 refuses a release where
    # the two disagree.
    by_name = {}
    for line in lines:
        hexpart, _, name = line.partition("  ")
        by_name[name] = hexpart
    for p in uv_paths:
        assert by_name[p.name] == hashlib.sha256(p.read_bytes()).hexdigest()
    assert by_name[wheel.name] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert by_name[symdex.name] == hashlib.sha256(symdex.read_bytes()).hexdigest()
    assert by_name[docdex.name] == hashlib.sha256(docdex.read_bytes()).hexdigest()
    assert by_name[maildex.name] == hashlib.sha256(maildex.read_bytes()).hexdigest()
    assert by_name[sh.name] == manifest["bootstrap_sha256"]
    assert by_name[ps1.name] == manifest["bootstrap_ps1_sha256"]


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


def test_dex_wheels_included_in_sums(tmp_path):
    """Every dex is an always-on part of the distribution; the bootstrap reads each wheel
    name from SHA256SUMS and fetches it. The existing sums glob already picks up any `.whl`,
    so all of them must be checksummed alongside the client wheel. Their versions are
    independent of the client tag (0.2.13 / 0.1.0 / 0.1.0 against a 1.2.3 release)."""
    _populate_dist_dir(tmp_path)
    symdex, docdex, maildex = _dex_wheels(tmp_path)
    make_release.main(["make_release.py", "1.2.3", str(tmp_path)])
    sums = (tmp_path / "SHA256SUMS").read_text()
    assert symdex.name in sums
    assert docdex.name in sums
    assert maildex.name in sums


def test_missing_symdex_wheel_fails_loud(tmp_path):
    """A release dir with no symdex wheel would ship a release the installer cannot complete —
    the bootstrap fetches the symdex wheel by the name it finds in SHA256SUMS. Presence +
    uniqueness is validated at build time here (NOT a match to the client `version`), so a
    missing wheel must hard-fail before any manifest is written."""
    _populate_dist_dir(tmp_path)
    _dex_wheels(tmp_path)
    next(tmp_path.glob("firekeep_symdex-*.whl")).unlink()
    with pytest.raises(SystemExit, match="firekeep_symdex"):
        make_release.main(["make_release.py", "1.2.3", str(tmp_path)])
    assert not (tmp_path / "latest.json").exists()


def test_missing_docdex_wheel_fails_loud(tmp_path):
    """Same contract for the second dex, asserted independently: with symdex PRESENT the
    build must still fail, and must name docdex. A shared 'some dex is missing' check would
    pass this while shipping a release whose bootstrap dies at the docdex fetch."""
    _populate_dist_dir(tmp_path)
    _dex_wheels(tmp_path)
    next(tmp_path.glob("firekeep_docdex-*.whl")).unlink()
    assert list(tmp_path.glob("firekeep_symdex-*.whl"))
    with pytest.raises(SystemExit, match="firekeep_docdex"):
        make_release.main(["make_release.py", "1.2.3", str(tmp_path)])
    assert not (tmp_path / "latest.json").exists()


def test_missing_maildex_wheel_fails_loud(tmp_path):
    """And the third, asserted the same independent way: with symdex and docdex PRESENT
    the build must still fail, and the message must name maildex — otherwise a release
    ships whose bootstrap dies at the maildex fetch on a stranger's machine instead."""
    _populate_dist_dir(tmp_path)
    _dex_wheels(tmp_path)
    next(tmp_path.glob("firekeep_maildex-*.whl")).unlink()
    assert list(tmp_path.glob("firekeep_symdex-*.whl"))
    assert list(tmp_path.glob("firekeep_docdex-*.whl"))
    with pytest.raises(SystemExit, match="firekeep_maildex"):
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
    _dex_wheels(tmp_path)

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
    _dex_wheels(tmp_path)

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
    _dex_wheels(tmp_path)
    with pytest.raises(SystemExit, match="placeholder"):
        make_release.main(["make_release.py", "1.2.3", str(tmp_path),
                           "--dist-base", "https://reg.example"])


def test_without_dist_base_nothing_is_baked(tmp_path):
    _populate_dist_dir(tmp_path)
    for p in (tmp_path / "install.sh", tmp_path / "install.ps1"):
        p.unlink()
    sh, ps1 = _scripts_with_placeholder(tmp_path)
    _dex_wheels(tmp_path)
    rc = make_release.main(["make_release.py", "1.2.3", str(tmp_path)])
    assert rc == 0
    assert "__FIREKEEP_DIST_BASE_DEFAULT__" in sh.read_text()


# --- release signing (docs/RELEASE-SIGNING.md) ---------------------------------


def _scripts_with_signing_placeholder(tmp_path):
    sh = tmp_path / "install.sh"
    sh.write_bytes(
        b'#!/bin/sh\nSIGNING_PUB_DEFAULT="__FIREKEEP_SIGNING_PUB_DEFAULT__"\n'
    )
    ps1 = tmp_path / "install.ps1"
    ps1.write_bytes(
        b"# ps\n$SigningPubDefault = '__FIREKEEP_SIGNING_PUB_DEFAULT__'\n"
    )
    return sh, ps1


def _signed_dist_dir(tmp_path, monkeypatch, version="1.2.3"):
    _populate_dist_dir(tmp_path, version=version)
    for p in (tmp_path / "install.sh", tmp_path / "install.ps1"):
        p.unlink()
    sh, ps1 = _scripts_with_signing_placeholder(tmp_path)
    _dex_wheels(tmp_path)
    pub_text, sec_text = signing.generate_keypair()
    monkeypatch.setenv(make_release.SIGNING_KEY_ENV, sec_text)
    return sh, ps1, pub_text, sec_text


def test_signed_release_produces_a_verifying_minisig(tmp_path, monkeypatch):
    """The exact artifact chain a signed release publishes: SHA256SUMS.minisig verifies
    over the SHA256SUMS bytes with the key derived from FIREKEEP_SIGNING_KEY, and the
    trusted comment binds the release version (the client refuses cross-version replay)."""
    _sh, _ps1, pub_text, _sec = _signed_dist_dir(tmp_path, monkeypatch)
    rc = make_release.main(["make_release.py", "1.2.3", str(tmp_path)])
    assert rc == 0
    sums = (tmp_path / "SHA256SUMS").read_bytes()
    sig_text = (tmp_path / "SHA256SUMS.minisig").read_text()
    trusted = signing.verify(sums, sig_text, pub_text)
    assert signing.trusted_comment_version(trusted) == "1.2.3"
    assert b"\r\n" not in (tmp_path / "SHA256SUMS.minisig").read_bytes()


def test_signed_release_publishes_the_public_key(tmp_path, monkeypatch):
    """signing.pub is the transparency copy CI serves at latest/signing.pub. It must be
    the SIGNING key's public half — a mismatch would advertise a key that verifies nothing."""
    _sh, _ps1, pub_text, _sec = _signed_dist_dir(tmp_path, monkeypatch)
    make_release.main(["make_release.py", "1.2.3", str(tmp_path)])
    published = (tmp_path / "signing.pub").read_text()
    assert signing.parse_public_key(published) == signing.parse_public_key(pub_text)


def test_signing_bakes_the_public_key_into_the_bootstraps_before_hashing(tmp_path, monkeypatch):
    """Like the dist base: the baked bytes are what ships, so they are what latest.json
    hashes AND what the signed SHA256SUMS lists — all three must describe the same file."""
    sh, ps1, pub_text, _sec = _signed_dist_dir(tmp_path, monkeypatch)
    make_release.main(["make_release.py", "1.2.3", str(tmp_path)])
    pub_b64 = pub_text.splitlines()[1]
    baked = sh.read_text()
    assert "__FIREKEEP_SIGNING_PUB_DEFAULT__" not in baked
    assert pub_b64 in baked
    assert pub_b64 in ps1.read_text()
    manifest = json.loads((tmp_path / "latest.json").read_text())
    assert manifest["bootstrap_sha256"] == hashlib.sha256(sh.read_bytes()).hexdigest()
    sums = (tmp_path / "SHA256SUMS").read_text()
    assert f"{manifest['bootstrap_sha256']}  install.sh" in sums


def test_unsigned_release_is_loud_but_not_fatal(tmp_path, monkeypatch, capsys):
    """Releases must keep working before the operator mints keys — but an unsigned
    build must never look like an oversight in the CI log."""
    _populate_dist_dir(tmp_path)
    _dex_wheels(tmp_path)
    rc = make_release.main(["make_release.py", "1.2.3", str(tmp_path)])
    assert rc == 0
    assert not (tmp_path / "SHA256SUMS.minisig").exists()
    assert not (tmp_path / "signing.pub").exists()
    assert "UNSIGNED" in capsys.readouterr().out


def test_unusable_signing_key_fails_the_release(tmp_path, monkeypatch):
    """A set-but-garbage secret must never fall back to shipping unsigned — that would
    turn a CI secret misconfiguration into a silent loss of the security property."""
    _populate_dist_dir(tmp_path)
    _dex_wheels(tmp_path)
    monkeypatch.setenv(make_release.SIGNING_KEY_ENV, "not a key")
    with pytest.raises(SystemExit, match="unusable"):
        make_release.main(["make_release.py", "1.2.3", str(tmp_path)])


def test_signing_requires_the_bootstrap_placeholder(tmp_path, monkeypatch):
    """Signing against bootstraps without the pub-key placeholder means the repo copies
    and make_release drifted — fail the release, never publish a script that silently
    cannot verify what it installs."""
    _populate_dist_dir(tmp_path)  # scripts WITHOUT the signing placeholder
    _dex_wheels(tmp_path)
    _pub, sec_text = signing.generate_keypair()
    monkeypatch.setenv(make_release.SIGNING_KEY_ENV, sec_text)
    with pytest.raises(SystemExit, match="placeholder"):
        make_release.main(["make_release.py", "1.2.3", str(tmp_path)])


# --- firekeep-hands is built/published but never bundled -----------------------

def test_hands_is_not_a_bundled_wheel():
    """hands is a separately-built, separately-published wheel (`firekeep hands
    enable` installs it on demand) — unlike symdex/docdex/maildex it must NEVER be
    named by make_release.py (the bundled-release manifest builder) or either
    bootstrap, which would make the installer fetch or verify it unconditionally
    for every customer."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts" / "make_release.py").read_text(encoding="utf-8")
    assert "firekeep_hands" not in src and "firekeep-hands" not in src
    for boot in ("install.sh", "install.ps1"):
        text = (Path(__file__).resolve().parents[1] / "bootstrap" / boot).read_text(encoding="utf-8")
        assert "firekeep_hands" not in text and "firekeep-hands" not in text
