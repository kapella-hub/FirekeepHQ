"""URL ingestion crawler for the docs->skills knowledge pipeline.

Fetches a URL and (optionally) crawls same-site links to a bounded depth,
converting each page to markdown for ingestion. This is a server-side fetcher of
UNTRUSTED URLs (they can arrive from an agent via prompt-injected content, not
just a human), so SSRF defense is the whole game.

Security model:
- `is_safe_url` / `_resolve_public_ips` resolve the host and reject unless EVERY
  resolved A/AAAA address is a public, routable IP. Blocks loopback / private /
  link-local (incl. the 169.254.169.254 cloud-metadata address) / reserved /
  multicast / CGNAT (100.64/10, incl. Alibaba metadata 100.100.100.200) / 6to4 /
  NAT64 / IPv4-mapped IPv6. Checking the RESOLVED IP (not the literal) defeats the
  decimal/octal/hex/DNS host-encoding zoo.
- Every hop — the start URL, each crawled link, and each redirect target — is
  re-validated before its request.
- DNS-rebinding TOCTOU is closed by PINNING: we resolve+validate once, then dial
  the validated IP directly (Host header + SNI preserved), so httpx cannot
  re-resolve to a different (internal) address between check and connect.
- Response bodies are STREAMED and capped (decoded bytes), so a gzip/br
  decompression bomb can't OOM the worker.
Residual/limits: same-site is a last-two-labels heuristic (see _same_site) — it
can wander across a shared public suffix (co.uk, github.io); that's a crawl-scope
concern, not SSRF (wandered URLs still pass is_safe_url). The endpoint itself is
NOT auth-gated on a personal VPS (AUTH_ENABLED=false), so the SSRF guard here is
the only control — hence the pinning.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_REDIRECT_HOPS = 4

# Ranges the stdlib predicates below do NOT already cover (or don't cover
# consistently across Python versions), denied explicitly. is_global alone is
# insufficient (6to4 reports is_global=True on 3.11), so we deny by CIDR.
_EXTRA_DENY = [
    ipaddress.ip_network(n)
    for n in (
        "100.64.0.0/10",   # RFC6598 CGNAT / shared address space (Alibaba metadata 100.100.100.200)
        "2002::/16",       # 6to4
        "64:ff9b::/96",    # NAT64
        "::ffff:0:0/96",   # IPv4-mapped IPv6 (belt-and-suspenders across py versions)
    )
]


@dataclass
class CrawledPage:
    url: str
    title: str
    markdown: str


def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    """A public, routable address. Everything an SSRF wants to reach is NOT this."""
    if any(ip in net for net in _EXTRA_DENY):
        return False
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )


def _resolve_public_ips(url: str) -> tuple[list[str], str]:
    """Resolve `url`'s host and return ([validated public IP strings], "") when
    every resolved address is public, else ([], reason). This is the SSRF gate;
    the returned IPs are what the fetcher must dial (pinning)."""
    try:
        parsed = urlparse(url)
    except Exception as exc:  # pragma: no cover - urlparse is very lenient
        return [], f"unparseable url: {exc}"
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return [], f"scheme not allowed: {parsed.scheme or '(none)'}"
    host = parsed.hostname
    if not host:
        return [], "no host in url"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except Exception as exc:
        return [], f"dns resolution failed: {exc}"
    raw_addrs = list(dict.fromkeys(info[4][0] for info in infos))  # ordered-unique
    if not raw_addrs:
        return [], "host resolved to no addresses"
    for raw in raw_addrs:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return [], f"unparseable address: {raw}"
        if not _ip_is_public(ip):
            return [], f"host resolves to non-public address {ip}"
    return raw_addrs, ""


def is_safe_url(url: str) -> tuple[bool, str]:
    """(ok, reason). ok only when url is http(s) AND every address it resolves to
    is a public IP. Thin bool wrapper over _resolve_public_ips for the endpoint
    fail-fast check and tests."""
    ips, reason = _resolve_public_ips(url)
    return (bool(ips), reason)


class _LinkTitleParser(HTMLParser):
    """Pull <a href> targets and the <title> from an HTML document (stdlib only)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.title: str = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for key, val in attrs:
                if key == "href" and val:
                    self.links.append(val)
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title = data.strip()


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _same_site(start: str, candidate: str) -> bool:
    """True only if `candidate` is the START host or a subdomain of it. Deliberately
    strict and dependency-free: a last-two-labels rule would treat a.github.io and
    b.github.io (or foo.co.uk / evil.co.uk) as same-site and let the crawl wander to
    unrelated third-party content sharing a public suffix. Staying on the exact host
    subtree the user pointed at cannot do that (may under-crawl sibling subdomains —
    acceptable for a doc-ingest tool)."""
    hs, hc = _host(start), _host(candidate)
    if not hs or not hc:
        return False
    return hc == hs or hc.endswith("." + hs)


def _to_markdown(html: str) -> str:
    from markdownify import markdownify  # MIT; replaced GPL html2text (audit blocker 1)

    return markdownify(html, strip=["img"]).strip()


async def _read_capped(resp: httpx.Response, max_bytes: int) -> bytes:
    """Accumulate the DECODED response body up to max_bytes, then stop. iter_bytes()
    yields post-decompression bytes, so this caps a gzip/br decompression bomb."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_bytes:
            break
    return b"".join(chunks)[:max_bytes]


async def _fetch(client: httpx.AsyncClient, url: str, max_bytes: int,
                 _hops: int = 0) -> tuple[str, str] | None:
    """Fetch one page, revalidating+pinning SSRF at every hop. Returns
    (html, final_url) or None (unsafe / non-2xx / non-HTML / error). Never raises."""
    # Offload the blocking getaddrinfo so a slow/hostile DNS can't stall the loop.
    ips, reason = await asyncio.to_thread(_resolve_public_ips, url)
    if not ips:
        logger.info("crawler: skip unsafe url %s (%s)", url, reason)
        return None
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    ip = ips[0]
    ip_host = f"[{ip}]" if ":" in ip else ip
    # Dial the validated IP directly (pin) — httpx cannot re-resolve the name to a
    # different address. Preserve Host + SNI so vhosts and TLS still work.
    pinned = parsed._replace(netloc=f"{ip_host}:{port}").geturl()
    headers = {"Host": host}
    # For HTTPS, set SNI + cert-verification hostname to the real host (httpcore uses
    # sni_hostname as ssl server_hostname), so TLS validates against the name even
    # though the TCP connection is pinned to the validated IP above.
    extensions = {"sni_hostname": host} if parsed.scheme == "https" else {}
    try:
        req = client.build_request("GET", pinned, headers=headers, extensions=extensions)
        resp = await client.send(req, stream=True)
    except Exception as exc:
        logger.info("crawler: fetch failed %s (%s)", url, exc)
        return None
    try:
        if resp.is_redirect:
            if _hops >= _MAX_REDIRECT_HOPS:
                return None
            loc = resp.headers.get("location")
            if not loc:
                return None
            return await _fetch(client, urljoin(url, loc), max_bytes, _hops + 1)
        if resp.status_code != 200:
            return None
        ctype = (resp.headers.get("content-type") or "").lower()
        if "html" not in ctype and "text/" not in ctype:
            return None
        body = await _read_capped(resp, max_bytes)
    finally:
        await resp.aclose()
    html = body.decode(resp.encoding or "utf-8", errors="replace")
    return html, url


async def crawl(start_url: str, *, depth: int, max_pages: int,
                timeout: float = 15.0, max_bytes: int = 2_000_000) -> list[CrawledPage]:
    """BFS from start_url to `depth` following same-site links, up to `max_pages`.
    depth=0 fetches only the start URL. Raises ValueError only for an unsafe START
    url (so the caller can 400); everything after degrades to skipped pages."""
    ok, reason = is_safe_url(start_url)
    if not ok:
        raise ValueError(f"unsafe url: {reason}")

    pages: list[CrawledPage] = []
    seen: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(urldefrag(start_url).url, 0)])

    # follow_redirects=False: we re-validate every 3xx target ourselves.
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=timeout,
        headers={"User-Agent": "Firekeep-KnowledgeCrawler/1.0"},
    ) as client:
        while queue and len(pages) < max_pages:
            url, d = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            fetched = await _fetch(client, url, max_bytes)
            if fetched is None:
                continue
            html, final_url = fetched
            md = _to_markdown(html)
            if not md:
                continue
            parser = _LinkTitleParser()
            try:
                parser.feed(html)
            except Exception:
                pass
            pages.append(CrawledPage(url=final_url, title=parser.title or final_url, markdown=md))
            if d < depth:
                for href in parser.links:
                    nxt = urldefrag(urljoin(final_url, href)).url
                    if nxt in seen or not _same_site(start_url, nxt):
                        continue
                    safe, _ = is_safe_url(nxt)
                    if safe:
                        queue.append((nxt, d + 1))
    return pages
