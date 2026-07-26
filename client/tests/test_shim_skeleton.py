"""Task 9 — shim skeleton: service validation, active-profile resolution, client headers.

Also carries forward three items flagged in the Task 9 review that were never
exercised by a test: the deprecated `verify=<str>` httpx passthrough, the
`follow_redirects=True` default, and three untested `run()`/`parse_args`
branches (missing --service, run() success, run() ConfigError)."""
import anyio
import pytest
from unittest.mock import AsyncMock

from firekeep_client import shim
from firekeep_client.resolver import Endpoint

OFFICE_KEY = "nxs_secret_office_key"


def _write_office_config(tmp_path):
    ca = tmp_path / "firekeep-root-ca.crt"          # path only needs to exist as a string for resolve()
    cfg = tmp_path / "config"
    cfg.write_text(
        "[active]\n"
        "profile = office\n"
        "\n"
        "[office]\n"
        "kind = paths\n"
        "scheme = https\n"
        "base_url = https://firekeep.office.example\n"
        "verify_tls = true\n"
        f"ca_path = {ca}\n"
        f"api_key = {OFFICE_KEY}\n"
        "agent_id = mogan\n",
        encoding="utf-8",
    )
    return cfg


def test_symdex_is_refused_before_any_resolution(capsys, monkeypatch, tmp_path):
    # Point config somewhere valid so we prove the refusal happens on the service
    # name, NOT because config is missing.
    monkeypatch.setenv("FIREKEEP_CONFIG", str(_write_office_config(tmp_path)))
    rc = shim.run("symdex")
    err = capsys.readouterr().err
    assert rc == 2
    assert "symdex" in err
    assert "never routed through the shim" in err


def test_unknown_service_is_refused(capsys):
    rc = shim.run("bogus")
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown service" in err
    assert "bogus" in err


def test_office_profile_resolves_headers_with_key(monkeypatch, tmp_path):
    monkeypatch.setenv("FIREKEEP_CONFIG", str(_write_office_config(tmp_path)))
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    endpoint, profile = shim.resolve_active("cortex")
    assert profile == "office"
    assert endpoint.headers["X-API-Key"] == OFFICE_KEY
    assert endpoint.headers["X-Agent-Id"] == "mogan"
    assert endpoint.mcp_url == "https://firekeep.office.example/mcp/cortex"
    # verify must carry the ca_path string through for build_client's ssl handoff
    # (https scheme => verify is the ca_path string, never True/False — resolver's
    # MITM guard already forbids https with verify_tls=false at load time).
    assert endpoint.verify == str(tmp_path / "firekeep-root-ca.crt")


def test_build_client_forwards_auth_headers():
    # build_client must pass the resolver's headers through to httpx verbatim.
    endpoint = Endpoint(
        mcp_url="http://198.51.100.7:8080/mcp",
        rest_base="http://198.51.100.7:8100",
        headers={"X-Agent-Id": "mogan", "X-API-Key": OFFICE_KEY},
        verify=False,
    )

    async def _scenario():
        client = shim.build_client(endpoint)
        try:
            assert client.headers.get("x-api-key") == OFFICE_KEY
            assert client.headers.get("x-agent-id") == "mogan"
        finally:
            await client.aclose()

    anyio.run(_scenario)


# --- Task 9 review carry-forwards -------------------------------------------


def test_build_client_str_verify_builds_explicit_ssl_context(monkeypatch):
    # httpx>=0.28 deprecates verify=<str> (DeprecationWarning out of
    # httpx._config.create_ssl_context) — build_client must build the
    # ssl.SSLContext itself via ssl.create_default_context(cafile=...) and
    # hand httpx the context object, never the raw path string.
    captured = {}
    sentinel_ctx = object()

    def fake_create_default_context(*, cafile=None, **kwargs):
        captured["cafile"] = cafile
        return sentinel_ctx

    monkeypatch.setattr(shim.ssl, "create_default_context", fake_create_default_context)

    seen_kwargs = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs):
            seen_kwargs.update(kwargs)

    monkeypatch.setattr(shim.httpx, "AsyncClient", _FakeAsyncClient)

    endpoint = Endpoint(
        mcp_url="https://firekeep.office.example/mcp/cortex",
        rest_base="https://firekeep.office.example/api/cortex",
        headers={"X-Agent-Id": "mogan"},
        verify="/path/to/firekeep-root-ca.crt",
    )
    shim.build_client(endpoint)

    assert captured["cafile"] == "/path/to/firekeep-root-ca.crt"
    assert seen_kwargs["verify"] is sentinel_ctx


def test_build_client_verify_false_is_not_turned_into_a_context(monkeypatch):
    # The personal plain-http profile's verify=False must pass straight
    # through — synthesizing a context for it would be a behavior change to
    # a path with no TLS handshake to verify.
    def _boom(**kwargs):
        raise AssertionError("create_default_context must not be called when verify=False")

    monkeypatch.setattr(shim.ssl, "create_default_context", _boom)

    seen_kwargs = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs):
            seen_kwargs.update(kwargs)

    monkeypatch.setattr(shim.httpx, "AsyncClient", _FakeAsyncClient)

    endpoint = Endpoint(
        mcp_url="http://198.51.100.7:8080/mcp",
        rest_base="http://198.51.100.7:8100",
        headers={"X-Agent-Id": "mogan"},
        verify=False,
    )
    shim.build_client(endpoint)

    assert seen_kwargs["verify"] is False


def test_build_client_disables_redirects():
    # X-API-Key is a custom header; httpx does NOT strip custom headers on
    # cross-origin redirects. The internal API has no legitimate redirects,
    # so build_client must never follow one.
    endpoint = Endpoint(
        mcp_url="http://198.51.100.7:8080/mcp",
        rest_base="http://198.51.100.7:8100",
        headers={"X-Agent-Id": "mogan", "X-API-Key": OFFICE_KEY},
        verify=False,
    )

    async def _scenario():
        client = shim.build_client(endpoint)
        try:
            assert client.follow_redirects is False
        finally:
            await client.aclose()

    anyio.run(_scenario)


def test_parse_args_missing_service_exits_2(capsys):
    with pytest.raises(SystemExit) as exc_info:
        shim.parse_args([])
    assert exc_info.value.code == 2


def test_run_success_path_delegates_to_serve_and_returns_0(monkeypatch, tmp_path):
    monkeypatch.setenv("FIREKEEP_CONFIG", str(_write_office_config(tmp_path)))
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    fake_serve = AsyncMock()
    monkeypatch.setattr(shim, "serve", fake_serve)

    rc = shim.run("cortex")

    assert rc == 0
    fake_serve.assert_awaited_once()
    args = fake_serve.await_args.args
    assert args[0] == "cortex"
    assert args[1].mcp_url == "https://firekeep.office.example/mcp/cortex"
    assert args[2] is None  # http_client injection seam, unset in production
    assert args[3] is None  # stdio_streams injection seam, unset in production


def test_run_config_error_returns_1_without_leaking_api_key(capsys, monkeypatch, tmp_path):
    # A profile with a bad 'kind' fails resolution AFTER headers (incl.
    # api_key) are built internally by resolve() — pin that the key never
    # reaches the exception message or the fail-loud stderr line.
    cfg = tmp_path / "config"
    cfg.write_text(
        "[active]\n"
        "profile = office\n"
        "\n"
        "[office]\n"
        "kind = bogus-kind\n"
        "scheme = http\n"
        f"api_key = {OFFICE_KEY}\n"
        "agent_id = mogan\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)

    rc = shim.run("cortex")
    err = capsys.readouterr().err

    assert rc == 1
    assert "config error" in err
    assert OFFICE_KEY not in err


def test_parse_args_accepts_profile():
    # Task 5: --profile lets a pinned runtime override [active]/FIREKEEP_PROFILE
    # for this one shim invocation; absent by default so unpinned callers are
    # unaffected.
    args = shim.parse_args(["--service", "cortex", "--profile", "office"])
    assert args.profile == "office"
    assert shim.parse_args(["--service", "cortex"]).profile is None
