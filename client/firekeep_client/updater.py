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
