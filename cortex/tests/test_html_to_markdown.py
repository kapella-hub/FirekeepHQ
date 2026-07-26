"""HTML->Markdown conversion + a licence regression guard.

html2text is GPL-3.0-or-later and was a hard dependency of every published
cortex image (audit blocker 1). These tests pin both the behaviour and the
absence of the GPL dependency.
"""
from pathlib import Path

from app.collectors.confluence import _to_markdown as confluence_to_markdown
from app.knowledge.crawler import _to_markdown as crawler_to_markdown

REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"


def test_crawler_converts_headings_and_links():
    html = "<h1>Runbook</h1><p>See <a href='https://example.test/x'>the docs</a>.</p>"
    out = crawler_to_markdown(html)
    assert "Runbook" in out
    assert "https://example.test/x" in out


def test_crawler_strips_images():
    html = "<p>before<img src='https://example.test/a.png' alt='a'>after</p>"
    out = crawler_to_markdown(html)
    assert "a.png" not in out


def test_crawler_does_not_hard_wrap_long_lines():
    html = "<p>" + ("word " * 60).strip() + "</p>"
    out = crawler_to_markdown(html).strip()
    assert len(out.splitlines()) == 1


def test_confluence_converts_storage_format():
    xhtml = "<h2>Step 1</h2><p>Run <code>make</code>.</p>"
    out = confluence_to_markdown(xhtml)
    assert "Step 1" in out
    assert "make" in out


def test_confluence_empty_input_returns_empty():
    assert confluence_to_markdown("") == ""


def test_no_gpl_html2text_in_requirements():
    text = REQUIREMENTS.read_text(encoding="utf-8")
    assert "html2text" not in text, "html2text is GPL-3.0-or-later; see audit blocker 1"
