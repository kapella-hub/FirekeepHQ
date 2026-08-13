"""Minisign-compatible Ed25519 signing/verification for the release channel.

Stdlib-only, deliberately. The client kit's import boundary (tests/test_import_boundary.py)
keeps every module except shim.py free of third-party imports, so `cryptography`/`pynacl`
were never candidates — this module implements RFC 8032 Ed25519 in pure Python instead.
That is defensible here and only here:

- VERIFICATION handles exclusively public inputs (a public key, a published signature,
  a published checksum file), so the classic argument against hand-rolled crypto —
  secret-dependent side channels — does not apply to the client-side path at all.
- SIGNING (the bottom of this file) runs only in release tooling (client/scripts/
  make_release.py, CI), never on a customer machine and never in a hook. One-shot CI
  signing has no meaningful timing observer.
- Correctness is pinned by RFC 8032 test vectors in client/tests/test_signing.py,
  plus sign->verify round-trips through the minisign container format.

The FORMAT is minisign (https://jedisct1.github.io/minisign/), not a private one, so
"you can check this with public tools" holds: `minisign -Vm SHA256SUMS -P <key>`
verifies exactly what this module produces, and a keypair generated with
`minisign -G -W` signs releases this module verifies. Layouts:

  public key file   : untrusted comment line + base64("Ed" || key_id[8] || pubkey[32])
  signature file    : untrusted comment line
                      base64(sig_alg[2] || key_id[8] || signature[64])
                      trusted comment line
                      base64(global_signature[64])
  secret key file   : untrusted comment line + base64(
                      "Ed" || "Sc" || "B2" || kdf_salt[32] || kdf_opslimit[8] ||
                      kdf_memlimit[8] || (key_id[8] || sk[64] || checksum[32]))

sig_alg "ED" signs Blake2b-512(file) (minisign's default, what we emit); legacy "Ed"
signs the raw file and is still accepted on verify. The global signature covers
(signature || trusted_comment), so the trusted comment — which for releases carries
`version:<X.Y.Z>` — cannot be swapped without re-signing.

Only UNENCRYPTED secret keys (kdf limits zero, as `minisign -G -W` writes) are
accepted: the CI secret store is the encryption layer there, and shipping an scrypt
implementation for a password prompt no CI can answer would be dead weight.

`PINNED_PUBLIC_KEY` below is the trust anchor for `firekeep update`. It ships EMPTY
until the operator mints the release signing identity — see docs/RELEASE-SIGNING.md
for keygen, CI wiring, rotation, and the compromise procedure. While it is empty the
updater has nothing to verify against and says nothing; once a build pins a key, every
update verifies the fetched SHA256SUMS against it whenever a signature is published.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets as _secrets
import struct
from dataclasses import dataclass

# The minisign public key (the base64 line, or the whole two-line file) that release
# SHA256SUMS files must be signed with. Minted 2026-08-12 per docs/RELEASE-SIGNING.md
# (key ID 7D6D83D1240D4A61; the CI secret FIREKEEP_SIGNING_KEY holds the private
# half). The updater reads it via the module attribute so tests (and a rotation
# release) can see exactly one source of truth.
PINNED_PUBLIC_KEY = "RWRhSg0k0YNtfVG2DYqWZCyZaY9XRylvhxNdX3k0dseC0xoSSxnvrdh/"


class SignatureError(Exception):
    """Signature parsing or verification failed. On the update path this is always
    fatal — an invalid signature is tampering evidence, not absence."""


class VerifyUnavailable(SignatureError):
    """This Python build cannot verify (blake2b stripped, e.g. some FIPS builds).
    Distinct from SignatureError so the updater can degrade to warn-if-unverifiable
    instead of treating a crippled interpreter as a compromised release host."""


# --- Ed25519 (RFC 8032), verify + sign, pure Python --------------------------

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _sha512(*parts: bytes) -> bytes:
    h = hashlib.sha512()
    for part in parts:
        h.update(part)
    return h.digest()


def _blake2b512(data: bytes) -> bytes:
    try:
        return hashlib.blake2b(data, digest_size=64).digest()
    except (AttributeError, ValueError) as exc:  # blake2 stripped from this build
        raise VerifyUnavailable(
            "this Python build lacks blake2b, which minisign's prehashed signatures require"
        ) from exc


# Points are extended homogeneous coordinates (X, Y, Z, T): x = X/Z, y = Y/Z, T = XY/Z.
_IDENTITY = (0, 1, 1, 0)


def _pt_add(p, q):
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = 2 * t1 * t2 * _D % _P
    d = 2 * z1 * z2 % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _pt_mul(s: int, p):
    q = _IDENTITY
    while s > 0:
        if s & 1:
            q = _pt_add(q, p)
        p = _pt_add(p, p)
        s >>= 1
    return q


def _pt_equal(p, q) -> bool:
    x1, y1, z1, _ = p
    x2, y2, z2, _ = q
    return (x1 * z2 - x2 * z1) % _P == 0 and (y1 * z2 - y2 * z1) % _P == 0


def _recover_x(y: int, sign: int):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_G_Y = 4 * pow(5, _P - 2, _P) % _P
_G_X = _recover_x(_G_Y, 0)
_G = (_G_X, _G_Y, 1, _G_X * _G_Y % _P)


def _pt_compress(p) -> bytes:
    x, y, z, _ = p
    zinv = pow(z, _P - 2, _P)
    x, y = x * zinv % _P, y * zinv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _pt_decompress(b: bytes):
    if len(b) != 32:
        return None
    y = int.from_bytes(b, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _clamp(k: bytes) -> int:
    a = int.from_bytes(k, "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a


def _ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a = _pt_decompress(public_key)
    if a is None:
        return False
    r_bytes, s_bytes = signature[:32], signature[32:]
    r = _pt_decompress(r_bytes)
    if r is None:
        return False
    s = int.from_bytes(s_bytes, "little")
    if s >= _L:  # non-canonical s: reject (malleability)
        return False
    h = int.from_bytes(_sha512(r_bytes, public_key, message), "little") % _L
    return _pt_equal(_pt_mul(s, _G), _pt_add(r, _pt_mul(h, a)))


def _ed25519_public_key(seed: bytes) -> bytes:
    return _pt_compress(_pt_mul(_clamp(_sha512(seed)[:32]), _G))


def _ed25519_sign(seed: bytes, message: bytes) -> bytes:
    h = _sha512(seed)
    a = _clamp(h[:32])
    prefix = h[32:]
    a_bytes = _pt_compress(_pt_mul(a, _G))
    r = int.from_bytes(_sha512(prefix, message), "little") % _L
    r_bytes = _pt_compress(_pt_mul(r, _G))
    k = int.from_bytes(_sha512(r_bytes, a_bytes, message), "little") % _L
    s = (r + k * a) % _L
    return r_bytes + s.to_bytes(32, "little")


# --- minisign container format ------------------------------------------------

_ALG_LEGACY = b"Ed"   # signature over the raw file
_ALG_PREHASH = b"ED"  # signature over Blake2b-512(file) — minisign default, what we emit
_KDF_ALG = b"Sc"
_CHK_ALG = b"B2"
_UNTRUSTED_PREFIX = "untrusted comment:"
_TRUSTED_PREFIX = "trusted comment:"


@dataclass(frozen=True)
class PublicKey:
    key_id: bytes  # 8 bytes, opaque
    key: bytes     # 32 bytes


@dataclass(frozen=True)
class Signature:
    untrusted_comment: str
    sig_alg: bytes
    key_id: bytes
    signature: bytes
    trusted_comment: str
    global_signature: bytes


@dataclass(frozen=True)
class SecretKey:
    key_id: bytes
    seed: bytes        # 32 bytes — the actual signing secret
    public_key: bytes  # 32 bytes


def _b64(line: str, what: str) -> bytes:
    # binascii.Error (what standard_b64decode raises) is a ValueError subclass.
    try:
        return base64.standard_b64decode(line.strip())
    except ValueError as exc:
        raise SignatureError(f"{what}: not valid base64") from exc


def key_id_hex(key_id: bytes) -> str:
    """Render a key id the way minisign prints it (little-endian uint64, upper hex)."""
    return format(struct.unpack("<Q", key_id)[0], "X")


def parse_public_key(text: str) -> PublicKey:
    """Accept either the bare base64 line or a full minisign public key file."""
    for line in text.splitlines() or [text]:
        line = line.strip()
        if not line or line.lower().startswith(_UNTRUSTED_PREFIX):
            continue
        blob = _b64(line, "public key")
        if len(blob) != 42 or blob[:2] != _ALG_LEGACY:
            raise SignatureError("public key: not a minisign Ed25519 public key")
        return PublicKey(key_id=blob[2:10], key=blob[10:42])
    raise SignatureError("public key: empty")


def parse_signature(text: str) -> Signature:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) != 4:
        raise SignatureError("signature file: expected 4 lines (minisign format)")
    if not lines[0].lower().startswith(_UNTRUSTED_PREFIX):
        raise SignatureError("signature file: missing untrusted comment line")
    if not lines[2].lower().startswith(_TRUSTED_PREFIX):
        raise SignatureError("signature file: missing trusted comment line")
    blob = _b64(lines[1], "signature")
    if len(blob) != 74:
        raise SignatureError("signature file: signature blob is not 74 bytes")
    sig_alg = blob[:2]
    if sig_alg not in (_ALG_LEGACY, _ALG_PREHASH):
        raise SignatureError(f"signature file: unknown signature algorithm {sig_alg!r}")
    global_sig = _b64(lines[3], "global signature")
    if len(global_sig) != 64:
        raise SignatureError("signature file: global signature is not 64 bytes")
    # The comment content is everything after the prefix, minus minisign's one
    # separator space — this exact string is what the global signature covers.
    trusted = lines[2][len(_TRUSTED_PREFIX):]
    if trusted.startswith(" "):
        trusted = trusted[1:]
    return Signature(
        untrusted_comment=lines[0][len(_UNTRUSTED_PREFIX):].strip(),
        sig_alg=sig_alg,
        key_id=blob[2:10],
        signature=blob[10:74],
        trusted_comment=trusted,
        global_signature=global_sig,
    )


def verify(data: bytes, signature_text: str, public_key_text: str) -> str:
    """Verify a minisign signature over `data`. Returns the trusted comment on
    success; raises SignatureError (or VerifyUnavailable) on any failure.

    Checks all three bindings: the key id matches the pinned key, the Ed25519
    signature covers the data (prehashed or legacy), and the global signature
    covers (signature || trusted comment) so the comment cannot be swapped."""
    public = parse_public_key(public_key_text)
    sig = parse_signature(signature_text)
    if sig.key_id != public.key_id:
        raise SignatureError(
            f"signature key id {key_id_hex(sig.key_id)} does not match the pinned "
            f"public key {key_id_hex(public.key_id)}"
        )
    payload = _blake2b512(data) if sig.sig_alg == _ALG_PREHASH else data
    if not _ed25519_verify(public.key, payload, sig.signature):
        raise SignatureError("Ed25519 signature does not verify against the pinned key")
    if not _ed25519_verify(
        public.key, sig.signature + sig.trusted_comment.encode("utf-8"), sig.global_signature
    ):
        raise SignatureError("global signature (trusted comment binding) does not verify")
    return sig.trusted_comment


def trusted_comment_version(trusted_comment: str) -> "str | None":
    """The `version:<X.Y.Z>` token our release tooling embeds, or None when absent
    (foreign/older signatures)."""
    m = re.search(r"(?:^|[\t ])version:(\S+)", trusted_comment)
    return m.group(1) if m else None


# --- release tooling (CI only: make_release.py / keygen) ----------------------
# Nothing below runs on a customer machine. It lives in this module so the format
# is defined exactly once, next to the verifier that must agree with it.

def public_key_text(key_id: bytes, public_key: bytes) -> str:
    blob = _ALG_LEGACY + key_id + public_key
    return (
        f"{_UNTRUSTED_PREFIX} minisign public key {key_id_hex(key_id)}\n"
        f"{base64.standard_b64encode(blob).decode('ascii')}\n"
    )


def parse_secret_key(text: str) -> SecretKey:
    """Parse an UNENCRYPTED minisign secret key (what `minisign -G -W` and our keygen
    write). Password-protected keys are refused with instructions, not mis-parsed."""
    blob = None
    for line in text.splitlines() or [text]:
        line = line.strip()
        if not line or line.lower().startswith(_UNTRUSTED_PREFIX):
            continue
        blob = _b64(line, "secret key")
        break
    if blob is None:
        raise SignatureError("secret key: empty")
    if len(blob) != 158:
        raise SignatureError("secret key: not a minisign secret key (wrong length)")
    if blob[:2] != _ALG_LEGACY or blob[2:4] != _KDF_ALG or blob[4:6] != _CHK_ALG:
        raise SignatureError("secret key: not a minisign Ed25519/scrypt/Blake2 secret key")
    opslimit = struct.unpack("<Q", blob[38:46])[0]
    memlimit = struct.unpack("<Q", blob[46:54])[0]
    if opslimit or memlimit:
        raise SignatureError(
            "secret key is password-protected; CI signing needs an unencrypted key "
            "(generate with `minisign -G -W` or client/scripts/generate_signing_key.py "
            "— the CI secret store is the encryption layer there)"
        )
    keynum = blob[54:158]
    key_id, sk, checksum = keynum[:8], keynum[8:72], keynum[72:104]
    expected = hashlib.blake2b(_ALG_LEGACY + key_id + sk, digest_size=32).digest()
    if checksum != expected:
        raise SignatureError("secret key: checksum mismatch (corrupted key material)")
    seed, public = sk[:32], sk[32:64]
    if _ed25519_public_key(seed) != public:
        raise SignatureError("secret key: embedded public key does not match the seed")
    return SecretKey(key_id=key_id, seed=seed, public_key=public)


def sign(
    data: bytes,
    secret_key_text: str,
    *,
    trusted_comment: str,
    untrusted_comment: str = "signature from firekeep release signing",
) -> str:
    """Produce a minisign-format detached signature (prehashed 'ED' mode) over `data`."""
    key = parse_secret_key(secret_key_text)
    file_sig = _ed25519_sign(key.seed, _blake2b512(data))
    global_sig = _ed25519_sign(key.seed, file_sig + trusted_comment.encode("utf-8"))
    blob = _ALG_PREHASH + key.key_id + file_sig
    return (
        f"{_UNTRUSTED_PREFIX} {untrusted_comment}\n"
        f"{base64.standard_b64encode(blob).decode('ascii')}\n"
        f"{_TRUSTED_PREFIX} {trusted_comment}\n"
        f"{base64.standard_b64encode(global_sig).decode('ascii')}\n"
    )


def generate_keypair() -> "tuple[str, str]":
    """Mint a fresh signing identity. Returns (public_key_file_text,
    secret_key_file_text) in minisign's formats — the secret UNENCRYPTED, for a
    CI secret store (keep the offline original wherever the runbook says)."""
    seed = _secrets.token_bytes(32)
    key_id = _secrets.token_bytes(8)
    public = _ed25519_public_key(seed)
    sk = seed + public
    checksum = hashlib.blake2b(_ALG_LEGACY + key_id + sk, digest_size=32).digest()
    blob = (
        _ALG_LEGACY + _KDF_ALG + _CHK_ALG
        + _secrets.token_bytes(32)          # kdf salt: present in the layout, unused at limits 0
        + struct.pack("<Q", 0) + struct.pack("<Q", 0)
        + key_id + sk + checksum
    )
    secret_text = (
        f"{_UNTRUSTED_PREFIX} minisign unencrypted secret key {key_id_hex(key_id)}\n"
        f"{base64.standard_b64encode(blob).decode('ascii')}\n"
    )
    return public_key_text(key_id, public), secret_text
