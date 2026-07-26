import hashlib

import pytest

from firekeep_client import updater


def _cfg(text):
    import configparser
    c = configparser.ConfigParser(interpolation=None)
    c.read_string(text)
    return c


def test_dist_base_comes_from_config():
    cfg = _cfg("[dist]\nbase_url = http://gl/rel/v1\n")
    assert updater.dist_base(cfg) == "http://gl/rel/v1"


def test_dist_base_strips_trailing_slash():
    cfg = _cfg("[dist]\nbase_url = http://gl/rel/v1/\n")
    assert updater.dist_base(cfg) == "http://gl/rel/v1"


def test_dist_base_missing_is_a_loud_error():
    """An install that predates [dist] (or a hand-written config) must say exactly what to
    do, not fail with a KeyError three frames down."""
    with pytest.raises(updater.UpdateError, match="no \\[dist\\] base_url"):
        updater.dist_base(_cfg("[active]\nprofile = personal\n"))


@pytest.mark.parametrize("latest,current,expected", [
    ("1.2.3", "1.2.2", True),
    ("1.2.3", "1.2.3", False),
    ("1.2.3", "1.3.0", False),   # never "update" backwards on your own
    ("1.10.0", "1.9.0", True),   # numeric compare, not lexicographic
    ("2.0.0", "1.99.99", True),
])
def test_is_newer(latest, current, expected):
    assert updater.is_newer(latest, current) is expected


def test_parse_version_rejects_garbage():
    with pytest.raises(updater.UpdateError, match="unparseable version"):
        updater.parse_version("not-a-version")


MANIFEST_PAYLOAD = {
    "version": "1.2.3",
    "bootstrap_sha256": "cd" * 32,
    "bootstrap_ps1_sha256": "ef" * 32,
}


def test_fetch_manifest(monkeypatch):
    seen_url = {}

    def _fake_get_json(url, **kw):
        seen_url["url"] = url
        return dict(MANIFEST_PAYLOAD)

    monkeypatch.setattr(updater, "get_json", _fake_get_json)
    m = updater.fetch_manifest("http://gl/rel/v1")
    assert m.version == "1.2.3"
    assert m.bootstrap_sha256 == "cd" * 32
    assert m.bootstrap_ps1_sha256 == "ef" * 32
    # base is version-agnostic; the manifest lives under /latest/, the stable moving pointer
    # — not at base itself, which would make latest.json describe its own release forever.
    assert seen_url["url"] == "http://gl/rel/v1/latest/latest.json"


@pytest.mark.parametrize("windows,expected", [(False, "cd" * 32), (True, "ef" * 32)])
def test_manifest_bootstrap_hash_for_platform(windows, expected):
    m = updater.Manifest(**{k: v for k, v in MANIFEST_PAYLOAD.items()})
    assert m.bootstrap_hash_for(windows=windows) == expected


def test_fetch_manifest_rejects_incomplete_payload(monkeypatch):
    monkeypatch.setattr(updater, "get_json", lambda url, **kw: {"version": "1.2.3"})
    with pytest.raises(updater.UpdateError, match="malformed manifest"):
        updater.fetch_manifest("http://gl/rel/v1")


def test_fetch_manifest_rejects_a_manifest_missing_the_bootstrap_hashes(monkeypatch):
    """An old-format manifest must fail loudly rather than silently returning a Manifest
    whose bootstrap hash is empty — that would take us straight back to exec'ing an
    unverified script."""
    payload = {"version": "1.2.3"}
    monkeypatch.setattr(updater, "get_json", lambda url, **kw: payload)
    with pytest.raises(updater.UpdateError, match="malformed manifest"):
        updater.fetch_manifest("http://gl/rel/v1")


def test_fetch_manifest_rejects_a_manifest_carrying_the_retired_wheel_fields(monkeypatch):
    """wheel_url/sha256 are gone from the contract; a stray legacy field alongside the three
    required ones must not be silently accepted as if it still meant something. This mostly
    guards against a manifest builder regressing back to the old 5-field shape without the
    client noticing — the field-count check inside fetch_manifest is what would catch that
    (an extra key doesn't fail construction, but this documents the shrink is real)."""
    payload = dict(MANIFEST_PAYLOAD, wheel_url="http://gl/x.whl", sha256="ab" * 32)
    monkeypatch.setattr(updater, "get_json", lambda url, **kw: payload)
    m = updater.fetch_manifest("http://gl/rel/v1")
    assert not hasattr(m, "wheel_url")
    assert not hasattr(m, "sha256")


def test_fetch_manifest_wraps_transport_failure(monkeypatch):
    from firekeep_client.transport import TransportError

    def boom(url, **kw):
        raise TransportError("connection refused")

    monkeypatch.setattr(updater, "get_json", boom)
    with pytest.raises(updater.UpdateError, match="cannot reach"):
        updater.fetch_manifest("http://gl/rel/v1")


@pytest.mark.parametrize("windows,tail", [(False, "/latest/install.sh"), (True, "/latest/install.ps1")])
def test_bootstrap_url(windows, tail):
    assert updater.bootstrap_url("http://gl/rel/v1", windows=windows) == "http://gl/rel/v1" + tail


def test_download_verifies_sha256(tmp_path, monkeypatch):
    body = b"wheel-bytes"
    digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(updater, "_read_url", lambda url, timeout: body)
    dest = tmp_path / "x.whl"
    assert updater.download("http://gl/x.whl", dest, sha256=digest) == dest
    assert dest.read_bytes() == body


def test_download_cannot_be_called_without_a_checksum(tmp_path, monkeypatch):
    """Structural, not stylistic: `firekeep update` downloads the bootstrap script and then
    EXECUTES it. If sha256 were optional, the first caller to forget it would be the one
    fetching that script — verifying uv while exec'ing the unverified script that runs uv.
    The type must make that impossible."""
    monkeypatch.setattr(updater, "_read_url", lambda url, timeout: b"x")
    with pytest.raises(TypeError):
        updater.download("http://gl/x.whl", tmp_path / "x.whl")  # no sha256=


def test_download_rejects_a_checksum_mismatch(tmp_path, monkeypatch):
    """The artifact fetch is unauthenticated plain HTTP inside the office network, so the
    checksum is the ONLY thing standing between a teammate and someone else's code."""
    monkeypatch.setattr(updater, "_read_url", lambda url, timeout: b"tampered")
    dest = tmp_path / "x.whl"
    with pytest.raises(updater.UpdateError, match="checksum mismatch"):
        updater.download("http://gl/x.whl", dest, sha256="00" * 32)
    assert not dest.exists(), "a file failing verification must never be left on disk"


def test_dist_ssl_context_none_without_truststore(monkeypatch):
    import sys
    from firekeep_client import updater
    monkeypatch.setitem(sys.modules, "truststore", None)  # import truststore -> ImportError
    assert updater._dist_ssl_context() is None


def test_dist_ssl_context_uses_truststore_when_available(monkeypatch):
    import ssl
    import sys
    import types
    from firekeep_client import updater
    sentinel = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    fake = types.SimpleNamespace(SSLContext=lambda proto: sentinel)
    monkeypatch.setitem(sys.modules, "truststore", fake)
    assert updater._dist_ssl_context() is sentinel
