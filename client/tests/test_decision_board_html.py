"""Failing-first tests for the SP4 Decision Board static HTML module (Task 4).

Guarantees under test (per .superpowers/sdd/task-4-brief.md):
  - BOARD_HTML is a fully static Python string literal — no f-string / .format()
    substitution of spec data. Verified at the AST level: the BOARD_HTML
    assignment must be a plain string constant, not a JoinedStr (f-string) or
    a call to .format()/%-formatting.
  - BOARD_HTML fetches './spec' and posts to './answer' client-side — it never
    embeds question/evidence text itself.
  - BOARD_HTML's single inline <script> defines BOTH esc() (text-context
    escaper) and escAttr() (attribute-context escaper, which ALSO escapes
    quotes) — and escAttr is actually *used* on attribute-context values
    (answer/evidence-ref data), not merely defined.
  - evidence.ref is scheme-checked (only http/https render as a link).
  - BOARD_CSP is a hash-based CSP whose script-src/style-src hashes match the
    ACTUAL inline <script>/<style> bodies extracted from BOARD_HTML —
    recomputed independently here (not merely re-imported from board.py) so a
    future edit that desyncs HTML and CSP fails this test.
  - render_answers(answers) renders the exact shape the board's JS POSTs to
    ./answer: {question_id: {answer, actions_confirmed, skipped}}.

Note (per brief): a browser-level XSS test needs a JS execution harness the
client kit lacks — documented follow-up. This suite's guarantee is static
HTML + escaping helpers present-and-used + CSP hash correctness, not runtime
DOM behavior.
"""
import ast
import base64
import hashlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # client/tests/<file> -> repo root
BOARD_PY = REPO_ROOT / "client" / "firekeep_client" / "decision" / "board.py"


def _import_board():
    from firekeep_client.decision import board
    return board


def _sha256_b64(s: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode()


def _extract_script_and_style(html: str):
    script = re.search(r"<script>(.*?)</script>", html, re.S)
    style = re.search(r"<style>(.*?)</style>", html, re.S)
    assert script, "BOARD_HTML must contain exactly one <script>...</script> block"
    assert style, "BOARD_HTML must contain exactly one <style>...</style> block"
    return script.group(1), style.group(1)


# --- Module shape ------------------------------------------------------------


def test_board_module_importable_with_expected_interface():
    board = _import_board()
    assert isinstance(board.BOARD_HTML, str)
    assert isinstance(board.BOARD_CSP, str)
    assert callable(board.render_answers)


# --- Static-string guarantee (AST-level, not just runtime type) --------------


def test_board_html_is_assigned_a_plain_string_literal_not_fstring_or_format():
    assert BOARD_PY.is_file(), f"expected {BOARD_PY}"
    tree = ast.parse(BOARD_PY.read_text(encoding="utf-8"), filename=str(BOARD_PY))
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "BOARD_HTML" for t in node.targets
        ):
            found = True
            value = node.value
            assert isinstance(value, ast.Constant) and isinstance(value.value, str), (
                "BOARD_HTML must be a plain string literal (no f-string, "
                ".format(), or %-substitution) — found "
                f"{type(value).__name__} instead"
            )
    assert found, "BOARD_HTML assignment not found in board.py"


def test_board_html_contains_no_stray_python_format_placeholders():
    """Defense in depth: no bare {question}/{evidence}-style placeholders."""
    board = _import_board()
    for marker in ("{text}", "{question", "{evidence", "{snippet", "{answers}", "{ref}"):
        assert marker not in board.BOARD_HTML


def test_board_html_has_exactly_one_script_and_one_style_block():
    board = _import_board()
    assert board.BOARD_HTML.count("<script>") == 1
    assert board.BOARD_HTML.count("</script>") == 1
    assert board.BOARD_HTML.count("<style>") == 1
    assert board.BOARD_HTML.count("</style>") == 1


def test_board_html_has_no_inline_event_handlers_or_inline_styles():
    """Hash-based CSP has no 'unsafe-inline'/'unsafe-hashes' — inline on*= handlers
    and style= attributes would silently no-op (or worse, look wired but do nothing)."""
    board = _import_board()
    assert re.search(r'\son\w+\s*=', board.BOARD_HTML) is None
    assert 'style="' not in board.BOARD_HTML
    assert "javascript:" not in board.BOARD_HTML.lower()


# --- Client-side fetch/post contract ------------------------------------------


def test_board_html_fetches_spec_client_side_relative():
    """Must resolve against the CURRENT document path, not a bare './spec'.

    A bare fetch('./spec') resolves against the URL's *directory*: served at
    a trailing-slash-less path like "/board/<id>" (plausible for a hand-rolled
    HTTP handler — see task-5-brief.md's `GET /board/<id>`), './spec' would
    resolve to "/board/spec", silently dropping the board id. The board must
    instead derive its base from location.pathname (trailing slash stripped)
    and append '/spec', which is correct with or without a trailing slash.
    """
    board = _import_board()
    script, _style = _extract_script_and_style(board.BOARD_HTML)
    assert "location.pathname" in script
    assert "'/spec'" in script or '"/spec"' in script
    assert re.search(r"fetch\(\s*['\"]\./spec['\"]", script) is None, (
        "bare fetch('./spec') loses the board id when served without a "
        "trailing slash — must resolve via location.pathname instead"
    )


def test_board_html_posts_answers_as_json():
    board = _import_board()
    script, _style = _extract_script_and_style(board.BOARD_HTML)
    assert "location.pathname" in script
    assert "'/answer'" in script or '"/answer"' in script
    assert re.search(r"fetch\(\s*['\"]\./answer['\"]", script) is None
    assert "application/json" in script


# --- Escaping helpers present AND used ----------------------------------------


def test_esc_and_escattr_are_both_defined():
    board = _import_board()
    script, _style = _extract_script_and_style(board.BOARD_HTML)
    assert re.search(r"function\s+esc\s*\(|const\s+esc\s*=", script)
    assert re.search(r"function\s+escAttr\s*\(|const\s+escAttr\s*=", script)


def test_escattr_escapes_quotes_in_addition_to_esc_targets():
    board = _import_board()
    script, _style = _extract_script_and_style(board.BOARD_HTML)
    escattr_def = re.search(r"function\s+escAttr\s*\([^)]*\)\s*\{(.*?)\n  \}", script, re.S)
    assert escattr_def, "could not locate escAttr function body"
    body = escattr_def.group(1)
    assert '"' in body and "'" in body, (
        "escAttr must additionally escape double and single quotes"
    )


def test_escattr_is_actually_called_on_attribute_values_not_just_defined():
    """The brief requires attribute-context values to be *routed through*
    escAttr, not merely that escAttr exists. Count > 1 proves real call sites
    beyond the function's own definition."""
    board = _import_board()
    script, _style = _extract_script_and_style(board.BOARD_HTML)
    assert script.count("escAttr(") > 1
    assert script.count("esc(") > 1


# --- evidence.ref scheme-checking ---------------------------------------------


def test_evidence_ref_is_scheme_checked_before_rendering_as_link():
    board = _import_board()
    script, _style = _extract_script_and_style(board.BOARD_HTML)
    # Must construct a URL object and gate on http/https protocol specifically.
    assert "new URL(" in script
    assert "'http:'" in script or '"http:"' in script
    assert "'https:'" in script or '"https:"' in script
    # try/catch around URL parsing so a malformed ref never throws unhandled.
    assert re.search(r"try\s*\{.*new URL\(.*\}\s*catch", script, re.S)


# --- render_answers -----------------------------------------------------------


def test_render_answers_renders_the_exact_client_post_shape():
    board = _import_board()
    out = board.render_answers({
        "q0": {"answer": "Restart the ingest worker", "actions_confirmed": ["Run scripts/restart_worker.sh"], "skipped": False},
        "q1": {"answer": "", "actions_confirmed": [], "skipped": True},
    })
    assert isinstance(out, str)
    assert "q0" in out
    assert "Restart the ingest worker" in out
    assert "Run scripts/restart_worker.sh" in out
    assert "q1" in out
    assert "Skip" in out or "skip" in out


def test_render_answers_handles_empty_dict():
    board = _import_board()
    out = board.render_answers({})
    assert isinstance(out, str)
    assert out.strip()


# --- CSP: hash matches the ACTUAL inline script/style, recomputed independently -


def test_board_csp_header_shape():
    board = _import_board()
    assert board.BOARD_CSP.startswith("default-src 'none'; script-src 'sha256-")
    assert "style-src 'sha256-" in board.BOARD_CSP
    assert "connect-src 'self'" in board.BOARD_CSP


def test_board_csp_hashes_match_actual_inline_blocks():
    board = _import_board()
    script, style = _extract_script_and_style(board.BOARD_HTML)
    assert len(script) > 200, "suspiciously short inline script — regex may have truncated early"
    assert len(style) > 100, "suspiciously short inline style — regex may have truncated early"

    assert _sha256_b64(script) in board.BOARD_CSP
    assert _sha256_b64(style) in board.BOARD_CSP


def test_board_csp_hash_is_sensitive_to_script_changes():
    """Sanity check the hash function itself discriminates — guards against a
    no-op _sha256_b64 that always returns the same value."""
    assert _sha256_b64("a") != _sha256_b64("b")
