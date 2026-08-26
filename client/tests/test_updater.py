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


# --- release-signing verification (docs/RELEASE-SIGNING.md) --------------------
#
# The trust chain under test: the client PINS a public key (signing.PINNED_PUBLIC_KEY,
# from the previously installed version); the release host serves SHA256SUMS +
# SHA256SUMS.minisig; verification anchors the bootstrap hash to the SIGNED sums.
# verify-if-present: absence warns (require_signed=false, today's default), an INVALID
# signature is fatal regardless — invalid is tampering evidence, absence is history.

from firekeep_client import signing  # noqa: E402  (grouped with the tests that use it)


def _release(version="9.9.9", *, sh=b"#!/bin/sh\nreal\n", ps1=b"# ps\nreal\n"):
    """A minimal signed release: keypair, SHA256SUMS covering both bootstraps, minisig."""
    pub_text, sec_text = signing.generate_keypair()
    sums = (
        f"{hashlib.sha256(sh).hexdigest()}  install.sh\n"
        f"{hashlib.sha256(ps1).hexdigest()}  install.ps1\n"
        f"{'ab' * 32}  firekeep_client-{version}-py3-none-any.whl\n"
    ).encode()
    sig = signing.sign(
        sums, sec_text,
        trusted_comment=f"timestamp:1\tfile:SHA256SUMS\tversion:{version}\thashed",
    ).encode()
    return {"pub": pub_text, "sums": sums, "sig": sig, "sh": sh, "ps1": ps1}


def _serve(monkeypatch, rel, *, sig_missing=False):
    """Route updater._read_url to the fake release host."""
    def _read(url, timeout):
        if url.endswith("SHA256SUMS.minisig"):
            if sig_missing:
                import urllib.error
                raise urllib.error.URLError("404")
            return rel["sig"]
        if url.endswith("SHA256SUMS"):
            return rel["sums"]
        raise AssertionError(f"unexpected fetch: {url}")
    monkeypatch.setattr(updater, "_read_url", _read)


def _pin(monkeypatch, pub_text):
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEY", pub_text)


def test_no_pinned_key_skips_silently_and_touches_no_network(monkeypatch):
    """Pre-mint builds: nothing to verify against, nothing to nag about — and no
    fetch, so an unreachable host cannot break what verification never needed."""
    _pin(monkeypatch, "")
    monkeypatch.setattr(updater, "_read_url",
                        lambda url, timeout: (_ for _ in ()).throw(AssertionError(url)))
    out = updater.fetch_signed_sums("http://gl/rel", "9.9.9", require_signed=False)
    assert out == updater.SignedSums(text=None, verified=False, warning=None)


def test_require_signed_with_no_pinned_key_fails_loud(monkeypatch):
    _pin(monkeypatch, "")
    with pytest.raises(updater.UpdateError, match="pins no release signing key"):
        updater.fetch_signed_sums("http://gl/rel", "9.9.9", require_signed=True)


def test_valid_signature_verifies_and_returns_the_sums(monkeypatch):
    rel = _release()
    _pin(monkeypatch, rel["pub"])
    _serve(monkeypatch, rel)
    out = updater.fetch_signed_sums("http://gl/rel", "9.9.9", require_signed=False)
    assert out.verified is True
    assert out.warning is None
    assert out.text == rel["sums"].decode()


def test_missing_signature_is_a_warning_not_an_error(monkeypatch):
    """Backward compatibility: every release predating signing has no .minisig, and
    `firekeep update --to <old>` must keep working. A clear one-line warning, never silence."""
    rel = _release()
    _pin(monkeypatch, rel["pub"])
    _serve(monkeypatch, rel, sig_missing=True)
    out = updater.fetch_signed_sums("http://gl/rel", "9.9.9", require_signed=False)
    assert out.verified is False
    assert "not signed" in out.warning
    assert out.text == rel["sums"].decode()  # sums still usable for the checksum layer


def test_missing_signature_under_require_signed_is_fatal(monkeypatch):
    rel = _release()
    _pin(monkeypatch, rel["pub"])
    _serve(monkeypatch, rel, sig_missing=True)
    with pytest.raises(updater.UpdateError, match="not signed"):
        updater.fetch_signed_sums("http://gl/rel", "9.9.9", require_signed=True)


def test_an_invalid_signature_is_fatal_even_without_require_signed(monkeypatch):
    """The flag governs ABSENCE only. A signature that exists and fails to verify is
    tampering evidence; letting `require_signed=false` wave it through would make the
    default configuration ignore the exact attack signing exists to catch."""
    rel = _release()
    _pin(monkeypatch, rel["pub"])
    rel["sums"] += b"0" * 64 + b"  injected.whl\n"  # host swaps the sums, keeps the sig
    _serve(monkeypatch, rel)
    with pytest.raises(updater.UpdateError, match="SIGNATURE VERIFICATION FAILED"):
        updater.fetch_signed_sums("http://gl/rel", "9.9.9", require_signed=False)


def test_a_signature_for_a_different_release_is_refused(monkeypatch):
    """Replay: serving release A's (validly signed) sums under release B's directory.
    The trusted comment's version token, covered by the global signature, closes it."""
    rel = _release(version="1.0.0")
    _pin(monkeypatch, rel["pub"])
    _serve(monkeypatch, rel)
    with pytest.raises(updater.UpdateError, match="DIFFERENT release"):
        updater.fetch_signed_sums("http://gl/rel", "9.9.9", require_signed=False)


def test_unfetchable_sums_warns_or_fails_by_flag(monkeypatch):
    rel = _release()
    _pin(monkeypatch, rel["pub"])

    def _boom(url, timeout):
        import urllib.error
        raise urllib.error.URLError("conn refused")

    monkeypatch.setattr(updater, "_read_url", _boom)
    out = updater.fetch_signed_sums("http://gl/rel", "9.9.9", require_signed=False)
    assert out.verified is False and "skipping signature verification" in out.warning
    with pytest.raises(updater.UpdateError, match="signature verification"):
        updater.fetch_signed_sums("http://gl/rel", "9.9.9", require_signed=True)


def test_bootstrap_sha256_prefers_the_signed_entry(monkeypatch):
    """When the signature verified, the bootstrap hash `firekeep update` enforces comes
    from the SIGNED sums — and the unsigned manifest must agree with it."""
    rel = _release()
    m = updater.Manifest(
        "9.9.9",
        bootstrap_sha256=hashlib.sha256(rel["sh"]).hexdigest(),
        bootstrap_ps1_sha256=hashlib.sha256(rel["ps1"]).hexdigest(),
    )
    signed = updater.SignedSums(rel["sums"].decode(), True, None)
    assert updater.bootstrap_sha256(m, signed, windows=False) == hashlib.sha256(rel["sh"]).hexdigest()
    assert updater.bootstrap_sha256(m, signed, windows=True) == hashlib.sha256(rel["ps1"]).hexdigest()


def test_bootstrap_sha256_refuses_a_manifest_that_disagrees_with_the_signed_sums():
    """The precise attack signing exists for: the host serves a tampered latest.json
    whose bootstrap hash matches a tampered script. The signed sums say otherwise."""
    rel = _release()
    m = updater.Manifest("9.9.9", bootstrap_sha256="66" * 32, bootstrap_ps1_sha256="77" * 32)
    signed = updater.SignedSums(rel["sums"].decode(), True, None)
    with pytest.raises(updater.UpdateError, match="does not match the SIGNED"):
        updater.bootstrap_sha256(m, signed, windows=False)


def test_bootstrap_sha256_unverified_falls_back_to_the_manifest():
    m = updater.Manifest("9.9.9", bootstrap_sha256="cd" * 32, bootstrap_ps1_sha256="ef" * 32)
    unverified = updater.SignedSums(None, False, "release 9.9.9 is not signed")
    assert updater.bootstrap_sha256(m, unverified, windows=False) == "cd" * 32


def test_signed_sums_missing_the_bootstrap_entry_is_malformed():
    signed = updater.SignedSums("ab" * 32 + "  firekeep_client-9.9.9-py3-none-any.whl\n", True, None)
    m = updater.Manifest("9.9.9", bootstrap_sha256="cd" * 32, bootstrap_ps1_sha256="ef" * 32)
    with pytest.raises(updater.UpdateError, match="no entry for install.sh"):
        updater.bootstrap_sha256(m, signed, windows=False)


@pytest.mark.parametrize("raw,expected", [
    ("", False), ("false", False), ("no", False), ("0", False),
    ("true", True), ("1", True), ("yes", True), ("on", True),
])
def test_require_signed_config_parsing(raw, expected):
    text = "[dist]\nbase_url = http://gl/rel\n"
    if raw:
        text += f"require_signed = {raw}\n"
    assert updater.require_signed(_cfg(text)) is expected


def test_require_signed_garbage_fails_loud():
    """A mistyped security flag silently read as false would be worse than either
    setting; `require_signed = ture` must stop the update, not weaken it."""
    with pytest.raises(updater.UpdateError, match="not a boolean"):
        updater.require_signed(_cfg("[dist]\nrequire_signed = ture\n"))


def test_require_signed_defaults_false_without_a_dist_section():
    assert updater.require_signed(_cfg("[identity]\nagent_id = t\n")) is False


def test_dist_ssl_context_none_without_truststore(monkeypatch):
    import sys
    from firekeep_client import updater
    monkeypatch.setitem(sys.modules, "truststore", None)  # import truststore -> ImportError
    assert updater.dist_ssl_context() is None


def test_dist_ssl_context_uses_truststore_when_available(monkeypatch):
    import ssl
    import sys
    import types
    from firekeep_client import updater
    sentinel = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    fake = types.SimpleNamespace(SSLContext=lambda proto: sentinel)
    monkeypatch.setitem(sys.modules, "truststore", fake)
    assert updater.dist_ssl_context() is sentinel
