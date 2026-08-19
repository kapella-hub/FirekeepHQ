import configparser
import importlib.util
import json
import os
import sys
import textwrap
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from firekeep_client import __version__, backups, cli, resolver, transport, updater
from firekeep_client.adapters import get_adapter

# The docdex doctor rows read the wheel's own sources/state modules. In a
# monorepo checkout it may not be pip-installed; its sibling source dir is
# stdlib-only at those modules, so a path fallback lets the rows be tested
# against the REAL reader rather than a stub of it.
if importlib.util.find_spec("firekeep_docdex") is None:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "docdex" / "src"))

SERVER = textwrap.dedent("""\
    [identity]
    agent_id = tester
    [server]
    kind = ports
    scheme = http
    host = 10.0.0.5
    verify_tls = false
""")

SERVER_CHANGEME = textwrap.dedent("""\
    [identity]
    agent_id = CHANGEME
    [server]
    kind = ports
    scheme = http
    host = 10.0.0.5
    verify_tls = false
""")


def _cfg(tmp_path, monkeypatch, text):
    cfg = tmp_path / ".firekeep" / "config"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text, encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    # Config agent_id is authoritative unless a test opts into the env
    # override (matches the convention in tests/conftest.py::firekeep_env) --
    # otherwise a real FIREKEEP_AGENT_ID in the ambient shell leaks in here.
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    return resolver.load_config(cfg)


def test_check_health_all_ok(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, SERVER)
    monkeypatch.setattr(cli, "get_json", lambda url, **kw: {"status": "ok"})
    results = cli._check_health(cfg)
    assert {svc for svc, _, _ in results} == set(resolver.SERVICES)
    assert all(status == "ok" for _, status, _ in results)


def test_check_health_reports_fail_on_transport_error(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, SERVER)

    def boom(url, **kw):
        raise transport.TransportError("refused", status=None)

    monkeypatch.setattr(cli, "get_json", boom)
    results = cli._check_health(cfg)
    assert all(status == "fail" for _, status, _ in results)


def test_versions_row_is_ok_for_a_REALISTIC_server_version(tmp_path, monkeypatch):
    """THE REGRESSION GUARD.

    The old test stubbed the server as returning the CLIENT's own version and
    asserted "ok" — an input production cannot produce, since the client ships on
    `client-v*` (0.1.23) and the server on `v[0-9]+.[0-9]+.[0-9]+`. It proved the
    code worked for a case that never occurs, which is exactly why `doctor` shipped
    a `version-skew: warn` on every correct install without any test noticing.

    So this stubs a REAL server version and requires ok.
    """
    cfg = _cfg(tmp_path, monkeypatch, SERVER)
    monkeypatch.setattr(cli, "get_json", lambda url, **kw: {"version": "0.6.0"})
    name, status, detail = cli._check_versions(cfg)
    assert name == "versions"
    assert status == "ok", "a normal install must not warn about differing versions"
    assert __version__ in detail and "0.6.0" in detail   # reports BOTH, judges neither


def test_versions_row_never_warns_merely_because_they_differ(tmp_path, monkeypatch):
    """Differing versions are the NORMAL state, not a finding. Any version the
    server reports must produce ok — the row has no verdict to render."""
    cfg = _cfg(tmp_path, monkeypatch, SERVER)
    for server_version in ("0.6.0", "9.9.9", "1.0.0-rc1", __version__):
        monkeypatch.setattr(cli, "get_json", lambda url, _v=server_version, **kw: {"version": _v})
        _, status, _ = cli._check_versions(cfg)
        assert status == "ok", f"warned on server version {server_version!r}"


def test_versions_row_warns_when_server_reports_no_version(tmp_path, monkeypatch):
    """A reachable /version that yields nothing IS a real, checkable defect —
    unlike skew, which is not."""
    cfg = _cfg(tmp_path, monkeypatch, SERVER)
    monkeypatch.setattr(cli, "get_json", lambda url, **kw: {})
    _, status, _ = cli._check_versions(cfg)
    assert status == "warn"


def test_versions_row_warns_when_unreachable(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, SERVER)

    def boom(url, **kw):
        raise transport.TransportError("down")

    monkeypatch.setattr(cli, "get_json", boom)
    _, status, _ = cli._check_versions(cfg)
    assert status == "warn"  # unreachable is real; not a hard fail


def test_check_health_survives_ssl_errors_not_wrapped_by_transport(tmp_path, monkeypatch):
    # transport._build_ssl_context() runs OUTSIDE transport._request()'s own
    # try/except, so a malformed/unverifiable ca_path raises a raw
    # ssl.SSLError (an OSError subclass), not TransportError. A doctor check
    # must fail loud on ITS OWN row, never crash the whole preflight before
    # later checks (skew, agent-id, venv, perms, ca-expiry) get to run.
    import ssl as _ssl
    cfg = _cfg(tmp_path, monkeypatch, SERVER)

    def boom(url, **kw):
        raise _ssl.SSLError("no certificate or crl found")

    monkeypatch.setattr(cli, "get_json", boom)
    results = cli._check_health(cfg)
    assert {svc for svc, _, _ in results} == set(resolver.SERVICES)
    assert all(status == "fail" for _, status, _ in results)


def test_check_versions_survives_ssl_errors_not_wrapped_by_transport(tmp_path, monkeypatch):
    import ssl as _ssl
    cfg = _cfg(tmp_path, monkeypatch, SERVER)

    def boom(url, **kw):
        raise _ssl.SSLError("no certificate or crl found")

    monkeypatch.setattr(cli, "get_json", boom)
    _, status, _ = cli._check_versions(cfg)
    assert status == "warn"


def test_check_venv_scripts_present_and_missing(tmp_path):
    venv = tmp_path / "venv"
    for is_win in (False, True):
        binname = "Scripts" if is_win else "bin"
        ext = ".exe" if is_win else ""
        bindir = venv / binname
        bindir.mkdir(parents=True, exist_ok=True)
        for n in (
            "firekeep", "firekeep-shim", "firekeep-sidecar",
            "firekeep-decision", "firekeep-symdex",
        ):
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


def _tls_server(tmp_path, monkeypatch, ca_path):
    text = textwrap.dedent(f"""\
        [identity]
        agent_id = tester
        [server]
        kind = paths
        scheme = https
        base_url = https://firekeep.example
        verify_tls = true
        ca_path = {ca_path.as_posix()}
        api_key = nxs_k
    """)
    return _cfg(tmp_path, monkeypatch, text)


def test_check_ca_expiry_ok_warn_fail(tmp_path, monkeypatch):
    ca = tmp_path / "ca.crt"
    ca.write_text("dummy", encoding="utf-8")
    cfg = _tls_server(tmp_path, monkeypatch, ca)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(cli, "_cert_not_after", lambda p: now + timedelta(days=400))
    assert cli._check_ca_expiry(cfg)[1] == "ok"
    monkeypatch.setattr(cli, "_cert_not_after", lambda p: now + timedelta(days=10))
    assert cli._check_ca_expiry(cfg)[1] == "warn"
    monkeypatch.setattr(cli, "_cert_not_after", lambda p: now - timedelta(days=5))
    assert cli._check_ca_expiry(cfg)[1] == "fail"


def test_check_ca_expiry_skipped_on_plain_http(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, SERVER)
    assert cli._check_ca_expiry(cfg) is None


def test_cmd_doctor_exit_code(tmp_path, monkeypatch, capsys):
    _cfg(tmp_path, monkeypatch, SERVER)
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
    cfg = _cfg(tmp_path, monkeypatch, SERVER)
    name, status, detail = cli._check_agent_id(cfg)
    assert name == "agent-id" and status == "ok" and "tester" in detail


def test_check_agent_id_warns_on_changeme(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, SERVER_CHANGEME)
    name, status, detail = cli._check_agent_id(cfg)
    assert name == "agent-id" and status == "warn" and "CHANGEME" in detail


def test_check_agent_id_env_override_beats_changeme(tmp_path, monkeypatch):
    # resolver.agent_id() lets FIREKEEP_AGENT_ID override the config value —
    # doctor must report the EFFECTIVE identity, not the stale config text.
    cfg = _cfg(tmp_path, monkeypatch, SERVER_CHANGEME)
    monkeypatch.setenv("FIREKEEP_AGENT_ID", "real-agent")
    name, status, detail = cli._check_agent_id(cfg)
    assert status == "ok" and "real-agent" in detail


def test_check_venv_scripts_partial_venv_is_diagnosed_distinctly(tmp_path):
    # venv dir + bin dir exist (creation started) but nothing landed inside —
    # this is NOT the same as "no venv at all": `firekeep install` skips venv
    # creation whenever the bin dir already exists, so a plain rerun won't
    # repair it. The detail string must say so — for a LEGACY/checkout venv
    # (anything not inspected through the `current` alias) the only working
    # advice is still delete-and-rerun, so the word "delete" must survive.
    for is_win in (False, True):
        venv = tmp_path / f"partial-{is_win}"
        bindir = venv / ("Scripts" if is_win else "bin")
        bindir.mkdir(parents=True, exist_ok=True)
        name, status, detail = cli._check_venv_scripts(venv, is_windows=is_win)
        assert name == "venv-scripts" and status == "fail"
        assert "partial venv" in detail.lower()
        assert "delete" in detail.lower()
        assert "firekeep install" in detail


def test_check_venv_scripts_partial_venv_under_current_advises_update_not_delete(tmp_path):
    """The side-by-side branch of the partial-venv advice.

    When doctor inspects the venv THROUGH the `current` alias (venv.name ==
    CURRENT_LINK_NAME), the legacy delete-and-rerun advice is actively wrong on
    both counts: hand-deleting through the alias would gut the versioned venv
    behind the junction, and the release bootstrap ALREADY repairs this case
    itself — its fast path probes venvs/<V> with `python -I` and reprovisions an
    unhealthy one. So the advice must be a re-run of the installer or `firekeep
    update`, and must NOT tell the user to delete anything."""
    for is_win in (False, True):
        # A plain dir NAMED `current` is enough: the branch keys off venv.name,
        # not off link-ness (doctor may hold either the resolved or alias path).
        venv = tmp_path / ("win" if is_win else "posix") / cli.CURRENT_LINK_NAME
        bindir = venv / ("Scripts" if is_win else "bin")
        bindir.mkdir(parents=True, exist_ok=True)
        name, status, detail = cli._check_venv_scripts(venv, is_windows=is_win)
        assert name == "venv-scripts" and status == "fail"
        assert "partial venv" in detail.lower()
        assert "firekeep update" in detail
        assert "delete" not in detail.lower()


def test_check_venv_scripts_missing_entirely_is_not_labeled_partial(tmp_path):
    # A venv dir that was never created at all is a plainer failure mode —
    # a normal `firekeep install` DOES handle this case, so it should not carry
    # the "delete and reinstall" partial-venv guidance.
    venv = tmp_path / "does-not-exist"
    name, status, detail = cli._check_venv_scripts(venv, is_windows=False)
    assert status == "fail"
    assert "partial venv" not in detail.lower()


def test_check_venv_scripts_requires_local_gateway_backends(tmp_path):
    venv = tmp_path / "venv"
    bindir = venv / "bin"
    bindir.mkdir(parents=True)
    for name in ("python", "firekeep", "firekeep-shim", "firekeep-sidecar"):
        (bindir / name).touch()

    _, status, detail = cli._check_venv_scripts(venv, is_windows=False)
    assert status == "fail"
    assert "firekeep-decision" in detail
    assert "firekeep-symdex" in detail


# --- current-link (side-by-side venvs, client 0.1.35) -------------------------
#
# Every rendered surface — shims, all four adapters, the /personal command —
# routes through ~/.firekeep/current, so a missing or mispointed alias is a dead
# client that still LOOKS installed (the versioned venvs are all healthy; nothing
# else would fail loudly). Doctor's `current-link` row is the one place that
# names it. These tests build REAL links via cli._point_current — a junction on
# Windows, a symlink on POSIX — so the row is exercised against the same
# primitive the installer uses, not a stand-in.

def test_check_current_link_absent_on_a_pure_legacy_layout(tmp_path):
    """A pre-0.1.35 install (legacy home/venv, no venvs/, no current) is not a
    fault — it just hasn't updated yet. Inventing a fail row for every existing
    customer the day 0.1.35 ships would teach them doctor cries wolf."""
    home = tmp_path / ".firekeep"
    (home / "venv").mkdir(parents=True)
    assert cli._check_current_link(home) is None


def test_check_current_link_fails_when_missing_beside_venvs(tmp_path):
    """venvs/ existing WITHOUT a current link is a half-migrated install (e.g. a
    bootstrap crash after provisioning but before the flip): every rendered
    surface points at a path that does not exist. The advice must name the
    repair — re-running the installer/update, whose fast path re-flips without
    re-downloading."""
    home = tmp_path / ".firekeep"
    (home / "venvs" / "1.0.0").mkdir(parents=True)
    name, status, detail = cli._check_current_link(home)
    assert (name, status) == ("current-link", "fail")
    assert "firekeep update" in detail


def test_check_current_link_fails_when_dangling(tmp_path):
    """A current link whose target venv was removed (crashed GC, hand-deleted
    dir) must fail, not warn: every spawn through it dies file-not-found.

    Only the status is pinned, not which fail message: a dangling POSIX symlink
    still is_symlink() and takes the explicit 'dangling' branch, while a
    dangling Windows JUNCTION reports neither is_symlink() nor exists() (the
    probe on this repo's own Windows box confirmed both are False), so it lands
    in the 'missing' branch. Both are fail rows naming the same repair."""
    import shutil

    home = tmp_path / ".firekeep"
    venv = home / "venvs" / "1.0.0"
    venv.mkdir(parents=True)
    cli._point_current(home, venv)
    shutil.rmtree(venv)  # the link node survives; its target is gone

    result = cli._check_current_link(home)
    assert result is not None, "a dangling alias must never read as legacy-and-fine"
    name, status, detail = result
    assert (name, status) == ("current-link", "fail")
    assert "update" in detail


def test_check_current_link_warns_on_version_mismatch(tmp_path):
    """current -> venvs/<other-version> while THIS client is __version__: warn,
    not fail — it is the normal state mid-update (the flip lands before the old
    process exits) and the legitimate state right after `firekeep update --to
    <prev>`. The detail must name both versions so a human can tell stale from
    mid-flight."""
    home = tmp_path / ".firekeep"
    venv = home / "venvs" / "0.0.1"
    venv.mkdir(parents=True)
    cli._point_current(home, venv)
    name, status, detail = cli._check_current_link(home)
    assert (name, status) == ("current-link", "warn")
    assert "0.0.1" in detail
    assert __version__ in detail


def test_check_current_link_ok_when_pointing_at_the_installed_version(tmp_path):
    home = tmp_path / ".firekeep"
    venv = home / "venvs" / __version__
    venv.mkdir(parents=True)
    cli._point_current(home, venv)
    name, status, detail = cli._check_current_link(home)
    assert (name, status) == ("current-link", "ok")
    assert __version__ in detail


def _render_codex(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    venv = tmp_path / "venv"
    bindir = venv / ("Scripts" if os.name == "nt" else "bin")
    get_adapter("codex").render(venv_bin=bindir)
    return home, venv


def test_check_codex_adapter_reports_healthy_generated_files(tmp_path, monkeypatch):
    # codex-mcp only since 0.1.41: the codex-instructions row moved to
    # _check_instructions (hash-based, per-runtime) — asserted separately below.
    _, venv = _render_codex(tmp_path, monkeypatch)
    rows = cli._check_codex_adapter(venv)
    assert {name: status for name, status, _ in rows} == {"codex-mcp": "ok"}
    instr = {name: status for name, status, _ in cli._check_instructions()}
    assert instr == {"codex-instructions": "ok"}


def test_check_instructions_reports_absent_when_agents_md_deleted(tmp_path, monkeypatch):
    home, venv = _render_codex(tmp_path, monkeypatch)
    (home / ".codex" / "AGENTS.md").unlink()

    assert {n: s for n, s, _ in cli._check_codex_adapter(venv)} == {"codex-mcp": "ok"}
    rows = cli._check_instructions()
    statuses = {name: status for name, status, _ in rows}
    assert statuses["codex-instructions"] == "warn"
    detail = {name: detail for name, _, detail in rows}["codex-instructions"]
    assert "absent" in detail
    assert "install --runtime codex" in detail


def test_check_codex_adapter_fails_when_gateway_config_is_missing(tmp_path, monkeypatch):
    home, venv = _render_codex(tmp_path, monkeypatch)
    (home / ".codex" / "config.toml").unlink()

    rows = cli._check_codex_adapter(venv)
    assert {name: status for name, status, _ in rows} == {"codex-mcp": "fail"}
    instr = {name: status for name, status, _ in cli._check_instructions()}
    assert instr == {"codex-instructions": "ok"}


def test_check_instructions_reports_edited_on_a_hand_modified_block(tmp_path, monkeypatch):
    """A rendered block whose content was hand-edited contradicts its own
    h= stamp: the row must say 'edited', not merely 'stale'."""
    home, _venv = _render_codex(tmp_path, monkeypatch)
    instructions = home / ".codex" / "AGENTS.md"
    instructions.write_text(
        instructions.read_text(encoding="utf-8").replace(
            "decision_board_check", "old_decision_board_check", 1,
        ),
        encoding="utf-8",
    )

    rows = cli._check_instructions()
    (name, status, detail), = rows
    assert (name, status) == ("codex-instructions", "warn")
    assert "edited" in detail


def test_check_codex_adapter_fails_on_stale_gateway_command(tmp_path, monkeypatch):
    home, venv = _render_codex(tmp_path, monkeypatch)
    config = home / ".codex" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "command = '", "command = 'stale-", 1,
        ),
        encoding="utf-8",
    )

    rows = cli._check_codex_adapter(venv)
    status = {name: status for name, status, _ in rows}["codex-mcp"]
    assert status == "fail"


def test_check_codex_adapter_skips_an_unconfigured_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cli._check_codex_adapter(tmp_path / "venv") == []


def test_check_instructions_emits_no_rows_on_a_machine_with_no_runtimes(tmp_path, monkeypatch):
    """No ~/.claude, ~/.codex, ~/.kiro or opencode config dir -> no rows at all:
    doctor must not warn a user about runtimes they never installed."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert cli._check_instructions() == []


def test_check_instructions_reports_stale_on_an_older_renders_block(tmp_path, monkeypatch):
    """An INTACT older render — content matching its own stamp, both differing
    from this wheel — is 'stale', the re-render nudge, not 'edited'."""
    from firekeep_client.adapters.base import (
        INSTRUCTIONS_BEGIN_PREFIX, INSTRUCTIONS_END, _hash12,
    )
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    old_content = "old wheel's instruction text\n"
    old_hash = _hash12(old_content)
    stamped = (f"{INSTRUCTIONS_BEGIN_PREFIX} v=0.1.40 h={old_hash} — firekeep-owned "
               f"block, do not edit; re-rendered by `firekeep install` -->\n"
               f"{old_content}{INSTRUCTIONS_END}\n")
    agents = home / ".codex" / "AGENTS.md"
    agents.parent.mkdir(parents=True)
    agents.write_text(stamped, encoding="utf-8")

    (name, status, detail), = cli._check_instructions()
    assert (name, status) == ("codex-instructions", "warn")
    assert "stale" in detail and old_hash in detail


def test_check_instructions_legacy_unstamped_block_reads_stale_not_edited(tmp_path, monkeypatch):
    """A pre-0.1.41 block has no h= stamp, so a mismatch cannot be blamed on the
    user: it reads 'stale' (re-render migrates it to the stamped form)."""
    from firekeep_client.adapters.base import INSTRUCTIONS_END
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    legacy_begin = ("<!-- firekeep:instructions:begin — firekeep-owned block, do not "
                    "edit; re-rendered by `firekeep install` -->")
    agents = home / ".codex" / "AGENTS.md"
    agents.parent.mkdir(parents=True)
    agents.write_text(f"{legacy_begin}\nold text\n{INSTRUCTIONS_END}\n", encoding="utf-8")

    (name, status, detail), = cli._check_instructions()
    assert (name, status) == ("codex-instructions", "warn")
    assert "stale" in detail and "edited" not in detail


def test_run_doctor_includes_agent_id_check(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, SERVER_CHANGEME)
    monkeypatch.setattr(cli, "_check_health", lambda cfg: [])
    monkeypatch.setattr(cli, "_check_versions", lambda cfg: ("versions", "ok", ""))
    monkeypatch.setattr(cli, "_check_venv_scripts", lambda venv, is_windows=None: ("venv-scripts", "ok", ""))
    monkeypatch.setattr(cli, "_check_config_perms", lambda config, is_windows=None: ("config-perms", "ok", ""))
    monkeypatch.setattr(cli, "_check_ca_expiry", lambda cfg: None)
    monkeypatch.setattr(cli, "_check_codex_adapter", lambda venv: [
        ("codex-mcp", "ok", ""),
    ])
    monkeypatch.setattr(cli, "_check_instructions", lambda: [
        ("codex-instructions", "warn", ""),
    ])
    results = cli.run_doctor(cfg)
    names = {n for n, _, _ in results}
    assert "agent-id" in names
    status = {n: s for n, s, _ in results}["agent-id"]
    assert status == "warn"
    assert {"codex-mcp", "codex-instructions"} <= names
    # This tmp home has neither venvs/ nor a current link — a pure legacy
    # layout, so run_doctor must NOT surface a current-link row (see
    # test_check_current_link_absent_on_a_pure_legacy_layout for why).
    assert "current-link" not in names


def test_run_doctor_includes_the_current_link_row_on_a_side_by_side_layout(tmp_path, monkeypatch):
    """run_doctor must actually WIRE _check_current_link in: the unit tests
    above prove the check works, but a row that run_doctor never appends is a
    check nobody runs — the exact write-only-machinery failure mode this repo
    deletes features over."""
    cfg = _cfg(tmp_path, monkeypatch, SERVER)
    home = tmp_path / ".firekeep"
    venv = home / "venvs" / __version__
    venv.mkdir(parents=True)
    cli._point_current(home, venv)
    monkeypatch.setattr(cli, "_check_health", lambda cfg: [])
    monkeypatch.setattr(cli, "_check_versions", lambda cfg: ("versions", "ok", ""))
    monkeypatch.setattr(cli, "_check_api_key", lambda cfg: None)
    monkeypatch.setattr(cli, "_check_venv_scripts", lambda venv, is_windows=None: ("venv-scripts", "ok", ""))
    monkeypatch.setattr(cli, "_check_codex_adapter", lambda venv: [])
    monkeypatch.setattr(cli, "_check_instructions", lambda: [])
    monkeypatch.setattr(cli, "_check_config_perms", lambda config, is_windows=None: ("config-perms", "ok", ""))
    monkeypatch.setattr(cli, "_check_ca_expiry", lambda cfg: None)
    results = cli.run_doctor(cfg)
    rows = {n: s for n, s, _ in results}
    assert rows.get("current-link") == "ok"


# --- api-key false-green trap (T27 review Critical) --------------------------

HTTPS_NO_KEY = textwrap.dedent("""\
    [identity]
    agent_id = mogan

    [server]
    kind = paths
    scheme = https
    base_url = https://firekeep.example
    verify_tls = true
    ca_path = ~/.firekeep/firekeep-root-ca.crt
    api_key =
""")

HTTPS_WITH_KEY = HTTPS_NO_KEY.replace("api_key =", "api_key = nxs_doctorsecret")


def test_check_api_key_warns_on_empty_key_https(tmp_path, monkeypatch):
    """/health + /version are auth-exempt: a keyless https server must not
    false-green the preflight."""
    cfg = _cfg(tmp_path, monkeypatch, HTTPS_NO_KEY)
    name, status, detail = cli._check_api_key(cfg)
    assert (name, status) == ("api-key", "warn")
    assert "401" in detail


def test_check_api_key_ok_when_key_set_and_never_printed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, HTTPS_WITH_KEY)
    name, status, detail = cli._check_api_key(cfg)
    assert (name, status) == ("api-key", "ok")
    assert "nxs_doctorsecret" not in detail  # redacted


def test_check_api_key_skipped_for_plain_http(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, SERVER)
    assert cli._check_api_key(cfg) is None


def test_doctor_output_never_contains_api_key(tmp_path, monkeypatch, capsys):
    """Regression pin: the configured key appears NOWHERE in doctor output."""
    cfg = _cfg(tmp_path, monkeypatch, HTTPS_WITH_KEY)
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
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string("[identity]\nagent_id = tester\n")
    assert cli._check_client_version(cfg) is None


def test_check_client_version_degrades_when_manifest_unreachable(monkeypatch):
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


def test_retired_profile_env_is_reported(monkeypatch):
    monkeypatch.setenv("FIREKEEP_PROFILE", "office")
    name, status, detail = cli._check_retired_profile_env()
    assert (name, status) == ("retired-profile-env", "warn")
    assert "ignored" in detail


def test_check_ca_expiry_os_trust_reports_ok(tmp_path, monkeypatch):
    """ca_path = os: the OS owns rotation — no file to stat, parse, or expire."""
    text = textwrap.dedent("""\
        [identity]
        agent_id = tester
        [server]
        kind = paths
        scheme = https
        base_url = https://firekeep.example
        verify_tls = true
        ca_path = os
        api_key = nxs_k
    """)
    cfg = _cfg(tmp_path, monkeypatch, text)
    label, status, detail = cli._check_ca_expiry(cfg)
    assert status == "ok"
    assert "OS trust store" in detail


def test_status_is_an_alias_for_doctor():
    """`firekeep status` is what operators type first on an unfamiliar CLI —
    observed live before the alias existed. It must parse to the same handler
    as `doctor`, not error with 'invalid choice'."""
    parser = cli._build_parser()
    doctor_args = parser.parse_args(["doctor"])
    status_args = parser.parse_args(["status"])
    assert status_args.func is doctor_args.func
    assert status_args.func is cli.cmd_doctor


# --- dexes (dex registry milestone 1, Task A5) --------------------------------
#
# One row, and it is "ok" whether or not any dex is registered: absence is a
# CHOICE, not a fault. Since ROADMAP §5's 2026-08-19 amendment (symdex + docdex
# register themselves) an empty registry has exactly one cause — the user
# emptied it — so the row names the way back instead of making an offer. The one
# state that IS a fault is a dex registered on a machine whose wheel is gone —
# the gateway will mount a backend that cannot start, and the user's only
# evidence is tools that quietly stopped existing.


@pytest.fixture
def dexes_home(tmp_path, monkeypatch):
    from firekeep_client import dexes

    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "config"))
    monkeypatch.setattr(dexes, "is_installed", lambda manifest: True)
    return dexes


def test_dexes_row_frames_an_empty_registry_as_the_off_switch(dexes_home):
    """Was `..._offers_symdex_when_nothing_is_registered`, back when an empty
    registry was the SHIPPED default and the row was an invitation. Default-on
    (ROADMAP §5, 2026-08-19) makes that state unreachable except by choice, so
    the row now says whose choice it was and how to undo it."""
    dexes_home.write_registry({})
    (row,) = cli._check_dexes()
    name, status, detail = row
    assert (name, status) == ("dexes", "ok")
    assert "none registered" in detail
    assert "you removed them" in detail
    assert "firekeep dex add symdex" in detail
    assert "firekeep dex add docdex" in detail


def test_the_empty_registry_repair_is_a_command_that_exists(dexes_home):
    """`firekeep dex add` takes ONE name — a copy-pasteable `dex add symdex
    docdex` would be a usage error dressed up as a repair."""
    dexes_home.write_registry({})
    (row,) = cli._check_dexes()
    assert "dex add symdex docdex" not in row[2]

    args = cli._build_parser().parse_args(["dex", "add", "symdex"])
    assert args.name == "symdex"
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["dex", "add", "symdex", "docdex"])


def test_dexes_row_names_what_is_registered(dexes_home):
    dexes_home.add("symdex")
    (row,) = cli._check_dexes()
    name, status, detail = row
    assert (name, status) == ("dexes", "ok")
    assert "symdex" in detail and "registered" in detail


def test_dexes_row_warns_when_a_registered_dex_has_no_wheel(dexes_home, monkeypatch):
    dexes_home.add("symdex")
    monkeypatch.setattr(dexes_home, "is_installed", lambda manifest: False)
    (row,) = cli._check_dexes()
    name, status, detail = row
    assert (name, status) == ("dexes", "warn")
    assert "symdex" in detail


def test_run_doctor_includes_the_dexes_row(tmp_path, monkeypatch):
    """A check run_doctor never appends is a check nobody runs."""
    cfg = _cfg(tmp_path, monkeypatch, SERVER)
    monkeypatch.setattr(cli, "_check_health", lambda cfg: [])
    monkeypatch.setattr(cli, "_check_versions", lambda cfg: ("versions", "ok", ""))
    monkeypatch.setattr(cli, "_check_api_key", lambda cfg: None)
    monkeypatch.setattr(cli, "_check_venv_scripts", lambda venv, is_windows=None: ("venv-scripts", "ok", ""))
    monkeypatch.setattr(cli, "_check_codex_adapter", lambda venv: [])
    monkeypatch.setattr(cli, "_check_instructions", lambda: [])
    monkeypatch.setattr(cli, "_check_config_perms", lambda config, is_windows=None: ("config-perms", "ok", ""))
    monkeypatch.setattr(cli, "_check_ca_expiry", lambda cfg: None)
    results = cli.run_doctor(cfg)
    assert [n for n, _, _ in results].count("dexes") == 1


# --- docdex accounting (Task C3) ---------------------------------------------
#
# A SECOND row, appended only when docdex is registered. It is read entirely
# from disk — sources.json plus one state file per source — because doctor is
# something people run when the server is the thing that is broken, and a check
# that needs the server to answer "is the server reachable?" answers nothing.


def _docdex_source(tmp_path, name="notes"):
    from firekeep_docdex import sources

    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    return sources.add(folder)


@pytest.fixture
def docdex_home(dexes_home, tmp_path):
    dexes_home.add("docdex")
    return tmp_path


def test_no_docdex_row_when_the_dex_is_not_registered(dexes_home):
    dexes_home.add("symdex")
    assert [name for name, _, _ in cli._check_dexes()] == ["dexes"]


def test_docdex_row_reports_an_empty_registry_and_offers_the_first_folder(docdex_home):
    rows = cli._check_dexes()
    name, status, detail = rows[-1]
    assert (name, status) == ("docdex", "ok")
    assert "0 sources" in detail
    assert "firekeep docdex add" in detail


def test_docdex_row_counts_sources_and_reports_staleness(docdex_home, tmp_path):
    from firekeep_docdex import state

    first = _docdex_source(tmp_path, "notes")
    _docdex_source(tmp_path, "runbooks")
    st = state.read_state(first.id)
    st.last_sync_at = state.now()
    st.last_walk_completed = True
    state.write_state(first.id, st)

    name, status, detail = cli._check_dexes()[-1]
    assert (name, status) == ("docdex", "ok")
    assert "2 sources" in detail
    # The STALEST source, not the freshest: a row that reported "just now"
    # because one of five folders synced would hide the four that never did.
    assert "never" in detail


def test_docdex_row_warns_on_pending_deletes(docdex_home, tmp_path):
    from firekeep_docdex import state

    src = _docdex_source(tmp_path)
    st = state.read_state(src.id)
    state.record_ingested(st, "a.md", "cafe")
    state.mark_pending_delete(st, "a.md")
    state.write_state(src.id, st)

    name, status, detail = cli._check_dexes()[-1]
    assert (name, status) == ("docdex", "warn")
    assert "1 pending delete" in detail


def test_docdex_row_warns_on_failures(docdex_home, tmp_path):
    from firekeep_docdex import state

    src = _docdex_source(tmp_path)
    st = state.read_state(src.id)
    state.record_failure(st, "a.md", "cafe", "503 busy")
    state.write_state(src.id, st)

    name, status, detail = cli._check_dexes()[-1]
    assert (name, status) == ("docdex", "warn")
    assert "1 failure" in detail


def test_docdex_row_fails_when_the_wheel_is_gone(docdex_home, monkeypatch):
    """Registered but not importable is the state where nothing syncs and
    nothing says why — the one docdex fault doctor can see without a server."""
    monkeypatch.setitem(sys.modules, "firekeep_docdex", None)
    name, status, detail = cli._check_dexes()[-1]
    assert (name, status) == ("docdex", "fail")
    assert detail == "wheel not importable — reinstall via bootstrap"


def test_docdex_row_makes_no_server_call(docdex_home, tmp_path, monkeypatch):
    """Doctor stays offline and fast. Building a wire client here would make
    the row hang on exactly the machine whose server is down."""
    from firekeep_docdex import wire

    _docdex_source(tmp_path)
    monkeypatch.setattr(wire, "Client", _never_called)
    monkeypatch.setattr(cli, "get_json", _never_called)
    name, status, _ = cli._check_dexes()[-1]
    assert (name, status) == ("docdex", "ok")


def _never_called(*_a, **_kw):
    raise AssertionError("doctor must not talk to the server here")


def test_docdex_row_survives_an_unreadable_sources_file(docdex_home, tmp_path):
    """A corrupt registry costs the numbers, never the doctor run."""
    from firekeep_docdex import sources

    _docdex_source(tmp_path)
    sources.sources_path().write_text("{ not json", encoding="utf-8")
    name, status, _ = cli._check_dexes()[-1]
    assert name == "docdex" and status in ("ok", "warn")


def test_run_doctor_includes_the_docdex_row_once_registered(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, SERVER)
    from firekeep_client import dexes

    monkeypatch.setattr(dexes, "is_installed", lambda manifest: True)
    dexes.add("docdex")
    monkeypatch.setattr(cli, "_check_health", lambda cfg: [])
    monkeypatch.setattr(cli, "_check_versions", lambda cfg: ("versions", "ok", ""))
    monkeypatch.setattr(cli, "_check_api_key", lambda cfg: None)
    monkeypatch.setattr(cli, "_check_venv_scripts",
                        lambda venv, is_windows=None: ("venv-scripts", "ok", ""))
    monkeypatch.setattr(cli, "_check_codex_adapter", lambda venv: [])
    monkeypatch.setattr(cli, "_check_instructions", lambda: [])
    monkeypatch.setattr(cli, "_check_config_perms",
                        lambda config, is_windows=None: ("config-perms", "ok", ""))
    monkeypatch.setattr(cli, "_check_ca_expiry", lambda cfg: None)
    names = [n for n, _, _ in cli.run_doctor(cfg)]
    assert names.count("dexes") == 1
    assert names.count("docdex") == 1


# --- maildex accounting (maildex plan Task I) --------------------------------
#
# The docdex row's twin, with one deliberate difference: it reads the files
# itself instead of importing the wheel. maildex is the module that holds a
# mailbox app password in memory for the length of a sync, and `firekeep doctor`
# has no business loading it to count accounts.


@pytest.fixture
def maildex_home(dexes_home, tmp_path):
    dexes_home.add("maildex")
    return tmp_path


def _write_accounts(tmp_path, entries):
    path = tmp_path / "maildex" / "accounts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")


def _write_maildex_state(tmp_path, account_id, **fields):
    path = tmp_path / "maildex" / "state" / f"{account_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, **fields}), encoding="utf-8")


def _account(username="you@example.com"):
    return {"host": "imap.example.com", "port": 993, "username": username,
            "folders": ["INBOX", "Sent"], "backfill_days": 90}


def test_no_maildex_row_when_the_dex_is_not_registered(dexes_home):
    dexes_home.add("symdex")
    assert [name for name, _, _ in cli._check_dexes()] == ["dexes"]


def test_maildex_row_offers_the_first_mailbox_when_none_is_connected(maildex_home):
    name, status, detail = cli._check_dexes()[-1]
    assert (name, status) == ("maildex", "ok")
    assert "0 accounts" in detail
    assert "firekeep maildex add" in detail


def test_maildex_row_counts_accounts_and_reports_the_stalest(maildex_home, tmp_path):
    _write_accounts(tmp_path, {"a" * 32: _account(), "b" * 32: _account("other@x")})
    _write_maildex_state(tmp_path, "a" * 32,
                         last_sync_at=datetime.now(timezone.utc).isoformat())

    name, status, detail = cli._check_dexes()[-1]
    assert (name, status) == ("maildex", "ok")
    assert "2 accounts" in detail
    # The STALEST account, not the freshest: "just now" earned by one of two
    # mailboxes would hide the one that has never synced at all.
    assert "last sync never" in detail


def test_maildex_row_warns_on_ingest_failures(maildex_home, tmp_path):
    _write_accounts(tmp_path, {"a" * 32: _account()})
    _write_maildex_state(
        tmp_path, "a" * 32,
        last_sync_at=datetime.now(timezone.utc).isoformat(),
        messages={"1": {"uid": 1}, "2": {"uid": 2, "error": "503 busy"}},
    )

    name, status, detail = cli._check_dexes()[-1]
    assert (name, status) == ("maildex", "warn")
    assert "1 failure" in detail


def test_maildex_row_fails_when_the_wheel_is_gone(maildex_home, monkeypatch):
    """Registered but not installed is the state where nothing syncs and nothing
    says why — the one maildex fault doctor can see without a server."""
    from firekeep_client import dexes

    monkeypatch.setattr(dexes, "is_installed",
                        lambda manifest: manifest.name != "maildex")
    name, status, detail = cli._check_dexes()[-1]
    assert (name, status) == ("maildex", "fail")
    assert detail == "wheel not importable — reinstall via bootstrap"


def test_maildex_row_never_imports_the_wheel(maildex_home, tmp_path, monkeypatch):
    """A `None` entry in sys.modules makes any import of it raise. The row must
    still answer — proof it reads the files rather than loading the module that
    handles the password."""
    monkeypatch.setitem(sys.modules, "firekeep_maildex", None)
    _write_accounts(tmp_path, {"a" * 32: _account()})

    name, status, detail = cli._check_dexes()[-1]
    assert (name, status) == ("maildex", "ok")
    assert "1 account" in detail


def test_maildex_row_makes_no_server_call(maildex_home, tmp_path, monkeypatch):
    """Doctor stays offline and fast: the row must not hang on exactly the
    machine whose server is down."""
    _write_accounts(tmp_path, {"a" * 32: _account()})
    monkeypatch.setattr(cli, "get_json", _never_called)
    name, status, _ = cli._check_dexes()[-1]
    assert (name, status) == ("maildex", "ok")


def test_maildex_row_survives_an_unreadable_accounts_file(maildex_home, tmp_path):
    """A corrupt registry costs the numbers, never the doctor run."""
    path = tmp_path / "maildex" / "accounts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    name, status, _ = cli._check_dexes()[-1]
    assert name == "maildex" and status in ("ok", "warn")


def test_run_doctor_includes_the_maildex_row_once_registered(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, SERVER)
    from firekeep_client import dexes

    monkeypatch.setattr(dexes, "is_installed", lambda manifest: True)
    dexes.add("maildex")
    monkeypatch.setattr(cli, "_check_health", lambda cfg: [])
    monkeypatch.setattr(cli, "_check_versions", lambda cfg: ("versions", "ok", ""))
    monkeypatch.setattr(cli, "_check_api_key", lambda cfg: None)
    monkeypatch.setattr(cli, "_check_venv_scripts",
                        lambda venv, is_windows=None: ("venv-scripts", "ok", ""))
    monkeypatch.setattr(cli, "_check_codex_adapter", lambda venv: [])
    monkeypatch.setattr(cli, "_check_instructions", lambda: [])
    monkeypatch.setattr(cli, "_check_config_perms",
                        lambda config, is_windows=None: ("config-perms", "ok", ""))
    monkeypatch.setattr(cli, "_check_ca_expiry", lambda cfg: None)
    names = [n for n, _, _ in cli.run_doctor(cfg)]
    assert names.count("dexes") == 1
    assert names.count("maildex") == 1


def test_venv_scripts_still_wants_every_bundled_wheel(tmp_path):
    """Registration gates MOUNTING, not installation. The wheels stay
    always-installed and checksum-verified, so removing a dex from the registry
    must not make its missing console script acceptable — that would hide a
    broken venv behind a preference."""
    from firekeep_client import dexes

    venv = tmp_path / "venv"
    bindir = venv / "bin"
    bindir.mkdir(parents=True)
    for name in ("python", "firekeep", "firekeep-shim", "firekeep-sidecar",
                 "firekeep-decision"):
        (bindir / name).touch()
    dexes.write_registry({})  # symdex deliberately NOT registered

    _, status, detail = cli._check_venv_scripts(venv, is_windows=False)
    assert status == "fail"
    assert "firekeep-symdex" in detail


# --- backup (Keep backup, Task C2) -------------------------------------------
#
# The row exists because a Keep whose only copy lives on one disk is one disk
# failure from gone, and nobody types `firekeep backup status` to find that out
# — they find out during the disaster. So doctor says it unprompted, and the
# three states it distinguishes are the three that change what you would do:
# fine, a nightly was missed, and there has never been a backup at all.
#
# These stub `backups.get_json` rather than `cli.get_json`: the row's whole body
# is `backups.doctor_row`, and that is deliberate — `firekeep backup status` and
# this row must never drift about what "stale" means.

BACKUP_POLICY = "nightly 04:30 · keep 7 nightly + 4 weekly"


def _backup_payload(*entries, enabled=True):
    return {"enabled": enabled, "policy": BACKUP_POLICY, "backups": list(entries)}


def _backup_entry(stamp="20260818-043000", age=3600.0, indexed=True):
    return {"stamp": stamp, "age_seconds": age, "mode": "cold",
            "total_bytes": 412_000_000, "indexed": indexed}


def test_backup_row_is_ok_when_a_nightly_ran_recently(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, SERVER)
    monkeypatch.setattr(backups, "get_json",
                        lambda url, **kw: _backup_payload(_backup_entry()))
    name, status, detail = cli._check_backup()
    assert (name, status) == ("backup", "ok")
    assert "1h ago" in detail
    assert BACKUP_POLICY in detail


def test_backup_row_warns_when_the_newest_is_older_than_36h(tmp_path, monkeypatch):
    """36h, not 24: one missed nightly is the signal, and a tighter threshold
    would cry wolf every time a box was off for an evening."""
    _cfg(tmp_path, monkeypatch, SERVER)
    monkeypatch.setattr(backups, "get_json",
                        lambda url, **kw: _backup_payload(_backup_entry(age=40 * 3600)))
    _, status, detail = cli._check_backup()
    assert status == "warn"
    assert "stale" in detail


def test_backup_row_stays_ok_just_under_the_threshold(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, SERVER)
    monkeypatch.setattr(backups, "get_json",
                        lambda url, **kw: _backup_payload(_backup_entry(age=35 * 3600)))
    assert cli._check_backup()[1] == "ok"


def test_backup_row_fails_when_there_has_never_been_a_backup(tmp_path, monkeypatch):
    """The only red row this check can produce, and it earns it: nothing else
    in doctor means "one disk failure from gone"."""
    _cfg(tmp_path, monkeypatch, SERVER)
    monkeypatch.setattr(backups, "get_json",
                        lambda url, **kw: _backup_payload(enabled=False))
    _, status, detail = cli._check_backup()
    assert status == "fail"
    assert "one disk holds everything" in detail


def test_backup_row_warns_when_only_unindexed_backups_exist(tmp_path, monkeypatch):
    """A pre-feature or ad-hoc backup dir has no manifest, so nothing about it
    can be verified and `pull` will not touch it. That is not "backed up"."""
    _cfg(tmp_path, monkeypatch, SERVER)
    monkeypatch.setattr(
        backups, "get_json",
        lambda url, **kw: _backup_payload(_backup_entry(indexed=False)))
    _, status, detail = cli._check_backup()
    assert status == "warn"
    assert "none indexed" in detail or "not run yet" in detail


def test_backup_row_degrades_instead_of_blocking_when_the_server_is_down(
        tmp_path, monkeypatch):
    """Doctor is what people run when the SERVER is the broken thing. This row
    reports that and moves on — it never raises into the run."""
    _cfg(tmp_path, monkeypatch, SERVER)

    def boom(url, **kw):
        raise transport.TransportError("connection refused")

    monkeypatch.setattr(backups, "get_json", boom)
    name, status, detail = cli._check_backup()
    assert (name, status) == ("backup", "warn")
    assert "connection refused" in detail


def test_backup_row_spends_no_budget_when_nothing_is_reachable(tmp_path, monkeypatch):
    """When doctor has already concluded there is no server, this row must not
    re-prove it — but it must still be PRINTED, because a missing backup row
    reads as "backups are fine"."""
    _cfg(tmp_path, monkeypatch, SERVER)

    def never(url, **kw):
        raise AssertionError("no server call may be made when nothing is reachable")

    monkeypatch.setattr(backups, "get_json", never)
    name, status, detail = cli._check_backup(reachable=False)
    assert (name, status) == ("backup", "warn")
    assert "no Firekeep server is reachable" in detail


def test_backup_row_holds_a_five_second_budget(tmp_path, monkeypatch):
    """Every second here is a second added to a command someone runs while
    something is already wrong."""
    _cfg(tmp_path, monkeypatch, SERVER)
    seen = {}

    def capture(url, **kw):
        seen.update(kw)
        return _backup_payload(_backup_entry())

    monkeypatch.setattr(backups, "get_json", capture)
    cli._check_backup()
    assert seen["timeout"] == backups.DOCTOR_TIMEOUT == 5.0


def test_backup_row_discloses_a_stored_admin_key(tmp_path, monkeypatch):
    """A deployment ADMIN key sitting on a laptop is worth saying out loud
    wherever the user already looks — it opens every archive on the server."""
    _cfg(tmp_path, monkeypatch, SERVER)
    backups.store_admin_key("nxs_admin_key")
    monkeypatch.setattr(backups, "get_json",
                        lambda url, **kw: _backup_payload(_backup_entry()))
    _, _, detail = cli._check_backup()
    assert "admin key stored on this machine" in detail
    assert "nxs_admin_key" not in detail  # disclosed, never printed


def test_backup_row_reports_the_last_pull_on_this_machine(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, SERVER)
    monkeypatch.setattr(backups, "get_json",
                        lambda url, **kw: _backup_payload(_backup_entry()))
    assert "last pull here: never" in cli._check_backup()[2]


def test_run_doctor_includes_the_backup_row(tmp_path, monkeypatch):
    """A check run_doctor never appends is a check nobody runs."""
    cfg = _cfg(tmp_path, monkeypatch, SERVER)
    monkeypatch.setattr(backups, "get_json",
                        lambda url, **kw: _backup_payload(_backup_entry()))
    monkeypatch.setattr(cli, "_check_health", lambda cfg: [])
    monkeypatch.setattr(cli, "_check_versions", lambda cfg: ("versions", "ok", ""))
    monkeypatch.setattr(cli, "_check_embeddings", lambda cfg: None)
    monkeypatch.setattr(cli, "_check_api_key", lambda cfg: None)
    monkeypatch.setattr(cli, "_check_venv_scripts",
                        lambda venv, is_windows=None: ("venv-scripts", "ok", ""))
    monkeypatch.setattr(cli, "_check_codex_adapter", lambda venv: [])
    monkeypatch.setattr(cli, "_check_instructions", lambda: [])
    monkeypatch.setattr(cli, "_check_config_perms",
                        lambda config, is_windows=None: ("config-perms", "ok", ""))
    monkeypatch.setattr(cli, "_check_ca_expiry", lambda cfg: None)
    rows = {n: (s, d) for n, s, d in cli.run_doctor(cfg)}
    assert "backup" in rows
    assert rows["backup"][0] == "ok"


def test_backup_row_fails_when_the_directory_exists_but_is_empty(tmp_path, monkeypatch):
    """`enabled: true` with an empty list is reachable — the ./backups mount is
    there but no nightly has run yet. That is still "one disk holds
    everything", so it earns the same red row as a missing directory."""
    _cfg(tmp_path, monkeypatch, SERVER)
    monkeypatch.setattr(backups, "get_json",
                        lambda url, **kw: _backup_payload(enabled=True))
    _, status, detail = cli._check_backup()
    assert status == "fail"
    assert "one disk holds everything" in detail
