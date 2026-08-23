from firekeep_client import cli, report


def test_cli_main_flushes_before_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("FIREKEEP_REPORT_DIR", str(tmp_path))
    called = []
    monkeypatch.setattr(report, "flush", lambda *a, **k: called.append(True))
    monkeypatch.setattr(cli, "run_doctor", lambda: [])
    monkeypatch.setattr(cli, "_generic_hint", lambda: None)
    cli.main(["doctor"])
    assert called  # flush attempted on every CLI invocation


def test_session_start_flushes(monkeypatch, tmp_path):
    monkeypatch.setenv("FIREKEEP_REPORT_DIR", str(tmp_path))
    called = []
    monkeypatch.setattr(report, "flush", lambda *a, **k: called.append(True))
    from firekeep_client.hooks import session_start
    # run() is @never_raise({}); a full run needs no server — every step is
    # best-effort. Config may be absent in CI: monkeypatch load_config too.
    import configparser
    monkeypatch.setattr(session_start.resolver, "load_config",
                        lambda *a, **k: configparser.ConfigParser())
    monkeypatch.setattr(session_start.resolver, "agent_id", lambda cfg: "t")
    session_start.run({})
    assert called
