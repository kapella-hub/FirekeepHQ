"""firekeep_client.signing — the pure-stdlib minisign/Ed25519 layer under release signing.

Hand-rolled crypto is only defensible with known-answer tests: the RFC 8032 vectors
below pin the Ed25519 arithmetic to the specification, and the container tests pin the
minisign format (what `minisign -Vm` itself would check) — key-id binding, the file
signature, and the global signature over (signature || trusted comment)."""
import base64

import pytest

from firekeep_client import signing

# --- RFC 8032 §7.1 known-answer vectors --------------------------------------

RFC_VECTORS = [
    # (seed, public key, message, signature) — TEST 1, TEST 2, TEST 3
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
        "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]


@pytest.mark.parametrize("seed,pub,msg,sig", RFC_VECTORS)
def test_rfc8032_public_key_derivation(seed, pub, msg, sig):
    assert signing._ed25519_public_key(bytes.fromhex(seed)) == bytes.fromhex(pub)


@pytest.mark.parametrize("seed,pub,msg,sig", RFC_VECTORS)
def test_rfc8032_sign(seed, pub, msg, sig):
    assert signing._ed25519_sign(bytes.fromhex(seed), bytes.fromhex(msg)) == bytes.fromhex(sig)


@pytest.mark.parametrize("seed,pub,msg,sig", RFC_VECTORS)
def test_rfc8032_verify(seed, pub, msg, sig):
    assert signing._ed25519_verify(bytes.fromhex(pub), bytes.fromhex(msg), bytes.fromhex(sig))


def test_verify_rejects_a_tampered_message():
    _, pub, _, sig = RFC_VECTORS[2]
    assert not signing._ed25519_verify(bytes.fromhex(pub), b"af82-not", bytes.fromhex(sig))


def test_verify_rejects_a_tampered_signature():
    _, pub, msg, sig = RFC_VECTORS[2]
    bad = bytearray(bytes.fromhex(sig))
    bad[0] ^= 0x01
    assert not signing._ed25519_verify(bytes.fromhex(pub), bytes.fromhex(msg), bytes(bad))


def test_verify_rejects_a_non_canonical_s():
    """s >= L is the classic malleability lever: (R, s + L) verifies in a naive
    implementation. Reject it outright."""
    _, pub, msg, sig = RFC_VECTORS[2]
    raw = bytes.fromhex(sig)
    s = int.from_bytes(raw[32:], "little") + signing._L
    forged = raw[:32] + s.to_bytes(32, "little")
    assert not signing._ed25519_verify(bytes.fromhex(pub), bytes.fromhex(msg), forged)


# --- minisign container: keygen -> sign -> verify ------------------------------


@pytest.fixture(scope="module")
def keypair():
    return signing.generate_keypair()


@pytest.fixture(scope="module")
def signed(keypair):
    pub_text, sec_text = keypair
    data = b"ab" * 32 + b"  firekeep_client-1.2.3-py3-none-any.whl\n"
    sig_text = signing.sign(
        data, sec_text,
        trusted_comment="timestamp:1712345678\tfile:SHA256SUMS\tversion:1.2.3\thashed",
    )
    return {"pub": pub_text, "sec": sec_text, "data": data, "sig": sig_text}


def test_roundtrip_verifies_and_returns_the_trusted_comment(signed):
    trusted = signing.verify(signed["data"], signed["sig"], signed["pub"])
    assert signing.trusted_comment_version(trusted) == "1.2.3"


def test_signature_file_has_the_minisign_shape(signed):
    """4 lines: untrusted comment, 74-byte blob (alg 'ED' + key id + sig), trusted
    comment, 64-byte global signature. This is what makes `minisign -Vm` interop real."""
    lines = signed["sig"].splitlines()
    assert len(lines) == 4
    assert lines[0].startswith("untrusted comment:")
    assert lines[2].startswith("trusted comment:")
    blob = base64.standard_b64decode(lines[1])
    assert len(blob) == 74 and blob[:2] == b"ED"
    assert len(base64.standard_b64decode(lines[3])) == 64


def test_public_key_accepts_bare_base64_and_full_file(signed):
    full = signed["pub"]
    bare = full.splitlines()[1]
    assert signing.parse_public_key(full) == signing.parse_public_key(bare)
    signing.verify(signed["data"], signed["sig"], bare)


def test_tampered_data_fails(signed):
    with pytest.raises(signing.SignatureError, match="does not verify"):
        signing.verify(signed["data"] + b"!", signed["sig"], signed["pub"])


def test_tampered_trusted_comment_fails_the_global_signature(signed):
    """The version binding rides in the trusted comment; without the global signature
    a host could re-label release A's signature as release B's."""
    bad = signed["sig"].replace("version:1.2.3", "version:9.9.9")
    with pytest.raises(signing.SignatureError, match="global signature"):
        signing.verify(signed["data"], bad, signed["pub"])


def test_wrong_key_is_refused_by_key_id_before_any_math(signed):
    other_pub, _ = signing.generate_keypair()
    with pytest.raises(signing.SignatureError, match="key id"):
        signing.verify(signed["data"], signed["sig"], other_pub)


def test_legacy_unprehashed_signatures_still_verify(signed):
    """'Ed' (raw-file) mode is minisign's legacy format; verify-side support means a
    key holder signing with old tooling doesn't strand every client."""
    key = signing.parse_secret_key(signed["sec"])
    file_sig = signing._ed25519_sign(key.seed, signed["data"])
    global_sig = signing._ed25519_sign(key.seed, file_sig + b"legacy")
    sig_text = (
        "untrusted comment: legacy\n"
        + base64.standard_b64encode(b"Ed" + key.key_id + file_sig).decode()
        + "\ntrusted comment: legacy\n"
        + base64.standard_b64encode(global_sig).decode()
        + "\n"
    )
    assert signing.verify(signed["data"], sig_text, signed["pub"]) == "legacy"


@pytest.mark.parametrize("mangle", [
    lambda s: "",                                          # empty
    lambda s: s.replace("trusted comment:", "trusted x:"),  # missing trusted line
    lambda s: "\n".join(s.splitlines()[:2]) + "\n",         # truncated
    lambda s: s.replace(s.splitlines()[1], "!!!not-base64!!!"),
])
def test_malformed_signature_files_raise_not_crash(signed, mangle):
    with pytest.raises(signing.SignatureError):
        signing.verify(signed["data"], mangle(signed["sig"]), signed["pub"])


def test_garbage_public_key_is_a_signature_error(signed):
    with pytest.raises(signing.SignatureError):
        signing.verify(signed["data"], signed["sig"], "RWQnotakey")


# --- secret keys ---------------------------------------------------------------


def test_secret_key_roundtrip_and_checksum(keypair):
    pub_text, sec_text = keypair
    key = signing.parse_secret_key(sec_text)
    assert signing._ed25519_public_key(key.seed) == key.public_key
    assert signing.parse_public_key(pub_text).key == key.public_key
    assert signing.parse_public_key(pub_text).key_id == key.key_id


def test_corrupted_secret_key_fails_its_checksum(keypair):
    _, sec_text = keypair
    line = sec_text.splitlines()[1]
    blob = bytearray(base64.standard_b64decode(line))
    blob[70] ^= 0xFF  # inside keynum_sk
    bad = sec_text.replace(line, base64.standard_b64encode(bytes(blob)).decode())
    with pytest.raises(signing.SignatureError, match="checksum"):
        signing.parse_secret_key(bad)


def test_password_protected_secret_keys_are_refused_with_instructions(keypair):
    """CI cannot answer an scrypt password prompt; mis-parsing an encrypted key as
    key material would sign with garbage. Refuse, and say what to do instead."""
    _, sec_text = keypair
    line = sec_text.splitlines()[1]
    blob = bytearray(base64.standard_b64decode(line))
    blob[38] = 1  # nonzero kdf_opslimit -> encrypted
    bad = sec_text.replace(line, base64.standard_b64encode(bytes(blob)).decode())
    with pytest.raises(signing.SignatureError, match="password-protected"):
        signing.parse_secret_key(bad)


def test_generate_keypair_mints_distinct_identities():
    a_pub, _ = signing.generate_keypair()
    b_pub, _ = signing.generate_keypair()
    assert signing.parse_public_key(a_pub).key_id != signing.parse_public_key(b_pub).key_id


# --- keygen script: the secret must never be world-readable, even briefly --------


def _keygen(tmp_path):
    import importlib.util
    script = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "scripts" / "generate_signing_key.py"
    )
    spec = importlib.util.spec_from_file_location("generate_signing_key", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_keygen_applies_the_mode_at_open_never_chmod_after(tmp_path):
    """LOW finding: write_text-then-chmod leaves a window where a permissive umask
    makes the signing secret world-readable. Two halves:

    - shape: the script must open with O_EXCL and a 0o600 mode and must not
      contain a chmod at all (a chmod is only ever needed when the create was
      permissive — its absence IS the absence of the window);
    - behaviour (POSIX): the resulting file is 0600 and the content round-trips.
    """
    import os
    import stat
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "scripts" / "generate_signing_key.py"
    text = script.read_text(encoding="utf-8")
    assert "O_EXCL" in text and "O_CREAT" in text and "0o600" in text
    assert ".chmod(" not in text and "os.chmod" not in text, (
        "a chmod call after the write reintroduces the world-readable window the "
        "open-mode fix removed"
    )

    mod = _keygen(tmp_path)
    old_umask = os.umask(0o022)
    try:
        assert mod.main(["generate_signing_key.py", str(tmp_path)]) == 0
    finally:
        os.umask(old_umask)
    key = tmp_path / "firekeep-signing.key"
    from firekeep_client import signing as _signing
    _signing.parse_secret_key(key.read_text(encoding="utf-8"))  # content intact
    if os.name != "nt":
        assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_keygen_refuses_to_overwrite_an_existing_secret(tmp_path):
    mod = _keygen(tmp_path)
    assert mod.main(["generate_signing_key.py", str(tmp_path)]) == 0
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        mod.main(["generate_signing_key.py", str(tmp_path)])


# --- the pinned-key constant ----------------------------------------------------


def test_pinned_public_key_is_either_empty_or_parseable():
    """Ships empty until the operator mints keys (docs/RELEASE-SIGNING.md). Whatever
    lands there later must parse, or every update would fail at verification setup."""
    pinned = signing.PINNED_PUBLIC_KEY.strip()
    if pinned:
        signing.parse_public_key(pinned)


def test_trusted_comment_version_token():
    assert signing.trusted_comment_version("timestamp:1\tfile:SHA256SUMS\tversion:0.1.34\thashed") == "0.1.34"
    assert signing.trusted_comment_version("timestamp:1\tfile:SHA256SUMS") is None
    # 'version:' must be token-anchored, not a substring of another token
    assert signing.trusted_comment_version("myversion:9.9.9") is None
