"""Text extraction. The contract: NEVER raise, and an honest zero is a result."""
from __future__ import annotations

import pytest

from firekeep_docdex import extract


def test_supported_suffixes_are_the_documented_formats():
    """The pin is the disclosure (I5): the README's "What it indexes" list and
    this set are the same claim, so widening one without the other is a lie the
    test catches. To REVERSE a format, delete its suffix here, its branch in
    `extract`, its fixture, and its README row — in that order, and expect the
    scan and sync suffix pins to fail until all four are done."""
    assert extract.SUPPORTED_SUFFIXES == frozenset({
        ".md", ".txt", ".pdf", ".docx", ".html", ".htm", ".eml", ".json",
    })


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


# --- html -------------------------------------------------------------------


def test_html_keeps_the_prose(docs):
    text, err = extract.extract(docs / "sample.html")
    assert err is None
    assert "Rotating the widget key" in text
    assert "Run widgetctl rotate" in text
    assert "Step one: drain the node" in text


def test_html_drops_script_style_and_head(docs):
    """Markup is not prose. Without the mute, every page ingests its own
    stylesheet and the recall snippet is a wall of CSS."""
    text, _ = extract.extract(docs / "sample.html")
    assert "Comic Sans" not in text
    assert "bada55" not in text
    assert "window.tracker" not in text
    assert "Enable JavaScript" not in text  # <noscript> is chrome, not content


def test_html_unescapes_entities(docs):
    text, _ = extract.extract(docs / "sample.html")
    assert "rotate & wait" in text  # &amp;
    assert "café" in text           # &eacute;


def test_html_separates_block_elements_with_newlines(docs):
    text, _ = extract.extract(docs / "sample.html")
    # The two unclosed <li> items must not run together into one line.
    assert "drain the nodeStep two" not in text
    assert "Step two: rotate" in text.splitlines()


def test_htm_is_the_same_path_as_html(docs):
    """The alias is only an alias if it produces identical text."""
    assert extract.extract(docs / "sample.htm") == extract.extract(docs / "sample.html")


def test_malformed_html_never_raises(tmp_path):
    p = tmp_path / "broken.html"
    p.write_text("<p>open <b>bold <div><style>x{}</p></div", encoding="utf-8")
    text, err = extract.extract(p)
    assert err is None
    assert "open" in text and "x{}" not in text


# --- eml --------------------------------------------------------------------


def test_eml_renders_a_header_block_then_the_body(docs):
    text, err = extract.extract(docs / "sample.eml")
    assert err is None
    lines = text.splitlines()
    assert lines[0] == "Subject: Invoice 4417 and the rotation window"
    assert "From: Priya Raman <priya@example.com>" in lines
    assert "To: Team Platform <platform@example.com>" in lines
    assert any(line.startswith("Date: ") for line in lines)
    # Headers go into the indexed TEXT, not only into metadata: "what did Priya
    # say about the invoice" needs the sender's name inside the content to match.
    assert "The rotation window is Thursday 22:00 UTC." in text


def test_eml_prefers_text_plain_over_html(docs):
    text, _ = extract.extract(docs / "sample.eml")
    assert "Invoice 4417 is approved." in text
    assert "<b>" not in text and "approved</b>" not in text


def test_eml_lists_attachments_by_name_only(docs):
    text, _ = extract.extract(docs / "sample.eml")
    assert "Attachments: invoice-4417.pdf" in text
    assert "JVBERi0" not in text  # the base64 body is never decoded or indexed


def test_broken_mime_records_what_survived_and_never_raises(docs):
    """A truncated multipart whose boundary never appears, carrying a part with
    a charset that does not exist. The headers are still a document."""
    text, err = extract.extract(docs / "broken.eml")
    assert err is None
    assert "Subject: Truncated in transit" in text
    assert "!!!! this is not base64 !!!!" not in text


def test_eml_of_pure_garbage_never_raises(tmp_path):
    p = tmp_path / "junk.eml"
    p.write_bytes(bytes(range(256)) * 8)
    text, err = extract.extract(p)
    assert isinstance(text, str)
    assert err is None or isinstance(err, str)


def test_eml_attachment_filename_cannot_smuggle_a_path(tmp_path):
    p = tmp_path / "sneaky.eml"
    p.write_text(
        'Subject: hi\nMIME-Version: 1.0\n'
        'Content-Type: application/octet-stream; name="x"\n'
        'Content-Disposition: attachment; filename="../../etc/passwd"\n\nAA==\n',
        encoding="utf-8",
    )
    text, err = extract.extract(p)
    assert err is None
    assert "../../etc/passwd" not in text
    assert ".._.._etc_passwd" in text


# --- json: conversation exports only ----------------------------------------


def test_chatgpt_export_extracts_turns_in_reading_order(docs):
    text, err = extract.extract(docs / "chatgpt.json")
    assert err is None
    assert text.startswith("Conversation export (2 turns)")
    assert "user: How do I rotate the widget key?" in text
    assert "assistant: Drain the node first, then rotate." in text
    # The mapping is a TREE and its keys are in the wrong order on purpose:
    # following `children` from the root is the only thing that reproduces what
    # the person saw. An empty system turn contributes nothing.
    assert text.index("user: How") < text.index("assistant: Drain")


def test_claude_shaped_export_extracts_turns(docs):
    text, err = extract.extract(docs / "claude.json")
    assert err is None
    assert text.startswith("Conversation export (3 turns)")
    assert "user: What is the rollback plan for the widget rotation?" in text
    # A list-of-blocks content field is joined, not stringified as JSON.
    assert "Keep the previous key active for one hour." in text
    assert "Roll back by re-pointing the fleet at it." in text
    assert "'type':" not in text


def test_object_wrapped_message_list_is_a_conversation(tmp_path):
    p = tmp_path / "wrapped.json"
    p.write_text(
        '{"id": "abc", "messages": ['
        '{"author": "Sam", "text": "ship it"},'
        '{"author": {"role": "assistant"}, "text": "shipping"}]}',
        encoding="utf-8",
    )
    text, err = extract.extract(p)
    assert err is None
    assert "Sam: ship it" in text and "assistant: shipping" in text


def test_a_list_of_chatgpt_conversations_is_flattened(tmp_path):
    """`conversations.json` — the file a person actually exports — is a LIST of
    conversation objects. One file is one document."""
    p = tmp_path / "conversations.json"
    p.write_text(
        '[{"mapping": {"a": {"parent": null, "children": [], "message":'
        ' {"author": {"role": "user"}, "content": {"parts": ["first chat"]}}}}},'
        ' {"mapping": {"b": {"parent": null, "children": [], "message":'
        ' {"author": {"role": "user"}, "content": {"parts": ["second chat"]}}}}}]',
        encoding="utf-8",
    )
    text, err = extract.extract(p)
    assert err is None
    assert text.startswith("Conversation export (2 turns)")
    assert "first chat" in text and "second chat" in text


def test_a_generic_json_dump_is_declined_with_a_reason(docs):
    """A generic JSON dump is noise, not a document — and declining is not
    failing: it must not land in the failure count or the retry set."""
    text, err = extract.extract(docs / "config.json")
    assert text == ""
    assert err == "unsupported: json is not a conversation export"
    assert extract.is_unsupported(err)


def test_invalid_json_is_declined_not_failed(tmp_path):
    p = tmp_path / "truncated.json"
    p.write_text('{"messages": [{"role": "user", "content": "cut off"', encoding="utf-8")
    text, err = extract.extract(p)
    assert text == ""
    assert extract.is_unsupported(err) and "not valid json" in err


def test_a_list_of_records_that_is_not_a_chat_log_is_declined(tmp_path):
    """One object carrying `author` and `text` does not make a conversation —
    a real export is message dicts nearly all the way down."""
    p = tmp_path / "records.json"
    p.write_text(
        '[{"id": 1, "kind": "row"}, {"id": 2, "kind": "row"}, {"id": 3, "kind": "row"},'
        ' {"author": "someone", "text": "a comment"}]',
        encoding="utf-8",
    )
    text, err = extract.extract(p)
    assert text == "" and extract.is_unsupported(err)


def test_an_empty_conversation_export_is_an_honest_zero(tmp_path):
    """Conversation-shaped with nothing said. Not an error, not a decline —
    the scanned-PDF outcome: recorded as seen, never retried."""
    p = tmp_path / "empty.json"
    p.write_text('{"messages": [{"role": "user", "content": ""}]}', encoding="utf-8")
    text, err = extract.extract(p)
    assert text == "" and err is None


def test_declining_is_reported_apart_from_failing():
    assert extract.is_unsupported("unsupported: json is not a conversation export")
    assert not extract.is_unsupported("ValueError: broken")
    assert not extract.is_unsupported(None)


def test_json_content_recursion_is_bounded(tmp_path):
    """A hand-written file can nest forever; the extractor may not follow."""
    deep = '{"text": ' * 60 + '"needle"' + "}" * 60
    p = tmp_path / "deep.json"
    p.write_text('{"messages": [{"role": "user", "content": ' + deep + "}]}", encoding="utf-8")
    text, err = extract.extract(p)
    assert err is None or extract.is_unsupported(err)  # never a crash, never a hang


def test_suffix_matching_is_case_insensitive(tmp_path):
    p = tmp_path / "SHOUTING.MD"
    p.write_text("# loud", encoding="utf-8")
    assert extract.is_supported(p)
    text, err = extract.extract(p)
    assert err is None and "loud" in text


def test_unsupported_suffix_is_a_decline_not_a_crash(docs):
    text, err = extract.extract(docs / "notes.rtf")
    assert text == ""
    assert err is not None and "rtf" in err
    assert extract.is_unsupported(err)


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


@pytest.mark.parametrize("name", [
    "sample.md", "sample.txt", "sample.pdf", "sample.docx",
    "sample.html", "sample.htm", "sample.eml", "chatgpt.json",
])
def test_every_supported_format_is_recognized(docs, name):
    assert extract.is_supported(docs / name)
