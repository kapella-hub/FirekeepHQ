"""Tests for the SSRF-guarded URL crawler (app/knowledge/crawler.py).

Hermetic: all DNS resolution is monkeypatched via socket.getaddrinfo, and the
BFS tests monkeypatch _fetch directly rather than performing real HTTP calls.
"""
from __future__ import annotations

import socket

import pytest

from app.knowledge.crawler import _same_site, crawl, is_safe_url


def _fake_getaddrinfo(ip: str, family: int = socket.AF_INET):
    """Build a socket.getaddrinfo replacement that always resolves to `ip`."""

    def _resolve(host, port, *args, **kwargs):
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]

    return _resolve


# ---------------------------------------------------------------------------
# is_safe_url
# ---------------------------------------------------------------------------


def test_is_safe_url_true_for_public_host(monkeypatch):
    monkeypatch.setattr(
        "app.knowledge.crawler.socket.getaddrinfo", _fake_getaddrinfo("93.184.216.34")
    )
    ok, reason = is_safe_url("https://example.com")
    assert ok is True
    assert reason == ""


def test_is_safe_url_false_for_file_scheme():
    ok, reason = is_safe_url("file:///etc/passwd")
    assert ok is False
    assert reason


def test_is_safe_url_false_for_ftp_scheme():
    ok, reason = is_safe_url("ftp://example.com/file")
    assert ok is False
    assert reason


def test_is_safe_url_false_for_loopback(monkeypatch):
    monkeypatch.setattr(
        "app.knowledge.crawler.socket.getaddrinfo", _fake_getaddrinfo("127.0.0.1")
    )
    ok, reason = is_safe_url("https://internal.example.com")
    assert ok is False
    assert reason


def test_is_safe_url_false_for_private_10_range(monkeypatch):
    monkeypatch.setattr(
        "app.knowledge.crawler.socket.getaddrinfo", _fake_getaddrinfo("10.0.0.5")
    )
    ok, reason = is_safe_url("https://internal.example.com")
    assert ok is False
    assert reason


def test_is_safe_url_false_for_private_192_range(monkeypatch):
    monkeypatch.setattr(
        "app.knowledge.crawler.socket.getaddrinfo", _fake_getaddrinfo("192.168.1.1")
    )
    ok, reason = is_safe_url("https://internal.example.com")
    assert ok is False
    assert reason


def test_is_safe_url_false_for_cloud_metadata(monkeypatch):
    monkeypatch.setattr(
        "app.knowledge.crawler.socket.getaddrinfo", _fake_getaddrinfo("169.254.169.254")
    )
    ok, reason = is_safe_url("https://metadata.example.com")
    assert ok is False
    assert reason


def test_is_safe_url_false_for_ipv6_loopback(monkeypatch):
    monkeypatch.setattr(
        "app.knowledge.crawler.socket.getaddrinfo",
        _fake_getaddrinfo("::1", family=socket.AF_INET6),
    )
    ok, reason = is_safe_url("https://internal.example.com")
    assert ok is False
    assert reason


def test_is_safe_url_false_for_no_host():
    ok, reason = is_safe_url("http://")
    assert ok is False
    assert reason == "no host in url"


def test_is_safe_url_false_for_cgnat_100_64(monkeypatch):
    # F1 regression: RFC6598 CGNAT / shared address space must be blocked.
    monkeypatch.setattr(
        "app.knowledge.crawler.socket.getaddrinfo", _fake_getaddrinfo("100.64.0.1")
    )
    ok, reason = is_safe_url("http://internal.example.com")
    assert ok is False
    assert reason


def test_is_safe_url_false_for_alibaba_metadata_100_100(monkeypatch):
    # F1 regression: Alibaba cloud-metadata endpoint lives in 100.64/10.
    monkeypatch.setattr(
        "app.knowledge.crawler.socket.getaddrinfo", _fake_getaddrinfo("100.100.100.200")
    )
    ok, reason = is_safe_url("http://metadata.example.com")
    assert ok is False
    assert reason


def test_is_safe_url_false_for_ipv4_mapped_ipv6(monkeypatch):
    # IPv4-mapped IPv6 must not smuggle a loopback address past the gate.
    monkeypatch.setattr(
        "app.knowledge.crawler.socket.getaddrinfo",
        _fake_getaddrinfo("::ffff:127.0.0.1", family=socket.AF_INET6),
    )
    ok, reason = is_safe_url("http://mapped.example.com")
    assert ok is False
    assert reason


# ---------------------------------------------------------------------------
# _same_site
# ---------------------------------------------------------------------------


def test_same_site_true_for_start_host_and_its_subdomains():
    assert _same_site("https://example.com/x", "https://example.com/z") is True
    assert _same_site("https://example.com/x", "https://docs.example.com/y") is True


def test_same_site_false_for_different_domain():
    assert _same_site("https://a.example.com/x", "https://evil.com/y") is False


def test_same_site_false_for_parent_and_shared_public_suffix():
    # F5: strict host-subtree scoping — do NOT wander up to the parent domain...
    assert _same_site("https://a.example.com/x", "https://example.com/y") is False
    # ...and never to a sibling that merely shares a public suffix.
    assert _same_site("https://foo.co.uk/x", "https://evil.co.uk/y") is False
    assert _same_site("https://a.github.io/x", "https://b.github.io/y") is False


# ---------------------------------------------------------------------------
# crawl BFS
# ---------------------------------------------------------------------------

START_HTML = (
    "<html><head><title>Start Page</title></head><body>"
    "<p>Start content</p>"
    '<a href="/page2">Page 2</a>'
    '<a href="https://evil.com/x">Evil offsite link</a>'
    "</body></html>"
)

PAGE2_HTML = (
    "<html><head><title>Page 2</title></head><body>"
    "<p>Page 2 content</p>"
    "</body></html>"
)


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """All hosts resolve to a public IP by default in the BFS tests; individual
    tests override this where they need an unsafe start URL."""
    monkeypatch.setattr(
        "app.knowledge.crawler.socket.getaddrinfo", _fake_getaddrinfo("93.184.216.34")
    )


@pytest.mark.asyncio
async def test_crawl_depth_zero_returns_only_start_page(monkeypatch):
    calls: list[str] = []

    async def fake_fetch(client, url, max_bytes, _hops=0):
        calls.append(url)
        if url == "https://example.com/start":
            return START_HTML, "https://example.com/start"
        return None

    monkeypatch.setattr("app.knowledge.crawler._fetch", fake_fetch)

    pages = await crawl("https://example.com/start", depth=0, max_pages=25)

    assert len(pages) == 1
    assert pages[0].url == "https://example.com/start"
    assert pages[0].title == "Start Page"
    assert calls == ["https://example.com/start"]


@pytest.mark.asyncio
async def test_crawl_depth_one_follows_same_site_link_only(monkeypatch):
    calls: list[str] = []

    async def fake_fetch(client, url, max_bytes, _hops=0):
        calls.append(url)
        if url == "https://example.com/start":
            return START_HTML, "https://example.com/start"
        if url == "https://example.com/page2":
            return PAGE2_HTML, "https://example.com/page2"
        return None

    monkeypatch.setattr("app.knowledge.crawler._fetch", fake_fetch)

    pages = await crawl("https://example.com/start", depth=1, max_pages=25)

    urls = {p.url for p in pages}
    assert len(pages) == 2
    assert urls == {"https://example.com/start", "https://example.com/page2"}
    # off-site link must never be fetched
    assert "https://evil.com/x" not in calls


@pytest.mark.asyncio
async def test_crawl_raises_value_error_for_unsafe_start_url(monkeypatch):
    monkeypatch.setattr(
        "app.knowledge.crawler.socket.getaddrinfo", _fake_getaddrinfo("127.0.0.1")
    )

    with pytest.raises(ValueError):
        await crawl("https://internal.example.com/start", depth=0, max_pages=25)
