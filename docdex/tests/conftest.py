"""Shared fixtures. The whole suite runs OFFLINE: binary fixtures are built
here rather than fetched or committed, and every server call in the suite goes
through a fake transport.

`firekeep_client` is a real dependency of the wheel (resolver + transport). In
a monorepo checkout it may not be pip-installed, so fall back to the sibling
`client/` directory — it is stdlib-only at the modules docdex touches.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:  # noqa: SIM105
    import firekeep_client  # noqa: F401
except ImportError:  # pragma: no cover - exercised only on a bare checkout
    sys.path.insert(0, str(_REPO / "client"))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def firekeep_home(tmp_path, monkeypatch):
    """Isolate ~/.firekeep for every test.

    docdex derives its home from `resolver._config_path().parent`, so pointing
    FIREKEEP_CONFIG at a tmp file relocates sources.json, state/ and locks/
    together — the same isolation the client's own suite relies on.
    """
    home = tmp_path / "fkhome"
    home.mkdir()
    monkeypatch.setenv("FIREKEEP_CONFIG", str(home / "config"))
    monkeypatch.delenv("FIREKEEP_BYPASS", raising=False)
    for cap in ("FIREKEEP_DOCDEX_MAX_FILES", "FIREKEEP_DOCDEX_MAX_FILE_MB",
                "FIREKEEP_DOCDEX_MAX_EXTRACT_KB"):
        monkeypatch.delenv(cap, raising=False)
    return home


# --- binary fixture builders ------------------------------------------------
#
# A PDF is assembled by hand rather than with a writer library: it keeps the
# suite free of a generation dependency, and the bytes below are a real PDF
# that pypdf parses through its ordinary path.


def _pdf(pages: list[str | None]) -> bytes:
    """A minimal multi-page PDF. `None` for a page means NO content stream at
    all — the scanned-document shape, whose honest yield is zero text."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    kids_placeholder = add(b"")  # /Pages, patched below once kids are known
    for text in pages:
        contents = b""
        if text is not None:
            escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            stream = (
                b"BT /F1 12 Tf 72 720 Td (" + escaped.encode("latin-1", "replace") + b") Tj ET"
            )
            contents = add(
                b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
            )
        page_body = (
            b"<< /Type /Page /Parent " + str(kids_placeholder).encode() + b" 0 R "
            b"/MediaBox [0 0 612 792] /Resources << /Font << /F1 "
            + str(font).encode() + b" 0 R >> >>"
        )
        if contents:
            page_body += b" /Contents " + str(contents).encode() + b" 0 R"
        page_body += b" >>"
        page_ids.append(add(page_body))
    kids = b" ".join(f"{pid} 0 R".encode() for pid in page_ids)
    objects[kids_placeholder - 1] = (
        b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(len(page_ids)).encode() + b" >>"
    )
    catalog = add(b"<< /Type /Catalog /Pages " + str(kids_placeholder).encode() + b" 0 R >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size " + str(len(objects) + 1).encode()
        + b" /Root " + str(catalog).encode() + b" 0 R >>\nstartxref\n"
        + str(xref).encode() + b"\n%%EOF\n"
    )
    return bytes(out)


def _docx(paragraphs: list[str]) -> bytes:
    """A real .docx written by python-docx — the extractor is worth testing
    against the library that actually produces these files."""
    import io

    import docx

    doc = docx.Document()
    for para in paragraphs:
        doc.add_paragraph(para)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# Text fixtures live on disk (real markup, real MIME, real export shapes — the
# things a regex-based extractor gets wrong are exactly the things a hand-typed
# inline string quietly omits). `.htm` is written from the same bytes as
# `.html`: the alias must go through the identical path or it is not an alias.
_TEXT_FIXTURES = (
    "sample.md", "sample.txt", "sample.html", "sample.eml", "broken.eml",
    "chatgpt.json", "claude.json", "config.json",
)


@pytest.fixture(scope="session")
def docs(tmp_path_factory):
    """A directory of one real file per supported format, plus the honest-zero
    scanned PDF, a declined `.json`, and an unsupported file."""
    d = tmp_path_factory.mktemp("docs")
    for name in _TEXT_FIXTURES:
        (d / name).write_text((FIXTURES / name).read_text(encoding="utf-8"), encoding="utf-8")
    (d / "sample.htm").write_text(
        (FIXTURES / "sample.html").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (d / "sample.pdf").write_bytes(_pdf(["Hello from a real PDF page.", "Second page text."]))
    (d / "scanned.pdf").write_bytes(_pdf([None]))
    (d / "sample.docx").write_bytes(_docx(["Docx paragraph one.", "Docx paragraph two."]))
    (d / "notes.rtf").write_text("unsupported", encoding="utf-8")
    return d


# --- the fake server --------------------------------------------------------
#
# Every server call in the suite goes through this. It records the EXACT wire
# call — url, body, headers, verify — so the wire tests can assert shapes
# byte-for-byte, and it lets a test program a failure at a chosen call index.


class FakeServer:
    def __init__(self):
        self.posts: list[dict] = []
        self.deletes: list[dict] = []
        self.post_hook = None    # (index, url, body) -> response | None
        self.delete_hook = None  # (index, url) -> response | None

    def post(self, url, body, *, headers, verify, timeout=None):
        self.posts.append({"url": url, "body": body, "headers": headers, "verify": verify})
        if self.post_hook is not None:
            result = self.post_hook(len(self.posts) - 1, url, body)
            if result is not None:
                return result
        return {
            "source_name": body.get("source_name"), "chunks_stored": 1,
            "entities_extracted": 0, "relationships_extracted": 0,
            "entity_types_discovered": [], "extraction_status": "skipped",
        }

    def delete(self, url, *, headers, verify, timeout=None):
        self.deletes.append({"url": url, "headers": headers, "verify": verify})
        if self.delete_hook is not None:
            result = self.delete_hook(len(self.deletes) - 1, url)
            if result is not None:
                return result
        return {"deleted_sources": 1, "deleted_chunks": "all"}

    @property
    def ingested_names(self) -> list[str]:
        return [p["body"]["source_name"] for p in self.posts]


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
def endpoint():
    from firekeep_client import resolver

    # verify=False here is not a docdex choice: it is what the resolver itself
    # produces for `scheme = http`, and docdex only ever passes `ep.verify`
    # through. The TLS decision has exactly one home, and it is not this wheel.
    return resolver.Endpoint(
        mcp_url="http://keep.test:8080/mcp",
        rest_base="http://keep.test:8100",
        headers={"X-Agent-Id": "tester", "X-API-Key": "k3y"},
        verify=False,
    )


@pytest.fixture
def client(endpoint, server):
    from firekeep_docdex import wire

    return wire.Client(endpoint, post=server.post, delete=server.delete)


@pytest.fixture
def configured(firekeep_home):
    """A real kit config, so the resolver seam is exercised for real rather
    than only through a hand-built Endpoint."""
    (firekeep_home / "config").write_text(
        "[server]\nkind = ports\nscheme = http\nhost = keep.test\napi_key = k3y\n"
        "\n[identity]\nagent_id = tester\n",
        encoding="utf-8",
    )
    return firekeep_home
