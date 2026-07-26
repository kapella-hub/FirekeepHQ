import configparser
import os
import textwrap
import types
from datetime import datetime, timedelta, timezone

import pytest
from firekeep_client import cli, resolver, transport, __version__

PERSONAL = textwrap.dedent("""\
    [active]
    profile = personal
    [personal]
    kind = ports
    scheme = http
    host = 10.0.0.5
    verify_tls = false
    agent_id = tester
""")

PERSONAL_CHANGEME = textwrap.dedent("""\
    [active]
    profile = personal
    [personal]
    kind = ports
    scheme = http
    host = 10.0.0.5
    verify_tls = false
    agent_id = CHANGEME
""")


def _cfg(tmp_path, monkeypatch, text):
    cfg = tmp_path / ".firekeep" / "config"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text, encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    # Profile agent_id is authoritative unless a test opts into the env
    # override (matches the convention in tests/conftest.py::firekeep_env) --
    # otherwise a real FIREKEEP_AGENT_ID in the ambient shell leaks in here.
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    return resolver.load_config(cfg)


def test_check_health_all_ok(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, PERSONAL)
    monkeypatch.setattr(cli, "get_json", lambda url, **kw: {"status": "ok"})
    results = cli._check_health(cfg)
    assert {svc for svc, _, _ in results} == set(resolver.SERVICES)
    assert all(status == "ok" for _, status, _ in results)


def test_check_health_reports_fail_on_transport_error(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, PERSONAL)

    def boom(url, **kw):
        raise transport.TransportError("refused", status=None)

    monkeypatch.setattr(cli, "get_json", boom)
    results = cli._check_health(cfg)
    assert all(status == "fail" for _, status, _ in results)


def test_check_skew_ok_when_versions_match(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, PERSONAL)
    monkeypatch.setattr(cli, "get_json", lambda url, **kw: {"version": __version__})
    name, status, _ = cli._check_skew(cfg)
    assert name == "version-skew" and status == "ok"


def test_check_skew_warns_on_mismatch(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, PERSONAL)
    monkeypatch.setattr(cli, "get_json", lambda url, **kw: {"version": "9.9.9"})
    _, status, detail = cli._check_skew(cfg)
    assert status == "warn" and "9.9.9" in detail


def test_check_skew_warns_when_unreachable(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, PERSONAL)

    def boom(url, **kw):
        raise transport.TransportError("down")

    monkeypatch.setattr(cli, "get_json", boom)
    _, status, _ = cli._check_skew(cfg)
    assert status == "warn"  # skew unknown, not a hard fail


def test_check_health_survives_ssl_errors_not_wrapped_by_transport(tmp_path, monkeypatch):
    # transport._build_ssl_context() runs OUTSIDE transport._request()'s own
    # try/except, so a malformed/unverifiable ca_path raises a raw
    # ssl.SSLError (an OSError subclass), not TransportError. A doctor check
    # must fail loud on ITS OWN row, never crash the whole preflight before
    # later checks (skew, agent-id, venv, perms, ca-expiry) get to run.
    import ssl as _ssl
    cfg = _cfg(tmp_path, monkeypatch, PERSONAL)

    def boom(url, **kw):
        raise _ssl.SSLError("no certificate or crl found")

    monkeypatch.setattr(cli, "get_json", boom)
    results = cli._check_health(cfg)
    assert {svc for svc, _, _ in results} == set(resolver.SERVICES)
    assert all(status == "fail" for _, status, _ in results)


def test_check_skew_survives_ssl_errors_not_wrapped_by_transport(tmp_path, monkeypatch):
    import ssl as _ssl
    cfg = _cfg(tmp_path, monkeypatch, PERSONAL)

    def boom(url, **kw):
        raise _ssl.SSLError("no certificate or crl found")

    monkeypatch.setattr(cli, "get_json", boom)
    _, status, _ = cli._check_skew(cfg)
    assert status == "warn"


def test_check_venv_scripts_present_and_missing(tmp_path):
    venv = tmp_path / "venv"
    for is_win in (False, True):
        binname = "Scripts" if is_win else "bin"
        ext = ".exe" if is_win else ""
        bindir = venv / binname
        bindir.mkdir(parents=True, exist_ok=True)
        for n in ("firekeep", "firekeep-shim", "firekeep-sidecar"):
            (bindir / f"{n}{ext}").write_text("x", encoding="utf-8")
        assert cli._check_venv_scripts(venv, is_windows=is_win)[1] == "ok"
        (bindir / f"firekeep-shim{ext}").unlink()
        assert cli._check_venv_scripts(venv, is_windows=is_win)[1] == "fail"


@pytest.mark.skipif(os.name == "nt", reason="posix mode bits")
def test_check_config_perms_posix(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("x", encoding="utf-8")
    os.chmod(cfg, 0o600)
    assert cli._check_config_perms(cfg, is_windows=False)[1] == "ok"
    os.chmod(cfg, 0o644)
    assert cli._check_config_perms(cfg, is_windows=False)[1] == "warn"


def test_check_config_perms_windows(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(
            stdout="config NT AUTHORITY\\SYSTEM:(F)\n       Everyone:(R)\n", returncode=0),
    )
    assert cli._check_config_perms(cfg, is_windows=True)[1] == "warn"
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(
            stdout="config DESKTOP\\tester:(F)\n", returncode=0),
    )
    assert cli._check_config_perms(cfg, is_windows=True)[1] == "ok"


def _office(tmp_path, monkeypatch, ca_path):
    text = textwrap.dedent(f"""\
        [active]
        profile = office
        [office]
        kind = paths
        scheme = https
        base_url = https://firekeep.office.example
        verify_tls = true
        ca_path = {ca_path.as_posix()}
        api_key = nxs_k
        agent_id = tester
    """)
    return _cfg(tmp_path, monkeypatch, text)


def test_check_ca_expiry_ok_warn_fail(tmp_path, monkeypatch):
    ca = tmp_path / "ca.crt"
    ca.write_text("dummy", encoding="utf-8")
    cfg = _office(tmp_path, monkeypatch, ca)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(cli, "_cert_not_after", lambda p: now + timedelta(days=400))
    assert cli._check_ca_expiry(cfg)[1] == "ok"
    monkeypatch.setattr(cli, "_cert_not_after", lambda p: now + timedelta(days=10))
    assert cli._check_ca_expiry(cfg)[1] == "warn"
    monkeypatch.setattr(cli, "_cert_not_after", lambda p: now - timedelta(days=5))
    assert cli._check_ca_expiry(cfg)[1] == "fail"


def test_check_ca_expiry_skipped_on_personal(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, PERSONAL)
    assert cli._check_ca_expiry(cfg) is None


def test_cmd_doctor_exit_code(tmp_path, monkeypatch, capsys):
    _cfg(tmp_path, monkeypatch, PERSONAL)
    monkeypatch.setattr(cli, "run_doctor", lambda cfg=None: [("x", "fail", "boom")])
    assert cli.cmd_doctor(types.SimpleNamespace()) == 1
    monkeypatch.setattr(cli, "run_doctor", lambda cfg=None: [("x", "ok", "fine")])
    assert cli.cmd_doctor(types.SimpleNamespace()) == 0


# --- Controller additions (folded into Task 27, not in the original brief) --
#
# 1. agent_id == "CHANGEME" (the installer skeleton default) must WARN loudly:
#    attribution silently lands as "CHANGEME" otherwise.
# 2. Partial-venv detection: a venv dir that exists but whose python
#    interpreter never landed (creation was interrupted) is a DIFFERENT
#    failure mode than "no venv at all" — a rerun of `firekeep install` will
#    NOT repair it (venv-creation is skipped once the bin dir exists), so
#    doctor must name it distinctly so the user knows to delete the venv
#    dir and reinstall rather than just re-running install.

def test_check_agent_id_ok_when_set(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, PERSONAL)
    name, status, detail = cli._check_agent_id(cfg)
    assert name == "agent-id" and status == "ok" and "tester" in detail


def test_check_agent_id_warns_on_changeme(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, PERSONAL_CHANGEME)
    name, status, detail = cli._check_agent_id(cfg)
    assert name == "agent-id" and status == "warn" and "CHANGEME" in detail


def test_check_agent_id_env_override_beats_changeme(tmp_path, monkeypatch):
    # resolver.agent_id() lets FIREKEEP_AGENT_ID override the config value —
    # doctor must report the EFFECTIVE identity, not the stale config text.
    cfg = _cfg(tmp_path, monkeypatch, PERSONAL_CHANGEME)
    monkeypatch.setenv("FIREKEEP_AGENT_ID", "real-agent")
    name, status, detail = cli._check_agent_id(cfg)
    assert status == "ok" and "real-agent" in detail


def test_check_venv_scripts_partial_venv_is_diagnosed_distinctly(tmp_path):
    # venv dir + bin dir exist (creation started) but nothing landed inside —
    # this is NOT the same as "no venv at all": `firekeep install` skips venv
    # creation whenever the bin dir already exists, so a plain rerun won't
    # repair it. The detail string must say so.
    for is_win in (False, True):
        venv = tmp_path / f"partial-{is_win}"
        bindir = venv / ("Scripts" if is_win else "bin")
        bindir.mkdir(parents=True, exist_ok=True)
        name, status, detail = cli._check_venv_scripts(venv, is_windows=is_win)
        assert name == "venv-scripts" and status == "fail"
        assert "partial venv" in detail.lower()
        assert "install" in detail.lower()


def test_check_venv_scripts_missing_entirely_is_not_labeled_partial(tmp_path):
    # A venv dir that was never created at all is a plainer failure mode —
    # a normal `firekeep install` DOES handle this case, so it should not carry
    # the "delete and reinstall" partial-venv guidance.
    venv = tmp_path / "does-not-exist"
    name, status, detail = cli._check_venv_scripts(venv, is_windows=False)
    assert status == "fail"
    assert "partial venv" not in detail.lower()


def test_run_doctor_includes_agent_id_check(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, PERSONAL_CHANGEME)
    monkeypatch.setattr(cli, "_check_health", lambda cfg: [])
    monkeypatch.setattr(cli, "_check_skew", lambda cfg: ("version-skew", "ok", ""))
    monkeypatch.setattr(cli, "_check_venv_scripts", lambda venv, is_windows=None: ("venv-scripts", "ok", ""))
    monkeypatch.setattr(cli, "_check_config_perms", lambda config, is_windows=None: ("config-perms", "ok", ""))
    monkeypatch.setattr(cli, "_check_ca_expiry", lambda cfg: None)
    results = cli.run_doctor(cfg)
    names = {n for n, _, _ in results}
    assert "agent-id" in names
    status = {n: s for n, s, _ in results}["agent-id"]
    assert status == "warn"


# --- api-key false-green trap (T27 review Critical) --------------------------

OFFICE_NO_KEY = textwrap.dedent("""\
    [active]
    profile = office

    [office]
    kind = paths
    scheme = https
    base_url = https://firekeep.office.example
    verify_tls = true
    ca_path = ~/.firekeep/firekeep-root-ca.crt
    api_key =
    agent_id = mogan
""")

OFFICE_WITH_KEY = OFFICE_NO_KEY.replace("api_key =", "api_key = nxs_doctorsecret")


def test_check_api_key_warns_on_empty_key_https(tmp_path, monkeypatch):
    """/health + /version are auth-exempt: a keyless office profile must not
    false-green the preflight."""
    cfg = _cfg(tmp_path, monkeypatch, OFFICE_NO_KEY)
    name, status, detail = cli._check_api_key(cfg)
    assert (name, status) == ("api-key", "warn")
    assert "401" in detail


def test_check_api_key_ok_when_key_set_and_never_printed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, OFFICE_WITH_KEY)
    name, status, detail = cli._check_api_key(cfg)
    assert (name, status) == ("api-key", "ok")
    assert "nxs_doctorsecret" not in detail  # redacted


def test_check_api_key_skipped_for_plain_http(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, PERSONAL)
    assert cli._check_api_key(cfg) is None


def test_doctor_output_never_contains_api_key(tmp_path, monkeypatch, capsys):
    """Regression pin: the configured key appears NOWHERE in doctor output."""
    cfg = _cfg(tmp_path, monkeypatch, OFFICE_WITH_KEY)
    monkeypatch.setattr(cli, "get_json", lambda url, **kw: {"status": "ok",
                                                            "version": cli.__version__})
    results = cli.run_doctor(cfg)
    monkeypatch.setattr(cli, "run_doctor", lambda cfg=None: results)
    cli.cmd_doctor(None)
    out = capsys.readouterr()
    assert "nxs_doctorsecret" not in out.out
    assert "nxs_doctorsecret" not in out.err


# --- client-version staleness check (Task 3) ---------------------------------

def test_check_client_version_nudges_when_stale(monkeypatch):
    import configparser
    from firekeep_client import cli, updater

    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string("[dist]\nbase_url = http://gl/rel\n")
    monkeypatch.setattr(
        cli.updater, "fetch_manifest",
        lambda base, **kw: updater.Manifest(
            "9.9.9", bootstrap_sha256="cd" * 32, bootstrap_ps1_sha256="ef" * 32,
        ),
    )
    name, status, detail = cli._check_client_version(cfg)
    assert (name, status) == ("client-version", "warn")
    assert "firekeep update" in detail


def test_check_client_version_is_absent_without_dist_base(monkeypatch):
    """A checkout install has no [dist] section. That is not a fault — doctor must not
    invent a warning for a developer who never used the bootstrap."""
    import configparser
    from firekeep_client import cli

    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string("[active]\nprofile = personal\n")
    assert cli._check_client_version(cfg) is None


def test_check_client_version_degrades_when_manifest_unreachable(monkeypatch):
    import configparser
    from firekeep_client import cli, updater

    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string("[dist]\nbase_url = http://gl/rel\n")

    def boom(base, **kw):
        raise updater.UpdateError("cannot reach the release manifest")

    monkeypatch.setattr(cli.updater, "fetch_manifest", boom)
    name, status, _ = cli._check_client_version(cfg)
    assert (name, status) == ("client-version", "warn")  # never 'fail' — the client still works


def test_doctor_survives_a_malformed_manifest_version(monkeypatch, capsys):
    """A reachable manifest carrying a junk `version` must not take down the whole doctor
    run. run_doctor's contract is that each check is self-contained — one check's failure can
    never mask the rest — so a bad release deploy must NOT hide the agent-id, api-key, venv
    and CA results a teammate is running doctor to see."""
    import configparser
    from firekeep_client import cli, updater

    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string("[dist]\nbase_url = http://gl/rel\n")
    monkeypatch.setattr(
        cli.updater, "fetch_manifest",
        lambda base, **kw: updater.Manifest(
            "not-a-version", bootstrap_sha256="cd" * 32, bootstrap_ps1_sha256="ef" * 32,
        ),
    )
    name, status, detail = cli._check_client_version(cfg)
    assert (name, status) == ("client-version", "warn")
    assert "unparseable version" in detail


# --- pin hygiene + per-pinned-profile checks (Task 8) ------------------------


def _doctor_cfg(extra=""):
    cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    cfg.read_string("""
[active]
profile = personal
[personal]
agent_id = tester
scheme = http
[office]
agent_id = tester
scheme = https
""" + extra)
    return cfg


def test_check_pins_ok_and_warnings():
    from firekeep_client import cli
    rows = cli._check_pins(_doctor_cfg("[pins]\nkiro = office\n"))
    assert ("pins", "ok", "kiro -> office") in rows
    rows = cli._check_pins(_doctor_cfg("[pins]\nkiro = ghost\n"))
    assert any(s == "warn" and "ghost" in d for _, s, d in rows)
    rows = cli._check_pins(_doctor_cfg("[pins]\nvscode = office\n"))
    assert any(s == "warn" and "vscode" in d for _, s, d in rows)
    assert cli._check_pins(_doctor_cfg()) == []


def test_check_pins_warns_on_reserved_section_pin():
    """A hand-edited `kiro = active` pin passes the charset check AND has_section (the
    [active] section EXISTS — as does [pins] itself for a `kiro = pins` pin), so without
    a reserved-name branch _check_pins reports it ok. It must warn instead, for all
    three reserved sections."""
    from firekeep_client import cli
    for name in ("active", "pins", "dist"):
        rows = cli._check_pins(_doctor_cfg(f"[pins]\nkiro = {name}\n"))
        assert any(s == "warn" and "reserved" in d for _, s, d in rows), rows
        assert not any(s == "ok" for _, s, _ in rows), rows


def test_doctor_checks_pinned_profile_api_key():
    """kiro pinned to an https office profile with an empty api_key must WARN even while
    active=personal is all-ok — the false-green trap, per-pin edition."""
    from firekeep_client import cli
    cfg = _doctor_cfg("[pins]\nkiro = office\n")
    row = cli._check_api_key(cfg, profile="office", label="api-key[pin:kiro->office]")
    assert row is not None
    name, status, detail = row
    assert name == "api-key[pin:kiro->office]" and status == "warn"


def test_check_ca_expiry_os_trust_reports_ok(tmp_path, monkeypatch):
    """ca_path = os: the OS owns rotation — no file to stat, parse, or expire."""
    text = textwrap.dedent("""\
        [active]
        profile = office
        [office]
        kind = paths
        scheme = https
        base_url = https://firekeep.office.example
        verify_tls = true
        ca_path = os
        api_key = nxs_k
        agent_id = tester
    """)
    cfg = _cfg(tmp_path, monkeypatch, text)
    label, status, detail = cli._check_ca_expiry(cfg)
    assert status == "ok"
    assert "OS trust store" in detail
