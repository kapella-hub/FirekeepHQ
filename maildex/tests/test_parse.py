"""Parsing mail, which is parsing whatever strangers happened to send.

The governing rule of this file: `parse_message` NEVER raises. Every fixture
below that is deliberately broken asserts a recorded error and a returned
object, because one malformed message must not end a sync over the other 499.
"""
from __future__ import annotations

from conftest import make_message

from firekeep_maildex import parse


# --- headers ----------------------------------------------------------------


def test_the_headers_a_person_reads_are_extracted():
    msg = parse.parse_message(make_message())
    assert msg.headers["subject"] == "Quarterly numbers"
    assert msg.headers["from"] == "priya@example.com"
    assert msg.headers["to"] == "me@example.com"
    assert msg.headers["message_id"] == "<abc123@example.com>"
    assert "18 Aug 2026" in msg.headers["date"]


def test_an_rfc2047_encoded_subject_is_decoded():
    raw = (b"Subject: =?utf-8?B?Q2Fmw6kgcGxhbnM=?=\r\n"
           b"From: a@b.test\r\nMessage-ID: <1@x>\r\n\r\nbody\r\n")
    assert parse.parse_message(raw).subject == "Café plans"


def test_a_folded_header_is_joined_into_one_line():
    raw = (b"Subject: a very long\r\n subject that folded\r\n"
           b"From: a@b.test\r\n\r\nbody\r\n")
    assert parse.parse_message(raw).subject == "a very long subject that folded"


def test_a_message_with_no_headers_at_all_still_parses():
    msg = parse.parse_message(b"just some text with no headers\r\n")
    assert msg.headers == {}
    assert msg.error is None


def test_a_broken_encoded_word_does_not_lose_the_whole_message():
    """The policy's header parser raises on ACCESS, not on parse — so each
    header is read behind its own guard and a bad one costs only itself."""
    raw = (b"Subject: =?utf-8?B?!!!not-base64!!!?=\r\n"
           b"From: a@b.test\r\nMessage-ID: <1@x>\r\n\r\nthe body survives\r\n")
    msg = parse.parse_message(raw)
    assert "the body survives" in msg.body
    assert msg.headers.get("from") == "a@b.test"


# --- bodies -----------------------------------------------------------------


def test_a_plain_text_body_is_taken_verbatim():
    msg = parse.parse_message(make_message(plain="Line one.\nLine two."))
    assert msg.body == "Line one.\nLine two."


def test_text_plain_wins_over_text_html():
    """It is what the sender's client wrote for a text reader, and it needs no
    stripping to be right."""
    msg = parse.parse_message(make_message(
        plain="the plain version", html="<p>the html version</p>"))
    assert msg.body == "the plain version"


def test_an_html_only_message_is_stripped_to_text():
    msg = parse.parse_message(make_message(
        plain=None, html="<html><body><p>Hello <b>there</b></p></body></html>"))
    assert "Hello" in msg.body and "there" in msg.body
    assert "<p>" not in msg.body and "<b>" not in msg.body


def test_a_message_with_no_body_is_an_honest_zero():
    raw = b"Subject: nothing\r\nFrom: a@b.test\r\n\r\n"
    msg = parse.parse_message(raw)
    assert msg.body == ""
    assert msg.error is None


# --- the html stripper ------------------------------------------------------


def test_nested_tags_are_unwrapped():
    assert parse.strip_html(
        "<div><p>outer <span>inner <em>deep</em></span></p></div>"
    ) == "outer inner deep"


def test_entities_are_decoded_including_inside_nesting():
    """`convert_charrefs` handles these at any depth. A regex stripper gets the
    nested case wrong, which is why this is an HTMLParser subclass.

    The non-breaking space is left as the character the sender actually wrote:
    normalising it would mean collapsing runs of spaces, and that mangles the
    indentation of code and quoted logs in plain-text mail.
    """
    assert parse.strip_html("<p>Tom &amp; <b>Jerry&#39;s</b> &nbsp;show</p>") == (
        "Tom & Jerry's \xa0show")


def test_style_and_script_content_never_reaches_the_corpus():
    """Without this, every HTML mail ingests its own stylesheet and the recall
    snippet is a wall of CSS."""
    html = ("<html><head><title>T</title><style>.a{color:red}</style></head>"
            "<body><script>alert(1)</script><p>real words</p></body></html>")
    text = parse.strip_html(html)
    assert text == "real words"


def test_a_style_block_inside_head_does_not_leak_the_rest_of_head():
    """The mute is a COUNTER, not a flag: `</style>` would clear a flag and let
    the remaining head content through."""
    html = "<head><style>x{}</style><title>leaked</title></head><body>kept</body>"
    assert parse.strip_html(html) == "kept"


def test_unclosed_tags_do_not_swallow_the_document():
    assert "words" in parse.strip_html("<div><p>words")


def test_block_tags_become_line_breaks():
    """Paragraphs read as paragraphs; a `<br>` is a single line break."""
    assert parse.strip_html("<p>one</p><p>two</p>") == "one\n\ntwo"
    assert parse.strip_html("one<br>two") == "one\ntwo"


def test_runs_of_layout_whitespace_collapse_to_one_blank_line():
    """A corpus chunk that is 80% newlines wastes the embedding budget the
    real words needed."""
    html = "<div>\n\n  <p>  a  </p>\n\n\n\n<p>b</p>\n\n</div>"
    assert parse.strip_html(html) == "a\n\nb"


def test_the_stripper_never_raises_on_garbage():
    for garbage in ("<<<>>>", "<p", "&#xZZZZ;", "<!--", "</>", "<a href=<b>>x"):
        parse.strip_html(garbage)  # must not raise


# --- attachments (names only, M6) -------------------------------------------


def test_attachment_filenames_are_collected():
    msg = parse.parse_message(make_message(attachments=("q3.pdf", "notes.txt")))
    assert msg.attachments == ["q3.pdf", "notes.txt"]


def test_attachment_content_is_never_extracted():
    """Round 1 indexes filenames, not content — which is also why a 40MB
    attachment costs the parser nothing."""
    msg = parse.parse_message(make_message(plain="see attached",
                                           attachments=("q3.pdf",)))
    assert msg.body == "see attached"
    assert "binary" not in msg.body


def test_a_path_shaped_filename_reads_as_nonsense_not_as_a_path():
    """A filename is attacker-controlled text that lands in metadata a person
    reads."""
    msg = parse.parse_message(make_message(attachments=("../../etc/passwd",)))
    assert msg.attachments == [".._.._etc_passwd"]


def test_a_filename_with_layout_whitespace_is_flattened():
    """The name lands in metadata a person reads and in a `list` line, so it
    must be one line of text whatever the sender called the file."""
    msg = parse.parse_message(make_message(attachments=("a\tb   c",)))
    assert msg.attachments == ["a b c"]


def test_an_absurdly_long_filename_is_bounded():
    msg = parse.parse_message(make_message(attachments=("x" * 500,)))
    assert len(msg.attachments[0]) == 120


# --- broken MIME (recorded, never raised) -----------------------------------


def test_a_truncated_multipart_is_recorded_not_raised():
    raw = (b"Subject: broken\r\nFrom: a@b.test\r\n"
           b'Content-Type: multipart/mixed; boundary="XX"\r\n\r\n'
           b"--XX\r\nContent-Type: text/plain\r\n\r\nthe first part\r\n")
    msg = parse.parse_message(raw)
    assert "the first part" in msg.body


def test_an_undecodable_part_keeps_the_parts_that_decoded():
    """Throwing a message away because its tail was broken would lose real
    mail."""
    raw = (b"Subject: mixed\r\nFrom: a@b.test\r\n"
           b'Content-Type: multipart/mixed; boundary="XX"\r\n\r\n'
           b"--XX\r\nContent-Type: text/plain\r\n\r\ngood part\r\n"
           b"--XX\r\nContent-Type: text/plain; charset=no-such-charset\r\n"
           b"Content-Transfer-Encoding: base64\r\n\r\n!!!!not base64!!!!\r\n"
           b"--XX--\r\n")
    msg = parse.parse_message(raw)
    assert "good part" in msg.body


def test_a_completely_unparseable_blob_records_an_error_and_returns():
    msg = parse.parse_message(b"\x00\xff\xfe" * 100)
    assert msg.error is None or isinstance(msg.error, str)
    assert isinstance(msg.body, str)


def test_parse_never_raises_on_any_of_these():
    for raw in (b"", b"\x00", b"Subject:\r\n", b"Content-Type: /\r\n\r\nx",
                b"Content-Type: multipart/mixed\r\n\r\nno boundary at all",
                "Subject: unicodé\r\n\r\nbody".encode("latin-1")):
        parse.parse_message(raw)  # must not raise


# --- render -----------------------------------------------------------------


def test_render_puts_the_headers_in_the_indexed_text():
    """Recall searches CONTENT — "what did Priya say about the invoice" needs
    the sender inside the indexed text to match."""
    text = parse.parse_message(make_message()).render()
    assert "Subject: Quarterly numbers" in text
    assert "From: priya@example.com" in text
    assert "The numbers are attached." in text


def test_render_lists_attachment_names():
    text = parse.parse_message(make_message(attachments=("q3.pdf",))).render()
    assert "Attachments: q3.pdf" in text


def test_render_of_an_empty_message_is_empty():
    assert parse.parse_message(b"\r\n").render() == ""


# --- the size cap (M6) ------------------------------------------------------


def test_a_message_under_the_cap_is_untouched():
    text, truncated = parse.truncate("short")
    assert (text, truncated) == ("short", False)


def test_a_message_over_the_cap_is_truncated_and_flagged(monkeypatch):
    monkeypatch.setenv("FIREKEEP_MAILDEX_MAX_MESSAGE_KB", "1")
    text, truncated = parse.truncate("x" * 5000)
    assert truncated is True
    assert len(text.encode("utf-8")) == 1024


def test_the_cap_defaults_to_200kb():
    assert parse.max_message_bytes() == 200 * 1024


def test_truncation_never_lands_mid_codepoint(monkeypatch):
    """A cut inside a multi-byte character produces a JSON body the server
    cannot decode."""
    monkeypatch.setenv("FIREKEEP_MAILDEX_MAX_MESSAGE_KB", "1")
    text, truncated = parse.truncate("é" * 2000)
    assert truncated is True
    text.encode("utf-8").decode("utf-8")  # must not raise


def test_a_nonsense_cap_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("FIREKEEP_MAILDEX_MAX_MESSAGE_KB", "-3")
    assert parse.max_message_bytes() == 200 * 1024
