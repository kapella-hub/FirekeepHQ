import configparser
import os
import types

import pytest

from firekeep_client import cli, updater


@pytest.fixture
def update_env(tmp_path, monkeypatch):
    home = tmp_path / ".firekeep"
    home.mkdir()
    cfg = home / "config"
    cfg.write_text(
        "[active]\nprofile = personal\n"
        "[personal]\nkind = ports\nscheme = http\nhost = 10.0.0.1\n"
        "verify_tls = false\nagent_id = tester\n"
        "[dist]\nbase_url = http://gl/rel\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    # cmd_update persists the unsigned-notice marker via state (scratch); keep
    # that off the real machine cache.
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
    execs = []
    handed = []
    monkeypatch.setattr(
        cli, "_exec_bootstrap",
        lambda script, version, base, sums_file=None: (
            execs.append((script, version, base)), handed.append(sums_file)))
    # Neutral signing default: no pinned key -> silent skip. Keeps these tests
    # network-free even after the operator pins a real key in signing.py; the
    # signing-specific wiring tests below override this per-test.
    monkeypatch.setattr(cli.updater, "fetch_signed_sums",
                        lambda base, version, **kw: updater.SignedSums(None, False, None))
    return {"home": home, "execs": execs, "handed": handed}


def test_update_auto_off_writes_config_and_does_not_update(update_env, monkeypatch):
    # --auto only flips the preference; it must NOT fetch a manifest or exec the bootstrap.
    def boom(*a, **k):
        raise AssertionError("--auto must not run an update")

    monkeypatch.setattr(updater, "fetch_manifest", boom)
    rc = cli.main(["update", "--auto", "off"])
    assert rc == 0
    assert update_env["execs"] == []
    cfg = configparser.ConfigParser()
    cfg.read(update_env["home"] / "config")
    assert cfg["dist"]["auto_update"] == "false"


def test_update_auto_on_writes_config(update_env):
    cli.main(["update", "--auto", "off"])
    cli.main(["update", "--auto", "on"])
    cfg = configparser.ConfigParser()
    cfg.read(update_env["home"] / "config")
    assert cfg["dist"]["auto_update"] == "true"


def test_exec_bootstrap_passes_the_dist_base_through(monkeypatch, tmp_path):
    """install.sh fail-louds on an unset FIREKEEP_DIST_BASE, and an exec'd script inherits none
    of our config — so the handoff MUST carry it or every update dies on the first line."""
    seen = {}
    monkeypatch.setattr(cli.os, "execve",
                        lambda path, argv, env: seen.update(env) or (_ for _ in ()).throw(SystemExit(0)))
    monkeypatch.setattr(cli.os, "name", "posix")
    with pytest.raises(SystemExit):
        cli._exec_bootstrap(tmp_path / "install.sh", "1.2.3", "http://gl/rel")
    assert seen["FIREKEEP_DIST_BASE"] == "http://gl/rel"
    assert seen["FIREKEEP_VERSION"] == "1.2.3"


def _manifest(monkeypatch, version):
    monkeypatch.setattr(
        cli.updater, "fetch_manifest",
        lambda base, **kw: updater.Manifest(
            version, bootstrap_sha256="cd" * 32, bootstrap_ps1_sha256="ef" * 32,
        ),
    )


def _fake_download(seen=None):
    """Stand-in for updater.download() that behaves like the real one: it creates dest's
    parent. Every fake in this file uses it — a stub that silently skipped the mkdir would
    force production code to add a redundant one, letting the test shape the source."""
    def _download(url, dest, *, sha256, **kw):
        if seen is not None:
            seen["url"], seen["sha256"] = url, sha256
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("#!/bin/sh\n")
        return dest
    return _download


def test_update_verifies_the_bootstrap_before_executing_it(update_env, monkeypatch):
    """We are about to EXECUTE this script. Verifying uv inside install.sh while exec'ing an
    unverified install.sh would be theatre — assert the manifest's hash reaches download()."""
    _manifest(monkeypatch, "9.9.9")
    seen = {}
    monkeypatch.setattr(cli.updater, "download", _fake_download(seen))
    assert cli.main(["update"]) == 0
    expected = "ef" * 32 if os.name == "nt" else "cd" * 32
    assert seen["sha256"] == expected


def test_update_check_only_reports_and_changes_nothing(update_env, monkeypatch, capsys):
    _manifest(monkeypatch, "9.9.9")
    rc = cli.main(["update", "--check"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "9.9.9" in out and "firekeep update" in out
    assert update_env["execs"] == [], "--check must never exec the bootstrap"


def test_update_when_already_current_does_nothing(update_env, monkeypatch, capsys):
    from firekeep_client import __version__
    _manifest(monkeypatch, __version__)
    rc = cli.main(["update"])
    assert rc == 0
    assert "already up to date" in capsys.readouterr().out
    assert update_env["execs"] == []


def test_update_downloads_bootstrap_and_execs_it(update_env, monkeypatch):
    _manifest(monkeypatch, "9.9.9")
    seen = {}
    monkeypatch.setattr(cli.updater, "download", _fake_download(seen))
    rc = cli.main(["update"])
    assert rc == 0
    assert seen["url"].endswith(("/install.sh", "/install.ps1"))
    assert len(update_env["execs"]) == 1
    _script, version, base = update_env["execs"][0]
    assert version == "9.9.9"
    assert base == "http://gl/rel"


def test_update_to_pins_a_version_and_allows_rollback(update_env, monkeypatch):
    """--to is also the rollback: there is no second mechanism to keep working."""
    _manifest(monkeypatch, "9.9.9")
    # Same fake as the other tests: it creates dest's parent, exactly as the real
    # updater.download() does. A stub that skips the mkdir would push production code into
    # growing a redundant one just to satisfy the stub — the test would be shaping the
    # source, not checking it.
    monkeypatch.setattr(cli.updater, "download", _fake_download())
    rc = cli.main(["update", "--to", "0.0.1"])
    assert rc == 0
    _script, version, _base = update_env["execs"][0]
    assert version == "0.0.1", "--to must win over the manifest's latest"


# --- release-signing wiring (the check must actually run on the update path) ----


def test_update_prints_the_unsigned_warning(update_env, monkeypatch, capsys):
    """Absence under require_signed=false must be a clear one-line warning, not
    silence — a teammate should be able to see their update ran unverified."""
    _manifest(monkeypatch, "9.9.9")
    monkeypatch.setattr(cli.updater, "download", _fake_download())
    monkeypatch.setattr(
        cli.updater, "fetch_signed_sums",
        lambda base, version, **kw: updater.SignedSums(None, False,
                                                       "release 9.9.9 is not signed"),
    )
    assert cli.main(["update"]) == 0
    assert "WARNING: release 9.9.9 is not signed" in capsys.readouterr().err
    assert len(update_env["execs"]) == 1, "a warning must not block the update"


def test_update_stops_on_a_signature_failure(update_env, monkeypatch, capsys):
    """fetch_signed_sums raising (invalid signature / require_signed violation) must
    end the update before any script is downloaded, let alone executed."""
    _manifest(monkeypatch, "9.9.9")

    def _boom(base, version, **kw):
        raise updater.UpdateError("SIGNATURE VERIFICATION FAILED for release 9.9.9")

    monkeypatch.setattr(cli.updater, "fetch_signed_sums", _boom)
    monkeypatch.setattr(cli.updater, "download",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download")))
    rc = cli.main(["update"])
    assert rc == 1
    assert "SIGNATURE VERIFICATION FAILED" in capsys.readouterr().err
    assert update_env["execs"] == []


def test_update_uses_the_signature_anchored_bootstrap_hash(update_env, monkeypatch):
    """With a verified signature, the hash handed to download() must come from the
    SIGNED SHA256SUMS (via bootstrap_sha256), not the unsigned manifest alone."""
    import hashlib
    script_body = {"sh": b"#!/bin/sh\nsigned\n", "ps1": b"# ps signed\n"}
    sh_hash = hashlib.sha256(script_body["sh"]).hexdigest()
    ps1_hash = hashlib.sha256(script_body["ps1"]).hexdigest()
    monkeypatch.setattr(
        cli.updater, "fetch_manifest",
        lambda base, **kw: updater.Manifest(
            "9.9.9", bootstrap_sha256=sh_hash, bootstrap_ps1_sha256=ps1_hash,
        ),
    )
    sums = f"{sh_hash}  install.sh\n{ps1_hash}  install.ps1\n"
    monkeypatch.setattr(
        cli.updater, "fetch_signed_sums",
        lambda base, version, **kw: updater.SignedSums(sums, True, None),
    )
    seen = {}
    monkeypatch.setattr(cli.updater, "download", _fake_download(seen))
    assert cli.main(["update"]) == 0
    assert seen["sha256"] == (ps1_hash if os.name == "nt" else sh_hash)


def test_update_on_a_malformed_manifest_version_fails_loud(update_env, monkeypatch, capsys):
    """fetch_manifest only checks that `version` is a str, not that it parses — so a bad
    release (the manifest is fetched over plain HTTP, unsigned) reaches is_newer() and
    raises. A teammate must get `firekeep: ...`, never a raw traceback."""
    monkeypatch.setattr(
        cli.updater, "fetch_manifest",
        lambda base, **kw: updater.Manifest(
            "not-a-version", bootstrap_sha256="cd" * 32, bootstrap_ps1_sha256="ef" * 32,
        ),
    )
    rc = cli.main(["update"])
    assert rc == 1
    assert "unparseable version" in capsys.readouterr().err
    assert update_env["execs"] == []


def test_update_without_dist_base_is_fail_loud(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config"
    cfg.write_text(
        "[identity]\nagent_id = t\n[server]\nkind = ports\nscheme = http\n"
        "host = 127.0.0.1\nverify_tls = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    rc = cli.main(["update"])
    assert rc == 1
    assert "no [dist] base_url" in capsys.readouterr().err


def test_update_unreachable_manifest_is_fail_loud(update_env, monkeypatch, capsys):
    def boom(base, **kw):
        raise updater.UpdateError("cannot reach the release manifest at http://gl/rel/latest.json")

    monkeypatch.setattr(cli.updater, "fetch_manifest", boom)
    rc = cli.main(["update"])
    assert rc == 1
    assert "cannot reach" in capsys.readouterr().err


# --- the verified SHA256SUMS is threaded THROUGH to the bootstrap (HIGH) --------
#
# The client verifies <version>/SHA256SUMS.minisig and used to throw the verified
# bytes away; the bootstrap then re-fetched its own copy, and a host serving
# different bytes to the two fetches installed attacker artifacts with exit 0.
# These tests pin the client half of the fix: verified -> a 0600 file handed via
# _exec_bootstrap; unverified -> nothing handed (and nothing stale inherited).


def _signed_release(version="9.9.9"):
    import hashlib
    sh, ps1 = b"#!/bin/sh\nsigned\n", b"# ps signed\n"
    sums = (
        f"{hashlib.sha256(sh).hexdigest()}  install.sh\n"
        f"{hashlib.sha256(ps1).hexdigest()}  install.ps1\n"
        f"{'ab' * 32}  firekeep_client-{version}-py3-none-any.whl\n"
    )
    manifest = updater.Manifest(
        version,
        bootstrap_sha256=hashlib.sha256(sh).hexdigest(),
        bootstrap_ps1_sha256=hashlib.sha256(ps1).hexdigest(),
    )
    return manifest, sums


def test_update_hands_the_verified_sums_file_to_the_bootstrap(update_env, monkeypatch):
    manifest, sums = _signed_release()
    monkeypatch.setattr(cli.updater, "fetch_manifest", lambda base, **kw: manifest)
    monkeypatch.setattr(cli.updater, "fetch_signed_sums",
                        lambda base, version, **kw: updater.SignedSums(sums, True, None))
    monkeypatch.setattr(cli.updater, "download", _fake_download())
    assert cli.main(["update"]) == 0
    assert update_env["handed"] == [update_env["home"] / "bootstrap" / "SHA256SUMS.verified"]
    handed = update_env["handed"][0]
    assert handed.read_text(encoding="utf-8") == sums, (
        "the bootstrap must verify against EXACTLY the bytes the client verified"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_the_handed_sums_file_is_private(update_env, monkeypatch):
    import stat
    manifest, sums = _signed_release()
    monkeypatch.setattr(cli.updater, "fetch_manifest", lambda base, **kw: manifest)
    monkeypatch.setattr(cli.updater, "fetch_signed_sums",
                        lambda base, version, **kw: updater.SignedSums(sums, True, None))
    monkeypatch.setattr(cli.updater, "download", _fake_download())
    assert cli.main(["update"]) == 0
    mode = stat.S_IMODE(update_env["handed"][0].stat().st_mode)
    assert mode == 0o600, f"handed sums must be 0600, got {oct(mode)}"


def test_the_handed_sums_mode_is_applied_at_creation_never_chmod_after(update_env, monkeypatch):
    """SHAPE, not just outcome — the outcome test above survives the mutant.

    Re-review found it: flipping _write_verified_sums' os.open mode 0o600 -> 0o644
    leaves the mode assertion GREEN, because state._private()'s chmod-after
    silently rescues it. That is exactly the write-then-chmod permissive window
    this codebase just removed from generate_signing_key.py — a co-resident user
    can read the file between the two calls. Assert the O_EXCL + explicit mode at
    open, the way test_keygen_applies_the_mode_at_open_never_chmod_after does."""
    import inspect
    src = inspect.getsource(cli._write_verified_sums)
    assert "O_EXCL" in src, "must refuse a pre-planted file/symlink race-free"
    assert "0o600" in src, "the final mode must be applied AT open, not after"
    open_at = src.index("os.open")
    assert ".chmod(0o600)" not in src[open_at:], (
        "a chmod after os.open re-opens the permissive window the mode argument closes"
    )


def test_the_handed_sums_parent_directory_is_not_world_writable(update_env, monkeypatch):
    """A 0600 file under a 0777 parent is still substitutable: an unprivileged
    co-resident can unlink and replace it between our write and the bootstrap's
    read. mkdir applies the umask, so the mode must be passed and re-asserted."""
    import inspect
    import os as _os
    import stat as _stat

    import pytest as _pytest
    src = inspect.getsource(cli._write_verified_sums)
    assert "mode=0o700" in src, "mkdir must not inherit a permissive umask"
    if _os.name == "nt":
        _pytest.skip("POSIX mode bits are meaningless on Windows (dirs report 0777)")
    manifest, sums = _signed_release()
    monkeypatch.setattr(cli.updater, "fetch_manifest", lambda base, **kw: manifest)
    monkeypatch.setattr(cli.updater, "fetch_signed_sums",
                        lambda base, version, **kw: updater.SignedSums(sums, True, None))
    monkeypatch.setattr(cli.updater, "download", _fake_download())
    assert cli.main(["update"]) == 0
    parent_mode = _stat.S_IMODE(update_env["handed"][0].parent.stat().st_mode)
    assert not (parent_mode & 0o022), f"parent must not be group/other-writable, got {oct(parent_mode)}"


def test_update_hands_nothing_when_the_signature_did_not_verify(update_env, monkeypatch):
    """Unverified sums must NOT be handed through: the hand-off asserts 'these bytes
    were verified against the pinned key', and handing an unverified fetch would
    launder it into that claim."""
    _manifest(monkeypatch, "9.9.9")
    monkeypatch.setattr(
        cli.updater, "fetch_signed_sums",
        lambda base, version, **kw: updater.SignedSums("00" * 32 + "  x.whl\n", False,
                                                       "release 9.9.9 is not signed"),
    )
    monkeypatch.setattr(cli.updater, "download", _fake_download())
    assert cli.main(["update"]) == 0
    assert update_env["handed"] == [None]


def test_exec_bootstrap_sets_and_clears_the_sums_env(monkeypatch, tmp_path):
    """With a verified file: FIREKEEP_SUMS_FILE points at it. Without: any inherited
    FIREKEEP_SUMS_FILE is DROPPED — a stale file from a previous update (or a
    caller's env) must never masquerade as this update's verified sums."""
    seen = {}

    def _capture(path, argv, env):
        seen.clear()
        seen.update(env)
        raise SystemExit(0)

    monkeypatch.setattr(cli.os, "execve", _capture)
    monkeypatch.setattr(cli.os, "name", "posix")
    monkeypatch.setenv("FIREKEEP_SUMS_FILE", "/stale/from/last/time")

    sums = tmp_path / "SHA256SUMS.verified"
    sums.write_text("aa\n")
    with pytest.raises(SystemExit):
        cli._exec_bootstrap(tmp_path / "install.sh", "1.2.3", "http://gl/rel", sums_file=sums)
    assert seen["FIREKEEP_SUMS_FILE"] == str(sums)

    with pytest.raises(SystemExit):
        cli._exec_bootstrap(tmp_path / "install.sh", "1.2.3", "http://gl/rel")
    assert "FIREKEEP_SUMS_FILE" not in seen


def test_exec_bootstrap_exports_the_pinned_signing_pub(monkeypatch, tmp_path):
    """LOW finding: the bootstrap's own minisign check otherwise trusts only the
    HOST-baked key — circular on the update path. The client must export ITS pinned
    key (the bare base64 line, what minisign -P takes) as FIREKEEP_SIGNING_PUB."""
    from firekeep_client import signing
    seen = {}
    monkeypatch.setattr(cli.os, "execve",
                        lambda path, argv, env: seen.update(env) or (_ for _ in ()).throw(SystemExit(0)))
    monkeypatch.setattr(cli.os, "name", "posix")
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEY",
                        "untrusted comment: minisign public key ABC\nRWTbase64line\n")
    with pytest.raises(SystemExit):
        cli._exec_bootstrap(tmp_path / "install.sh", "1.2.3", "http://gl/rel")
    assert seen["FIREKEEP_SIGNING_PUB"] == "RWTbase64line"

    # No pinned key -> nothing exported (a pre-mint build must not invent one).
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEY", "")
    monkeypatch.delenv("FIREKEEP_SIGNING_PUB", raising=False)
    seen.clear()
    with pytest.raises(SystemExit):
        cli._exec_bootstrap(tmp_path / "install.sh", "1.2.3", "http://gl/rel")
    assert "FIREKEEP_SIGNING_PUB" not in seen


@pytest.mark.parametrize("name", ["PSMODULEPATH", "PSModulePath"])
def test_exec_bootstrap_drops_psmodulepath_on_windows(monkeypatch, tmp_path, name):
    """MEASURED on Windows 11 + pwsh 7.6.4: `firekeep update` died with
    "Get-FileHash is not recognized" inside the bootstrap's Verify-AgainstSums.

    `powershell` is Windows PowerShell 5.1. Handed a PSModulePath built by pwsh 7,
    it autoloads Microsoft.PowerShell.Utility 7.0.0.0 ahead of its own 3.1.0.0, and
    that module binds `Select-String` under 5.1 but NOT `Get-FileHash` -- so the
    failure lands precisely on the checksum gate for a binary about to be executed,
    and reads like a broken Windows install.

    Both spellings are asserted because `dict(os.environ)` UPPERCASES keys on
    Windows and leaves them verbatim elsewhere; a case-sensitive pop passes on
    Linux CI and does nothing on the machine that actually has the bug.
    """
    seen = {}
    monkeypatch.setattr(cli.os, "name", "nt")
    # _exec_bootstrap is a FOREGROUND child on Windows now — it calls
    # proc.wait() and sys.exit()s with the child's code, so the stub must
    # return a process-shaped object, not None.
    monkeypatch.setattr(
        cli.subprocess, "Popen",
        lambda argv, env=None, **kw: seen.update(env or {})
        or types.SimpleNamespace(wait=lambda: 0),
    )
    monkeypatch.setenv(name, r"C:\pwsh7\Modules;C:\WINDOWS\system32\WindowsPowerShell\v1.0\Modules")
    monkeypatch.setenv("FIREKEEP_KEEP_ME", "1")

    with pytest.raises(SystemExit) as excinfo:
        cli._exec_bootstrap(tmp_path / "install.ps1", "1.2.3", "http://gl/rel")
    assert excinfo.value.code == 0

    assert not [k for k in seen if k.upper() == "PSMODULEPATH"], (
        "PSModulePath must be dropped so PowerShell 5.1 rebuilds its own default"
    )
    # Only that one variable goes; the rest of the environment must survive intact.
    assert seen["FIREKEEP_KEEP_ME"] == "1"
    assert seen["FIREKEEP_DIST_BASE"] == "http://gl/rel"


def test_exec_bootstrap_windows_waits_and_propagates_the_exit_code(monkeypatch, tmp_path):
    """The Windows hand-off is a FOREGROUND child: wait for it, exit with ITS code.

    The previous design rebuilt ~/.firekeep/venv in place, which forced a
    DETACHED spawn + immediate parent exit to release the firekeep.exe lock —
    and the detached installer's output then tore across the caller's returned
    prompt (the measured 0.1.34 console mess `firekeep update` printed on the
    owner's machine). The side-by-side layout is what makes waiting safe: the
    bootstrap provisions venvs/<V> BESIDE this process's venv and flips the
    `current` junction, so nothing the parent holds (its own exe included) is
    ever overwritten, and there is no lock to get out of the way of.

    Two invariants, either's regression re-tears the console:
      1. the parent WAITS (proc.wait() called exactly once), and
      2. the child's exit code propagates as the parent's (a swallowed nonzero
         would report a failed update as success to shells and schedulers).
    """
    class _Proc:
        def __init__(self):
            self.waits = 0

        def wait(self):
            self.waits += 1
            return 7

    proc = _Proc()
    captured = {}

    def _popen(argv, env=None, **kw):
        captured["argv"] = list(argv)
        captured["kw"] = kw
        return proc

    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(cli.subprocess, "Popen", _popen)

    with pytest.raises(SystemExit) as excinfo:
        cli._exec_bootstrap(tmp_path / "install.ps1", "1.2.3", "http://gl/rel")

    assert proc.waits == 1, "the parent must wait for the bootstrap child"
    assert excinfo.value.code == 7, "the child's exit code must propagate verbatim"
    # Foreground SHAPE, not just outcome: the detached spawn passed detach
    # creationflags and redirected/closed handles; a foreground child streams
    # its output in order to the SAME console by inheriting stdio, so none of
    # those knobs may be passed.
    for detached_only in ("creationflags", "close_fds", "stdout", "stderr", "stdin"):
        assert detached_only not in captured["kw"], (
            f"{detached_only}= is detached-spawn residue; the foreground child "
            f"must inherit the caller's console untouched"
        )


def test_exec_bootstrap_keeps_psmodulepath_on_posix(monkeypatch, tmp_path):
    """The clash is Windows-PowerShell-specific. /bin/sh neither reads nor is
    confused by PSModulePath, so the posix path stays a pure env passthrough."""
    seen = {}
    monkeypatch.setattr(cli.os, "name", "posix")
    monkeypatch.setattr(cli.os, "execve",
                        lambda path, argv, env: seen.update(env) or (_ for _ in ()).throw(SystemExit(0)))
    monkeypatch.setenv("PSModulePath", "/whatever")

    with pytest.raises(SystemExit):
        cli._exec_bootstrap(tmp_path / "install.sh", "1.2.3", "http://gl/rel")

    # Case-insensitive lookup: this file also runs ON Windows, where os.environ
    # uppercases the name we just set. Asserting seen["PSModulePath"] would fail
    # there for the very reason the fix exists.
    assert [seen[k] for k in seen if k.upper() == "PSMODULEPATH"] == ["/whatever"]


# --- `--to` verifies the TARGET version's signature (MEDIUM) --------------------


def test_update_to_verifies_the_target_and_the_script_versions(update_env, monkeypatch):
    """--to 0.0.1 must verify 0.0.1's sums (what the bootstrap installs) — verifying
    only latest's left every rollback unsigned. Latest's sums are ALSO fetched,
    because the executed bootstrap script is latest/'s and only latest's sums list
    its bytes."""
    _manifest(monkeypatch, "9.9.9")
    versions = []

    def _fss(base, version, **kw):
        versions.append(version)
        return updater.SignedSums(None, False, None)

    monkeypatch.setattr(cli.updater, "fetch_signed_sums", _fss)
    monkeypatch.setattr(cli.updater, "download", _fake_download())
    assert cli.main(["update", "--to", "0.0.1"]) == 0
    assert versions == ["0.0.1", "9.9.9"]
    assert update_env["execs"][0][1] == "0.0.1"


def test_update_without_to_verifies_latest_exactly_once(update_env, monkeypatch):
    _manifest(monkeypatch, "9.9.9")
    versions = []

    def _fss(base, version, **kw):
        versions.append(version)
        return updater.SignedSums(None, False, None)

    monkeypatch.setattr(cli.updater, "fetch_signed_sums", _fss)
    monkeypatch.setattr(cli.updater, "download", _fake_download())
    assert cli.main(["update"]) == 0
    assert versions == ["9.9.9"], "the common path must not grow a second fetch"


def test_update_to_an_unsigned_target_fails_under_require_signed(update_env, monkeypatch, capsys):
    """Rollback enforcement: an unsigned OLD target under require_signed must fail
    with a message naming the flag — before anything is downloaded or executed."""
    cfg_path = update_env["home"] / "config"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8")
                        + "require_signed = true\n", encoding="utf-8")
    _manifest(monkeypatch, "9.9.9")

    def _fss(base, version, *, require_signed, **kw):
        assert require_signed is True, "cmd_update must thread the config flag through"
        raise updater.UpdateError(
            f"release {version} is not signed (no SHA256SUMS.minisig) and "
            f"[dist] require_signed = true — refusing to update"
        )

    monkeypatch.setattr(cli.updater, "fetch_signed_sums", _fss)
    monkeypatch.setattr(cli.updater, "download",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download")))
    rc = cli.main(["update", "--to", "0.0.1"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "release 0.0.1 is not signed" in err
    assert "require_signed" in err
    assert update_env["execs"] == []


def test_update_to_with_a_verified_target_hands_the_targets_sums(update_env, monkeypatch):
    """The handed file must be the TARGET's sums — they are what the bootstrap
    verifies FIREKEEP_VERSION=target's artifacts against; latest's sums would fail
    every checksum on a rollback."""
    manifest, latest_sums = _signed_release("9.9.9")
    target_sums = "cc" * 32 + "  firekeep_client-0.0.1-py3-none-any.whl\n"
    monkeypatch.setattr(cli.updater, "fetch_manifest", lambda base, **kw: manifest)
    monkeypatch.setattr(
        cli.updater, "fetch_signed_sums",
        lambda base, version, **kw: updater.SignedSums(
            target_sums if version == "0.0.1" else latest_sums, True, None),
    )
    monkeypatch.setattr(cli.updater, "download", _fake_download())
    assert cli.main(["update", "--to", "0.0.1"]) == 0
    assert update_env["handed"][0].read_text(encoding="utf-8") == target_sums


# --- the unsigned warning must survive a detached update (MEDIUM) ---------------


def test_update_persists_the_unsigned_notice_for_the_next_session(update_env, monkeypatch):
    """The background auto-update runs detached with stderr on DEVNULL, so the
    one-line unsigned warning reaches nobody. cmd_update must persist a marker the
    next session_start briefing prints (and consumes)."""
    from firekeep_client import state
    _manifest(monkeypatch, "9.9.9")
    monkeypatch.setattr(
        cli.updater, "fetch_signed_sums",
        lambda base, version, **kw: updater.SignedSums(None, False,
                                                       "release 9.9.9 is not signed"),
    )
    monkeypatch.setattr(cli.updater, "download", _fake_download())
    assert cli.main(["update"]) == 0
    notice = state.consume_unsigned_update_notice()
    assert notice is not None
    assert "9.9.9" in notice
    assert "WITHOUT a verified release signature" in notice
    assert "require_signed" in notice
    # one-shot: consumed above, gone now
    assert state.consume_unsigned_update_notice() is None


def test_update_writes_no_unsigned_notice_when_verified(update_env, monkeypatch):
    from firekeep_client import state
    manifest, sums = _signed_release()
    monkeypatch.setattr(cli.updater, "fetch_manifest", lambda base, **kw: manifest)
    monkeypatch.setattr(cli.updater, "fetch_signed_sums",
                        lambda base, version, **kw: updater.SignedSums(sums, True, None))
    monkeypatch.setattr(cli.updater, "download", _fake_download())
    assert cli.main(["update"]) == 0
    assert state.consume_unsigned_update_notice() is None
