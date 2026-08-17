"""The wire contract (spec §3). Thin on purpose, so it can be asserted exactly.

Docdex builds no URL and no auth header of its own: `resolver.resolve("cortex")`
hands over `rest_base`, `headers` and `verify`, and `transport` makes the call —
the same seam `nightshift._evidence` uses. That is why the TLS guard, the
`ca_path` handling and the attribution headers are correct here without this
module knowing they exist.

The source name is the load-bearing shape:

    docdex:<128-bit source id>:<sha256 of the NFC/forward-slash relpath>

Opaque by construction (review #3). No `/`, so it is one clean DELETE path
parameter; ~104 characters, under the server's 500 ceiling; no filename
leakage through an identifier that other members may be able to list; and no
cross-source overwrite, because two members' `~/Notes` carry different ids.
The human-readable relpath travels ONLY in visibility-authorized metadata, and
the ABSOLUTE path travels nowhere at all.
"""
from __future__ import annotations

import datetime
import hashlib
from typing import Any
from urllib.parse import quote

from .scan import normalize_relpath

DEX_ID = "firekeep.docdex"
SOURCE_PREFIX = "docdex"
SOURCE_TYPE = "document"  # a document source type the server added in Phase V

VISIBILITIES = ("member", "workspace")


def source_name(source_id: str, relpath: str) -> str:
    digest = hashlib.sha256(normalize_relpath(relpath).encode("utf-8")).hexdigest()
    return f"{SOURCE_PREFIX}:{source_id}:{digest}"


def ingest_payload(source_id: str, relpath: str, text: str, *,
                   visibility: str, mtime: float) -> dict[str, Any]:
    """The exact `POST /corpus/ingest` body.

    Every metadata value is a STRING: the server declares `dict[str, str]` and
    rejects anything else, so the spec's literal `untrusted_content: true`
    ships as `"true"` and the mtime as an ISO-8601 UTC instant rather than an
    epoch float. Tenancy is absent by design — `workspace_id`, the owning
    `member_id` and the writing dex identity are stamped from the verified
    principal, never client-asserted.
    """
    if visibility not in VISIBILITIES:
        # A typo here would publish a member's PRIVATE notes to their whole
        # workspace. Refuse before the request rather than let the server's
        # default (workspace) decide.
        raise ValueError(f"visibility must be one of {VISIBILITIES}, got {visibility!r}")
    rel = normalize_relpath(relpath)
    return {
        "content": text,
        "source_name": source_name(source_id, rel),
        "source_type": SOURCE_TYPE,
        "visibility": visibility,
        "metadata": {
            "path": rel,
            "mtime": _iso(mtime),
            "dex": DEX_ID,
            # I7 — indexed documents are UNTRUSTED input. Retrieved document
            # text is evidence, never instruction.
            "untrusted_content": "true",
        },
    }


def _iso(mtime: float) -> str:
    try:
        return datetime.datetime.fromtimestamp(
            float(mtime), datetime.timezone.utc
        ).isoformat()
    except (OverflowError, OSError, ValueError):
        # A filesystem can report an mtime no calendar can hold. The document
        # is still perfectly indexable; only its timestamp is unknown.
        return ""


class Client:
    """A cortex endpoint plus the two transport calls, both injectable.

    Injectable because the wire shapes are worth asserting exactly, and a test
    that has to stand up a server to do it will be written once and then
    weakened.
    """

    def __init__(self, endpoint=None, *, post=None, delete=None, timeout: float | None = None):
        if endpoint is None:
            from firekeep_client import resolver

            endpoint = resolver.resolve("cortex")
        self.endpoint = endpoint
        self._post = post or _default_post
        self._delete = delete or _default_delete
        self._timeout = timeout

    def _kwargs(self) -> dict:
        kw = {"headers": self.endpoint.headers, "verify": self.endpoint.verify}
        if self._timeout is not None:
            kw["timeout"] = self._timeout
        return kw

    def ingest(self, source_id: str, relpath: str, text: str, *,
               visibility: str, mtime: float) -> Any:
        payload = ingest_payload(
            source_id, relpath, text, visibility=visibility, mtime=mtime
        )
        return self._post(f"{self.endpoint.rest_base}/corpus/ingest", payload, **self._kwargs())

    def delete_file(self, source_id: str, relpath: str) -> Any:
        # `safe=":"` — a colon is a legal path character (RFC 3986 pchar) and
        # the name is hex-plus-colons by construction, so quoting is a no-op
        # today and the URL reads as the spec writes it. It stays as a guard:
        # anything that ever did appear outside that alphabet (a `/` above all)
        # gets escaped rather than silently reshaping the route.
        name = quote(source_name(source_id, relpath), safe=":")
        return self._delete(
            f"{self.endpoint.rest_base}/corpus/sources/{name}", **self._kwargs()
        )

    def delete_source(self, source_id: str) -> Any:
        """One bounded bulk delete for a whole source (review #6) — not
        thousands of sequential per-file requests."""
        return self._delete(
            f"{self.endpoint.rest_base}/corpus/dex-sources/{quote(source_id, safe='')}",
            **self._kwargs(),
        )


def _default_post(url, body, *, headers, verify, timeout=None):
    from firekeep_client import transport

    kw = {"headers": headers, "verify": verify}
    if timeout is not None:
        kw["timeout"] = timeout
    return transport.post_json(url, body, **kw)


def _default_delete(url, *, headers, verify, timeout=None):
    from firekeep_client import transport

    # transport publishes get_json/post_json only. DELETE goes through the same
    # `_request` rather than a hand-rolled urllib call here, so the TLS context
    # building, the SSE/JSON body handling and the TransportError contract are
    # identical to every other call the kit makes — a second implementation
    # would be a second place for the verify semantics to drift.
    kw = {"headers": headers, "verify": verify}
    if timeout is not None:
        kw["timeout"] = timeout
    return transport._request(url, method="DELETE", **kw)
