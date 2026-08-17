"""The wire contract (spec §3), asserted byte-exact against a fake server.

Every shape here is a promise to a server that is already built. If one of
these drifts, the failure lands in production as a 422 or, worse, as content
filed under the wrong name.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata

import pytest

from firekeep_docdex import wire

SID = "0123456789abcdef" * 2
REL = "notes/runbook.md"


def _expected_name(source_id: str, relpath: str) -> str:
    digest = hashlib.sha256(relpath.encode("utf-8")).hexdigest()
    return f"docdex:{source_id}:{digest}"


# --- source_name ------------------------------------------------------------


def test_source_name_is_exactly_the_specified_scheme():
    assert wire.source_name(SID, REL) == _expected_name(SID, REL)


def test_source_name_hashes_the_NORMALIZED_relpath():
    """NFD input and backslashes must produce the SAME name as the NFC
    forward-slash form — one folder synced from macOS and Windows is one
    source, not two."""
    nfd = unicodedata.normalize("NFD", "notes/café.md")
    windows = "notes\\café.md"
    canonical = _expected_name(SID, unicodedata.normalize("NFC", "notes/café.md"))
    assert wire.source_name(SID, nfd) == canonical
    assert wire.source_name(SID, windows) == canonical


def test_source_name_carries_no_slash_and_no_filename():
    """The DELETE route takes this as one path parameter, and the name is
    visible to anyone who may list sources — so it must not leak the filename
    (review #3)."""
    name = wire.source_name(SID, "Personal/Tax Returns 2026.pdf")
    assert "/" not in name
    assert "Tax" not in name and "pdf" not in name.replace("docdex:", "")
    assert name.count(":") == 2


def test_source_name_fits_the_servers_500_char_ceiling():
    assert len(wire.source_name(SID, "a/" * 200 + "deep.md")) < 500


def test_source_name_is_stable_across_calls():
    assert wire.source_name(SID, REL) == wire.source_name(SID, REL)


def test_different_sources_never_collide_on_the_same_relpath():
    assert wire.source_name("a" * 32, REL) != wire.source_name("b" * 32, REL)


# --- ingest payload ---------------------------------------------------------


def test_ingest_payload_is_byte_exact():
    payload = wire.ingest_payload(
        SID, REL, "the text", visibility="member", mtime=1_755_000_000.0
    )
    assert payload == {
        "content": "the text",
        "source_name": _expected_name(SID, REL),
        "source_type": "document",
        "visibility": "member",
        "metadata": {
            "path": REL,
            "mtime": "2025-08-12T12:00:00+00:00",
            "dex": "firekeep.docdex",
            "untrusted_content": "true",
        },
    }


def test_metadata_values_are_all_strings():
    """The server declares `metadata: dict[str, str]`. A bool or a float here
    is a 422 on every single ingest — this is the assertion that would have
    caught the spec's literal `untrusted_content: true`."""
    payload = wire.ingest_payload(SID, REL, "t", visibility="member", mtime=1.0)
    assert all(isinstance(v, str) for v in payload["metadata"].values())


def test_metadata_never_carries_a_server_controlled_key():
    """The server rejects these outright (RESERVED_METADATA_KEYS) because a
    client that could set them could re-tenant its own chunks."""
    reserved = {"workspace_id", "member_id", "visibility", "ingest_id",
                "source_name", "chunk_index", "total_chunks", "committed"}
    payload = wire.ingest_payload(SID, REL, "t", visibility="member", mtime=1.0)
    assert set(payload["metadata"]) & reserved == set()


def test_the_absolute_path_never_reaches_the_wire(tmp_path):
    """The human-readable relpath travels in visibility-authorized metadata;
    the absolute path — which names the member's home directory — travels
    nowhere."""
    payload = wire.ingest_payload(SID, REL, "t", visibility="member", mtime=1.0)
    body = json.dumps(payload)
    assert str(tmp_path) not in body
    assert "C:\\" not in body and "/home/" not in body and "/Users/" not in body


def test_untrusted_content_is_always_present():
    """I7: every docdex chunk is untrusted input. There is no code path that
    omits this flag."""
    for visibility in ("member", "workspace"):
        payload = wire.ingest_payload(SID, REL, "t", visibility=visibility, mtime=1.0)
        assert payload["metadata"]["untrusted_content"] == "true"


def test_visibility_is_passed_through_verbatim():
    for visibility in ("member", "workspace"):
        payload = wire.ingest_payload(SID, REL, "t", visibility=visibility, mtime=1.0)
        assert payload["visibility"] == visibility


def test_an_unknown_visibility_is_refused():
    """Tenancy is never client-asserted, but a typo'd visibility would ingest
    PRIVATE notes as workspace-visible. Fail before the request."""
    with pytest.raises(ValueError, match="visibility"):
        wire.ingest_payload(SID, REL, "t", visibility="public", mtime=1.0)


# --- the routes -------------------------------------------------------------


def test_ingest_posts_to_the_corpus_route(client, server, endpoint):
    client.ingest(SID, REL, "body", visibility="member", mtime=1.0)
    assert server.posts[0]["url"] == "http://keep.test:8100/corpus/ingest"
    assert server.posts[0]["headers"] == endpoint.headers
    assert server.posts[0]["verify"] is endpoint.verify
    assert server.posts[0]["body"]["source_name"] == _expected_name(SID, REL)


def test_delete_file_uses_the_single_source_route(client, server):
    client.delete_file(SID, REL)
    assert server.deletes[0]["url"] == (
        f"http://keep.test:8100/corpus/sources/{_expected_name(SID, REL)}"
    )


def test_the_delete_route_is_not_percent_mangled(client, server):
    """The name is hex-plus-colons, and a colon is a legal path character.
    Escaping it would still route, but the URL in a server log would no longer
    match the name in `corpus_sources` output."""
    client.delete_file(SID, REL)
    assert "%3A" not in server.deletes[0]["url"]
    assert "docdex:" in server.deletes[0]["url"]


def test_delete_source_uses_the_bounded_bulk_route(client, server):
    """One bulk call, not thousands of sequential per-file deletes
    (review #6)."""
    client.delete_source(SID)
    assert server.deletes == [{
        "url": f"http://keep.test:8100/corpus/dex-sources/{SID}",
        "headers": client.endpoint.headers,
        "verify": client.endpoint.verify,
    }]


def test_delete_carries_the_same_auth_as_ingest(client, server, endpoint):
    client.delete_source(SID)
    assert server.deletes[0]["headers"] == endpoint.headers


def test_a_default_client_resolves_cortex_through_the_resolver(configured):
    """Docdex builds no URL and no auth header of its own — it asks the
    resolver, exactly as nightshift's `_evidence` does."""
    c = wire.Client()
    assert c.endpoint.rest_base == "http://keep.test:8100"
    assert c.endpoint.headers["X-API-Key"] == "k3y"
    assert c.endpoint.headers["X-Agent-Id"] == "tester"


def test_dex_identity_constants():
    assert wire.DEX_ID == "firekeep.docdex"
    assert wire.SOURCE_TYPE == "document"
    assert wire.SOURCE_PREFIX == "docdex"
