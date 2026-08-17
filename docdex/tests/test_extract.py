"""Text extraction. The contract: NEVER raise, and an honest zero is a result."""
from __future__ import annotations

import pytest

from firekeep_docdex import extract


def test_supported_suffixes_are_the_four_documented_formats():
    assert extract.SUPPORTED_SUFFIXES == frozenset({".md", ".txt", ".pdf", ".docx"})


def test_markdown(docs):
    text, err = extract.extract(docs / "sample.md")
    assert err is None
    assert "rotate the widget key" in text
    assert "café" in text  # non-ASCII survives the round trip


def test_plain_text(docs):
    text, err = extract.extract(docs / "sample.txt")
    assert err is None
    assert "Second paragraph" in text


def test_pdf(docs):
    text, err = extract.extract(docs / "sample.pdf")
    assert err is None
    assert "Hello from a real PDF page." in text
    assert "Second page text." in text  # every page, not just the first


def test_docx(docs):
    text, err = extract.extract(docs / "sample.docx")
    assert err is None
    assert "Docx paragraph one." in text
    assert "Docx paragraph two." in text


def test_scanned_pdf_yields_an_honest_zero_not_an_error(docs):
    """No OCR is a disclosed gap (I5). A text-free PDF is not a failure — it is
    a document with no extractable text, and saying so is the whole point of
    not retrying it every cycle."""
    text, err = extract.extract(docs / "scanned.pdf")
    assert text.strip() == ""
    assert err is None


def test_suffix_matching_is_case_insensitive(tmp_path):
    p = tmp_path / "SHOUTING.MD"
    p.write_text("# loud", encoding="utf-8")
    assert extract.is_supported(p)
    text, err = extract.extract(p)
    assert err is None and "loud" in text


def test_unsupported_suffix_is_an_error_not_a_crash(docs):
    text, err = extract.extract(docs / "notes.rtf")
    assert text == ""
    assert err is not None and "rtf" in err


def test_missing_file_never_raises(tmp_path):
    text, err = extract.extract(tmp_path / "gone.md")
    assert text == ""
    assert err is not None


def test_corrupt_pdf_never_raises(tmp_path):
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"%PDF-1.4\nthis is not a pdf body")
    text, err = extract.extract(p)
    assert text == ""
    assert err is not None


def test_corrupt_docx_never_raises(tmp_path):
    p = tmp_path / "broken.docx"
    p.write_bytes(b"PK\x03\x04 not really a zip")
    text, err = extract.extract(p)
    assert text == ""
    assert err is not None


def test_undecodable_bytes_are_replaced_not_rejected(tmp_path):
    p = tmp_path / "latin.txt"
    p.write_bytes(b"caf\xe9 \xff\xfe tail")
    text, err = extract.extract(p)
    assert err is None
    assert "�" in text and "tail" in text


def test_a_directory_is_an_error_not_a_crash(tmp_path):
    d = tmp_path / "dir.md"
    d.mkdir()
    text, err = extract.extract(d)
    assert text == "" and err is not None


# --- the extract cap --------------------------------------------------------


def test_default_extract_cap_is_400kb(firekeep_home):
    assert extract.max_extract_bytes() == 400 * 1024


def test_extract_cap_is_env_overridable(monkeypatch):
    monkeypatch.setenv("FIREKEEP_DOCDEX_MAX_EXTRACT_KB", "2")
    assert extract.max_extract_bytes() == 2048


def test_a_nonsense_cap_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("FIREKEEP_DOCDEX_MAX_EXTRACT_KB", "not-a-number")
    assert extract.max_extract_bytes() == 400 * 1024
    monkeypatch.setenv("FIREKEEP_DOCDEX_MAX_EXTRACT_KB", "0")
    assert extract.max_extract_bytes() == 400 * 1024


def test_truncate_flags_what_it_cut(monkeypatch):
    monkeypatch.setenv("FIREKEEP_DOCDEX_MAX_EXTRACT_KB", "1")
    text, truncated = extract.truncate("x" * 5000)
    assert truncated is True
    assert len(text.encode("utf-8")) <= 1024


def test_truncate_leaves_short_text_alone(monkeypatch):
    monkeypatch.setenv("FIREKEEP_DOCDEX_MAX_EXTRACT_KB", "1")
    text, truncated = extract.truncate("short")
    assert (text, truncated) == ("short", False)


def test_truncate_never_splits_a_character(monkeypatch):
    """The cap is a BYTE budget but the payload is JSON text: cutting mid
    codepoint would produce a body the server cannot decode."""
    monkeypatch.setenv("FIREKEEP_DOCDEX_MAX_EXTRACT_KB", "1")
    text, truncated = extract.truncate("é" * 2000)
    assert truncated is True
    text.encode("utf-8").decode("utf-8")  # no raise
    assert len(text.encode("utf-8")) <= 1024


@pytest.mark.parametrize("name", ["sample.md", "sample.txt", "sample.pdf", "sample.docx"])
def test_every_supported_format_is_recognized(docs, name):
    assert extract.is_supported(docs / name)
