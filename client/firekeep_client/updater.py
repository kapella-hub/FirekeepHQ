"""Release-manifest fetch, version compare, and verified download for `firekeep update`.

Stdlib-only (import boundary). Optional non-stdlib import: truststore (guarded) — OS-trust
for RELEASE-HOST fetches only. The GitLab host is NEVER hardcoded: the bootstrap knows the
URL it was fetched from and records it as [dist] base_url in ~/.firekeep/config, which is the
only place this module learns it from.
"""
from __future__ import annotations

import hashlib
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from firekeep_client.transport import TransportError, get_json


def _dist_ssl_context() -> "ssl.SSLContext | None":
    """OS-trust SSL context for release-host fetches ONLY (GitHub Pages / GitLab sit
    behind corporate TLS interception; the managed CPython's default bundle lacks the
    interception CA). Scoped on purpose — truststore.inject_into_ssl() would replace
    ssl.SSLContext process-wide and widen office ca_path pinning to 'pin OR OS store'
    (truststore treats loaded CAs as additional anchors). None -> caller uses the
    stdlib default context (truststore not installed, or context creation failed)."""
    try:
        import truststore
    except ImportError:
        return None
    try:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001 — never let trust-store probing kill an update
        return None


class UpdateError(Exception):
    """Any failure on the update path. Callers print it and exit non-zero — never a raw
    traceback at a teammate."""


@dataclass(frozen=True)
class Manifest:
    version: str
    bootstrap_sha256: str
    bootstrap_ps1_sha256: str

    def bootstrap_hash_for(self, *, windows: bool) -> str:
        return self.bootstrap_ps1_sha256 if windows else self.bootstrap_sha256


# `wheel_url` and `sha256` are deliberately NOT fields here. They had no consumer: install.sh
# reconstructs the wheel URL itself from a versioned BASE (it must — a pinned `--to` install
# has no manifest for that version), and the wheel's integrity comes from the versioned
# SHA256SUMS the bootstrap already parses. A `sha256` field that looks like the wheel is
# verified while nothing reads it is worse than no field at all — that is how C2 hid.
_MANIFEST_FIELDS = ("version", "bootstrap_sha256", "bootstrap_ps1_sha256")


def dist_base(cfg) -> str:
    base = cfg.get("dist", "base_url", fallback="").strip() if cfg.has_section("dist") else ""
    if not base:
        raise UpdateError(
            "no [dist] base_url in ~/.firekeep/config — this client was installed from a "
            "checkout, not from a release. Re-run the bootstrap installer to enable updates."
        )
    return base.rstrip("/")


def parse_version(text: str) -> tuple[int, int, int]:
    parts = text.strip().split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise UpdateError(f"unparseable version {text!r} (want MAJOR.MINOR.PATCH)")
    return tuple(int(p) for p in parts)  # type: ignore[return-value]


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def fetch_manifest(base: str, *, timeout: float = 10.0) -> Manifest:
    # base is version-agnostic; the moving pointer lives under /latest/, not at base itself
    # — that is what lets latest.json actually advance instead of describing the release it
    # shipped inside forever.
    url = f"{base.rstrip('/')}/latest/latest.json"
    try:
        ctx = _dist_ssl_context()
        data = get_json(url, headers={}, timeout=timeout,
                        verify=ctx if ctx is not None else True)
    except (TransportError, OSError) as exc:
        raise UpdateError(f"cannot reach the release manifest at {url}: {exc}") from exc
    if not isinstance(data, dict) or not all(
        isinstance(data.get(k), str) for k in _MANIFEST_FIELDS
    ):
        # A manifest missing the bootstrap hashes must fail LOUDLY. Defaulting them to ""
        # would hand `cmd_update` an empty checksum and take us straight back to executing
        # an unverified script.
        raise UpdateError(f"malformed manifest at {url}: {data!r}")
    return Manifest(*(data[k] for k in _MANIFEST_FIELDS))


def bootstrap_url(base: str, *, windows: bool) -> str:
    # The stable entry point, mirroring fetch_manifest(): always /latest/, never a version.
    return f"{base.rstrip('/')}/latest/{'install.ps1' if windows else 'install.sh'}"


def _read_url(url: str, timeout: float) -> bytes:
    ctx = _dist_ssl_context()
    with urllib.request.urlopen(url, timeout=timeout, context=ctx) as resp:  # noqa: S310
        return resp.read()


# --- release signing (docs/RELEASE-SIGNING.md) --------------------------------
#
# `SHA256SUMS` is what the whole update path hangs off: the bootstrap verifies uv and
# both wheels against it, and (since signing landed) it also carries the bootstrap
# scripts' own hashes. Verifying its minisign signature against the key PINNED IN THE
# CURRENTLY INSTALLED CLIENT is therefore what turns "trust the release host" into
# "trust the key holder": a compromised host can serve only bytes the signing key
# actually signed. Honest limits, stated where the code lives:
#   - first install (curl|sh) is TOFU and stays TOFU — the bootstrap fetched from the
#     host cannot be verified by a key it delivers itself; signing protects UPDATES,
#     where the pinned key predates the fetch.
#   - the manifest (latest.json) is unsigned, so a host compromise can still replay an
#     older SIGNED release (downgrade/freeze). It cannot introduce new code.
#   - verify-if-present: releases predating signing have no .minisig, so absence is a
#     WARNING while [dist] require_signed=false (the default until every supported
#     version is signed). An INVALID signature is fatal regardless of that flag —
#     invalid is tampering evidence, absence is history.

@dataclass(frozen=True)
class SignedSums:
    """Result of the best-effort SHA256SUMS signature check for one release."""
    text: "str | None"      # the SHA256SUMS content, when it could be fetched
    verified: bool          # True only when the minisign signature verified against the pinned key
    warning: "str | None"   # one-line, caller-printed explanation when not verified


def require_signed(cfg) -> bool:
    """[dist] require_signed — default false FOR NOW (flips once every supported
    release is signed). Garbage values fail loud: silently reading a mistyped
    security flag as false would be the worst of both worlds."""
    if not cfg.has_section("dist"):
        return False
    raw = cfg.get("dist", "require_signed", fallback="").strip().lower()
    if raw in ("", "0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    raise UpdateError(f"[dist] require_signed = {raw!r} is not a boolean (use true or false)")


def fetch_signed_sums(base: str, version: str, *, require_signed: bool,
                      timeout: float = 10.0) -> SignedSums:
    """Fetch `<base>/<version>/SHA256SUMS` (+ its .minisig) and verify the signature
    against the client's pinned key. Every degraded outcome is explicit:

      no pinned key            -> nothing to verify against; silent skip (pre-mint
                                  builds), unless require_signed, which then fails.
      sums/.minisig unfetchable-> warning (or hard failure under require_signed).
      signature INVALID        -> UpdateError, ALWAYS — require_signed only governs
                                  absence, never validity.
      verified                 -> the sums text is signature-anchored; the caller can
                                  pin further hashes (the bootstrap script) to it.
    """
    # Lazy on purpose: hooks import updater for the daily version check, and the
    # verification machinery must stay off every hook path (import-boundary spirit —
    # signing.py is stdlib-only, but it has exactly one caller: this update path).
    from firekeep_client import signing

    pinned = signing.PINNED_PUBLIC_KEY.strip()
    if not pinned:
        if require_signed:
            raise UpdateError(
                "[dist] require_signed = true, but this client build pins no release "
                "signing key (firekeep_client/signing.py PINNED_PUBLIC_KEY is empty) — "
                "update from a build that pins one, or unset require_signed"
            )
        return SignedSums(text=None, verified=False, warning=None)

    sums_url = f"{base.rstrip('/')}/{version}/SHA256SUMS"
    try:
        sums_bytes = _read_url(sums_url, timeout)
    except (urllib.error.URLError, OSError) as exc:
        if require_signed:
            raise UpdateError(
                f"cannot fetch {sums_url} for signature verification "
                f"([dist] require_signed = true): {exc}"
            ) from exc
        return SignedSums(None, False,
                          f"cannot fetch SHA256SUMS for {version} ({exc}); "
                          f"skipping signature verification")

    sums_text = sums_bytes.decode("utf-8", "replace")
    sig_url = sums_url + ".minisig"
    try:
        sig_bytes = _read_url(sig_url, timeout)
    except (urllib.error.URLError, OSError) as exc:
        if require_signed:
            raise UpdateError(
                f"release {version} is not signed (no SHA256SUMS.minisig) and "
                f"[dist] require_signed = true — refusing to update. ({exc})"
            ) from exc
        return SignedSums(sums_text, False,
                          f"release {version} is not signed (no SHA256SUMS.minisig); "
                          f"relying on TLS + checksums alone")

    try:
        trusted = signing.verify(sums_bytes, sig_bytes.decode("utf-8", "replace"), pinned)
    except signing.VerifyUnavailable as exc:
        if require_signed:
            raise UpdateError(
                f"cannot verify the release signature ({exc}) and "
                f"[dist] require_signed = true — refusing to update"
            ) from exc
        return SignedSums(sums_text, False,
                          f"release signature present but unverifiable ({exc}); "
                          f"relying on TLS + checksums alone")
    except signing.SignatureError as exc:
        raise UpdateError(
            f"SIGNATURE VERIFICATION FAILED for release {version}'s SHA256SUMS: {exc}. "
            f"Refusing to update — this can indicate a compromised release host, and "
            f"no configuration overrides it."
        ) from exc

    bound = signing.trusted_comment_version(trusted)
    if bound is not None and bound != version:
        raise UpdateError(
            f"release {version}'s SHA256SUMS carries a valid signature for a DIFFERENT "
            f"release ({bound}) — refusing to update (signature replay across versions)"
        )
    return SignedSums(sums_text, True, None)


def sums_entry(sums_text: str, name: str) -> str:
    """The sha256 for `name` out of a SHA256SUMS body ('<hex>  <basename>' lines)."""
    for line in sums_text.splitlines():
        digest, _, fname = line.strip().partition("  ")
        if fname.strip() == name and digest:
            return digest.strip().lower()
    raise UpdateError(f"the signed SHA256SUMS has no entry for {name} — release is malformed")


def bootstrap_sha256(manifest: Manifest, signed: SignedSums, *, windows: bool) -> str:
    """The hash `firekeep update` must demand of the bootstrap it is about to execute.

    Unverified: the manifest's hash, exactly as before signing existed. Verified: the
    SIGNED SHA256SUMS entry is authoritative, and the unsigned manifest must AGREE with
    it — a disagreement means the host is serving a manifest the key holder never
    described, which is precisely the attack signing exists to catch."""
    expected = manifest.bootstrap_hash_for(windows=windows).strip().lower()
    if not signed.verified or signed.text is None:
        return expected
    name = "install.ps1" if windows else "install.sh"
    anchored = sums_entry(signed.text, name)
    if anchored != expected:
        raise UpdateError(
            f"latest.json's {name} hash does not match the SIGNED SHA256SUMS entry — "
            f"refusing to update (the unsigned manifest disagrees with the signed release)"
        )
    return anchored


def download(url: str, dest: Path, *, sha256: str, timeout: float = 60.0) -> Path:
    """Fetch `url` to `dest`, verifying its digest FIRST. Nothing is written unless the
    checksum matches, so an unverified artifact never exists on disk at all.

    `sha256` is required on purpose — see the module's design note: `firekeep update` downloads
    the bootstrap script and then executes it, so an optional checksum would let exactly the
    most dangerous call site skip verification.
    """
    try:
        body = _read_url(url, timeout)
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError(f"download failed for {url}: {exc}") from exc
    actual = hashlib.sha256(body).hexdigest()
    if actual != sha256.strip().lower():
        raise UpdateError(f"checksum mismatch for {url}: expected {sha256}, got {actual}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return dest
