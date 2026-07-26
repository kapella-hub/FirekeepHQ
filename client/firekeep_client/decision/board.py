"""Static, XSS-safe Decision Board HTML (SP4 Task 4).

Produces:
  - BOARD_HTML: a fully static, self-contained page (inline <style> + a single
    inline <script>). It inlines NO untrusted data — on load its JS fetches
    ``./spec`` (relative -> GET /board/<id>/spec, JSON) and renders every
    field client-side.
  - BOARD_CSP: the Content-Security-Policy header value, hash-based against
    the ACTUAL inline <script>/<style> bodies extracted from BOARD_HTML (so a
    hand-edit of the HTML that isn't re-run through this module can never
    silently desync from the CSP — the hash is derived, not hand-maintained).
  - render_answers(answers): formats submitted answers as compact markdown
    for the agent side of the ``firekeep-decision`` MCP server (Task 5).

STDLIB ONLY. No `mcp`, no `httpx`, no server packages — this module is
imported by the client kit's import-boundary test
(client/tests/test_import_boundary.py), which forbids anything but shim.py
from depending on those.

## XSS design (binding constraint, see task-4-brief.md's B3)

BOARD_HTML's inline script builds each question's markup as an HTML string
via ``esc()``/``escAttr()``, then assigns it to a container element's
``innerHTML`` in one shot (the browser's HTML parser decodes the entities on
insertion, which is what makes both text rendering AND attribute round-trips
via ``getAttribute``/``dataset`` correct). Deliberately NOT built via
``element.setAttribute(attr, escAttr(value))`` / ``element.textContent =
esc(value)`` — those are DOM-API property/method sinks that never parse HTML,
so entity-escaping a value before handing it to them corrupts it (the user
would literally see "&lt;" instead of "<", hrefs would contain literal
"&amp;"). Two contexts, two helpers:
  - ``esc(s)``   — text-node context: escapes ``& < >``.
  - ``escAttr(s)`` — attribute-value context: escapes ``& < > " '`` (esc's
    set, plus quotes, since attribute values in these templates are always
    double-quoted HTML source text).
Every value that lands inside an HTML attribute (answer/action data-*
attributes, evidence.ref hrefs, generated element ids) is routed through
escAttr(); every value that lands in a text node is routed through esc().
``evidence.ref`` is additionally scheme-checked (``new URL(ref)`` with NO
base, so relative strings throw rather than silently resolving against the
page origin) — only http/https protocols render an <a href>.

A browser-level XSS test needs a JS execution harness the client kit lacks
(documented follow-up in task-4-brief.md) — this module's guarantee is
static HTML + escaping helpers present-and-used + correct hash CSP, not
runtime DOM verification.
"""
import base64
import hashlib
import re

# NOTE: BOARD_HTML is a plain (raw) triple-quoted string literal, NOT an
# f-string and NOT built via .format()/%-substitution — the client test
# (test_decision_board_html.py) asserts this at the AST level. Nothing here
# ever embeds question/evidence/answer text; all of that arrives client-side
# via `fetch('./spec')` and is rendered by the inline <script> below.
BOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Firekeep Decision Board</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg-primary: #0d1117;
  --bg-secondary: #161b22;
  --bg-tertiary: #21262d;
  --border: #30363d;
  --text-primary: #e6edf3;
  --text-secondary: #8b949e;
  --text-muted: #6e7681;
  --accent-blue: #58a6ff;
  --accent-green: #3fb950;
  --accent-red: #f85149;
  --accent-orange: #d29922;
}

@media (prefers-color-scheme: light) {
  :root {
    --bg-primary: #ffffff;
    --bg-secondary: #f6f8fa;
    --bg-tertiary: #eaeef2;
    --border: #d0d7de;
    --text-primary: #1f2328;
    --text-secondary: #57606a;
    --text-muted: #6e7781;
    --accent-blue: #0969da;
    --accent-green: #1a7f37;
    --accent-red: #cf222e;
    --accent-orange: #9a6700;
  }
}

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  background: var(--bg-primary);
  color: var(--text-primary);
  line-height: 1.5;
  min-height: 100vh;
}

#app { max-width: 860px; margin: 0 auto; padding: 24px; }

noscript {
  display: block;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
  margin: 24px;
  color: var(--text-primary);
}

.board-header { margin-bottom: 20px; }
.board-header h1 { font-size: 22px; font-weight: 600; margin-bottom: 6px; }
.board-meta { font-size: 13px; color: var(--accent-orange); min-height: 18px; }

.board-context {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent-blue);
  border-radius: 8px;
  padding: 14px 18px;
  margin-bottom: 20px;
  font-size: 14px;
  color: var(--text-secondary);
  line-height: 1.6;
}
.board-context .section-label { margin-bottom: 8px; }

.question {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 18px 20px;
  margin-bottom: 18px;
}

.q-number {
  display: inline-block;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--accent-blue);
  margin-bottom: 6px;
}

.q-text { font-size: 15.5px; font-weight: 500; margin-bottom: 10px; line-height: 1.65; }
.q-text p { margin-bottom: 8px; }
.q-text p:last-child { margin-bottom: 0; }
.q-text strong { font-weight: 700; }
.q-text ul, .q-text ol { margin: 6px 0 10px 22px; }
.q-text li { margin-bottom: 5px; }
.q-text code, .board-context code {
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 5px;
  font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace;
  font-size: 0.9em;
}
.q-text pre, .board-context pre {
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px 12px;
  margin: 8px 0 10px;
  overflow-x: auto;
}
.q-text pre code, .board-context pre code { background: none; border: none; padding: 0; }

.embed-block { margin: 12px 0; }
.embed-title {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  color: var(--text-secondary);
  margin-bottom: 6px;
}
.embed-frame {
  width: 100%;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #ffffff;
  display: block;
}

.badge {
  display: inline-block;
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 999px;
  margin-bottom: 10px;
}
.badge-found { background: rgba(63, 185, 80, 0.15); color: var(--accent-green); }
.badge-notfound { background: rgba(248, 81, 73, 0.15); color: var(--accent-red); }

.evidence { margin: 12px 0; }
.evidence > summary {
  cursor: pointer;
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  padding: 4px 0;
}
.evidence > summary:hover { color: var(--text-primary); }
.evidence[open] > summary { margin-bottom: 2px; }
.evidence-list { list-style: none; margin-top: 10px; display: flex; flex-direction: column; gap: 10px; }
.evidence-item {
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent-blue);
  border-radius: 6px;
  padding: 11px 13px 12px;
}
.evidence-source {
  display: inline-block;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--text-secondary);
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 2px 7px;
  margin-bottom: 9px;
}
.evidence-snippet {
  color: var(--text-primary);
  font-size: 14px;
  line-height: 1.6;
  word-break: break-word;
}
.evidence-ref {
  display: inline-block;
  margin-top: 10px;
  font-size: 12px;
  color: var(--accent-blue);
  word-break: break-all;
  text-decoration: none;
}
.evidence-ref::before { content: "\2197  "; opacity: 0.75; }
.evidence-ref:hover { text-decoration: underline; }

.section-label { font-size: 12px; text-transform: uppercase; color: var(--text-muted); margin-bottom: 6px; }

.suggested-answers, .suggested-actions, .answer-row { margin-top: 12px; }

.chip {
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 6px 12px;
  margin: 0 6px 6px 0;
  color: var(--text-primary);
  font-size: 13px;
  cursor: pointer;
}
.chip:hover { border-color: var(--accent-blue); }

.action-row { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 6px; font-size: 13px; }
.action-row input { margin-top: 3px; }

.answer-text {
  width: 100%;
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
  color: var(--text-primary);
  font-size: 14px;
  resize: vertical;
  font-family: inherit;
}
.answer-text:focus { outline: none; border-color: var(--accent-blue); }

.skip-row { display: flex; align-items: center; gap: 6px; margin-top: 10px; font-size: 13px; color: var(--text-secondary); }

.empty-state { color: var(--text-muted); padding: 20px 0; }

.board-footer { display: flex; align-items: center; gap: 14px; margin-top: 8px; }

.btn {
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 18px;
  color: var(--text-primary);
  font-size: 14px;
  cursor: pointer;
}
.btn:hover { background: var(--border); }
.btn:disabled { opacity: 0.6; cursor: not-allowed; }

.btn-primary { background: #1f6feb; border-color: #1f6feb; color: #ffffff; }
.btn-primary:hover { background: #388bfd; }

.submit-status { font-size: 13px; min-height: 16px; }
.status-ok { color: var(--accent-green); }
.status-error { color: var(--accent-red); }
</style>
</head>
<body>
<noscript>This decision board requires JavaScript to load its questions.</noscript>
<div id="app">
  <header class="board-header">
    <h1>Decision Board</h1>
    <div class="board-meta" id="board-meta"></div>
  </header>
  <section id="board-context" class="board-context" hidden></section>
  <div id="board-embeds"></div>
  <main id="questions">
    <p class="empty-state">Loading...</p>
  </main>
  <footer class="board-footer">
    <button type="button" id="submit-btn" class="btn btn-primary">Submit answers</button>
    <div id="submit-status" class="submit-status" role="status" aria-live="polite"></div>
  </footer>
</div>
<script>
(function () {
  'use strict';

  // Board-relative base path, derived from the CURRENT document URL rather
  // than a bare './spec'/'./answer' relative fetch: if this page is served
  // at a path with no trailing slash (e.g. "/board/<id>"), the browser's
  // relative-URL resolution treats "/board/" as the directory and a bare
  // './spec' would resolve to "/board/spec" -- silently dropping the board
  // id. Stripping any trailing slash and appending the endpoint name is
  // correct whether the page is served at ".../<id>" or ".../<id>/".
  var BOARD_BASE = location.pathname.replace(/\/$/, '');

  // Text-node escaper: & < > only. Used exclusively when building HTML
  // strings later assigned via innerHTML (the browser's parser decodes
  // these back on insertion) -- NEVER paired with .textContent = (that sink
  // does not parse HTML, so escaping first would show literal "&lt;").
  function esc(value) {
    var str = (value === undefined || value === null) ? '' : String(value);
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // Attribute-value escaper: esc()'s set plus quotes, since every attribute
  // this module writes is double-quoted HTML source text. Same innerHTML-only
  // rule applies -- never paired with .setAttribute(), which does not parse
  // HTML either.
  function escAttr(value) {
    return esc(value).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Scheme check for evidence.ref: only http/https render as a link.
  // new URL(value) with NO base means relative strings (and anything else
  // that is not an absolute URL) throw rather than silently resolving
  // against the page origin.
  function isSafeHttpUrl(value) {
    if (typeof value !== 'string') { return false; }
    var trimmed = value.trim();
    if (!trimmed) { return false; }
    try {
      var url = new URL(trimmed);
      return url.protocol === 'http:' || url.protocol === 'https:';
    } catch (err) {
      return false;
    }
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  // Mini-markdown: ESCAPE-THEN-FORMAT. The input is passed through esc()
  // FIRST, so every transform below operates on entity-escaped text and can
  // only ever introduce the fixed tags written here -- author-controlled
  // markup can never survive into the output. Supported: fenced code blocks,
  // `code`, **bold**, *italic*, - / 1. lists, paragraphs and line breaks.
  function mdFormat(raw) {
    var text = esc(raw);
    var fences = [];
    text = text.replace(/```([\s\S]*?)```/g, function (m, body) {
      fences.push('<pre><code>' + body.replace(/^\n+|\n+$/g, '') + '</code></pre>');
      return '<F' + (fences.length - 1) + '>';
    });
    text = text.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    text = text.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>');

    var lines = text.split('\n');
    var html = '';
    var para = [];
    var list = null; // 'ul' | 'ol' | null

    function flushPara() {
      if (para.length) { html += '<p>' + para.join('<br>') + '</p>'; para = []; }
    }
    function flushList() {
      if (list) { html += '</' + list + '>'; list = null; }
    }

    lines.forEach(function (line) {
      var bullet = /^\s*[-*]\s+(.*)$/.exec(line);
      var numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
      if (bullet || numbered) {
        flushPara();
        var kind = bullet ? 'ul' : 'ol';
        if (list !== kind) { flushList(); html += '<' + kind + '>'; list = kind; }
        html += '<li>' + (bullet ? bullet[1] : numbered[1]) + '</li>';
      } else if (!line.trim()) {
        flushPara(); flushList();
      } else {
        flushList();
        para.push(line);
      }
    });
    flushPara(); flushList();

    return html.replace(/<F(\d+)>/g, function (m, n) {
      return fences[Number(n)] || '';
    });
  }

  // Sandboxed embed iframes. src is built from BOARD_BASE plus a coerced
  // integer index -- never from author-controlled strings. allow-scripts
  // WITHOUT allow-same-origin: scripts run in an opaque origin with no access
  // to this page, its cookies, or the answer endpoint (the server's origin
  // guard independently rejects opaque-origin POSTs).
  function embedHtml(meta) {
    var idx = parseInt(meta && meta.i, 10);
    if (isNaN(idx) || idx < 0) { return ''; }
    var height = parseInt(meta && meta.height, 10);
    if (isNaN(height)) { height = 360; }
    height = Math.max(120, Math.min(2000, height));
    var html = '<div class="embed-block">';
    var title = (meta && meta.title) ? String(meta.title) : '';
    if (title) { html += '<div class="embed-title">' + esc(title) + '</div>'; }
    html += '<iframe class="embed-frame" src="' + escAttr(BOARD_BASE + '/embed/' + idx) +
      '" height="' + height + '" sandbox="allow-scripts" title="' +
      escAttr(title || 'embedded content') + '" loading="lazy"></iframe>';
    html += '</div>';
    return html;
  }

  // Builds one question's inner markup as an escaped HTML string. Every
  // interpolated text value goes through esc(); every interpolated
  // attribute value goes through escAttr(). Round-trip data needed later
  // (suggested-answer text, confirmed-action text) is carried in data-*
  // attributes -- reading them back via .dataset/.getAttribute() after this
  // string has been parsed by the browser (innerHTML) yields the correctly
  // DECODED original value, so there is no double-escaping on read.
  function questionCardHtml(q, num, qEmbeds) {
    var qid = (q && q.id !== undefined) ? q.id : '';
    var text = (q && q.text !== undefined) ? q.text : '';
    var found = !!(q && q.knowledge_found);
    var evidence = asArray(q && q.evidence);
    var suggestedAnswers = asArray(q && q.suggested_answers);
    var suggestedActions = asArray(q && q.suggested_actions);

    var html = '';
    html += '<span class="q-number">Question ' + (parseInt(num, 10) || 0) + '</span>';
    html += '<div class="q-text">' + mdFormat(text) + '</div>';
    html += '<span class="badge ' + (found ? 'badge-found' : 'badge-notfound') + '">' +
      (found ? 'Knowledge found' : 'No prior knowledge found') + '</span>';

    asArray(qEmbeds).forEach(function (meta) { html += embedHtml(meta); });

    if (evidence.length) {
      html += '<details class="evidence"><summary>Evidence (' + evidence.length + ')</summary>';
      html += '<ul class="evidence-list">';
      evidence.forEach(function (ev) {
        var source = (ev && ev.source !== undefined) ? ev.source : '';
        // Memory snippets arrive as raw blobs with ragged newlines/indentation;
        // collapse internal whitespace to single spaces so they read as clean prose.
        var snippet = (ev && ev.snippet !== undefined)
          ? String(ev.snippet).replace(/\s+/g, ' ').trim() : '';
        var ref = ev && ev.ref;
        html += '<li class="evidence-item">';
        html += '<div class="evidence-source">' + esc(source) + '</div>';
        html += '<div class="evidence-snippet">' + esc(snippet) + '</div>';
        if (isSafeHttpUrl(ref)) {
          html += '<a class="evidence-ref" href="' + escAttr(ref) + '" target="_blank" rel="noopener noreferrer">' + esc(ref) + '</a>';
        }
        html += '</li>';
      });
      html += '</ul></details>';
    }

    if (suggestedAnswers.length) {
      html += '<div class="suggested-answers"><div class="section-label">Suggested answers</div>';
      suggestedAnswers.forEach(function (a) {
        html += '<button type="button" class="chip suggested-answer" data-answer="' + escAttr(a) + '">' + esc(a) + '</button>';
      });
      html += '</div>';
    }

    if (suggestedActions.length) {
      html += '<div class="suggested-actions"><div class="section-label">Suggested actions</div>';
      suggestedActions.forEach(function (act, idx) {
        var cbId = 'action-' + escAttr(qid) + '-' + idx;
        html += '<label class="action-row" for="' + cbId + '">';
        html += '<input type="checkbox" class="action-checkbox" id="' + cbId + '" data-action="' + escAttr(act) + '">';
        html += '<span>UNVERIFIED proposal: ' + esc(act) + '</span></label>';
      });
      html += '</div>';
    }

    html += '<div class="answer-row">';
    html += '<label class="section-label" for="answer-' + escAttr(qid) + '">Your answer</label>';
    html += '<textarea class="answer-text" id="answer-' + escAttr(qid) + '" rows="3" placeholder="Type your answer..."></textarea>';
    html += '</div>';

    html += '<label class="skip-row"><input type="checkbox" class="skip-checkbox"> Skip this question</label>';

    return html;
  }

  function renderBoard(spec) {
    var container = document.getElementById('questions');
    var meta = document.getElementById('board-meta');
    var contextEl = document.getElementById('board-context');
    var boardEmbedsEl = document.getElementById('board-embeds');
    var questions = asArray(spec && spec.questions);
    var embeds = (spec && spec.embeds) || {};
    var byQuestion = (embeds && typeof embeds.by_question === 'object' && embeds.by_question) || {};

    // Plain DOM-property assignment (.textContent =) with the RAW string --
    // no esc() here, since esc() output is only valid inside innerHTML
    // strings.
    var metaText = '';
    if (spec && spec.degraded) {
      metaText = 'Retrieval-only board -- suggestions unavailable';
      if (spec.note) { metaText += ' (' + spec.note + ')'; }
    }
    meta.textContent = metaText;

    var contextText = (spec && spec.context) ? String(spec.context) : '';
    if (contextText) {
      contextEl.innerHTML = '<div class="section-label">Context</div>' + mdFormat(contextText);
      contextEl.hidden = false;
    }

    boardEmbedsEl.innerHTML = asArray(embeds.board).map(embedHtml).join('');

    container.innerHTML = '';
    if (!questions.length) {
      var empty = document.createElement('p');
      empty.className = 'empty-state';
      empty.textContent = 'No questions on this board.';
      container.appendChild(empty);
      return;
    }

    questions.forEach(function (q, i) {
      var qid = (q && q.id !== undefined) ? String(q.id) : '';
      var section = document.createElement('section');
      section.className = 'question';
      section.setAttribute('data-qid', qid);
      section.innerHTML = questionCardHtml(q, i + 1, byQuestion[qid]);
      container.appendChild(section);
    });

    container.querySelectorAll('.suggested-answer').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var section = btn.closest('.question');
        var textarea = section && section.querySelector('.answer-text');
        if (textarea) { textarea.value = btn.dataset.answer || ''; }
      });
    });
  }

  function collectAnswers() {
    var out = {};
    document.querySelectorAll('.question').forEach(function (section) {
      var qid = section.getAttribute('data-qid') || '';
      var textarea = section.querySelector('.answer-text');
      var skipBox = section.querySelector('.skip-checkbox');
      var actions = [];
      section.querySelectorAll('.action-checkbox').forEach(function (cb) {
        if (cb.checked) { actions.push(cb.dataset.action || ''); }
      });
      out[qid] = {
        answer: textarea ? textarea.value.trim() : '',
        actions_confirmed: actions,
        skipped: !!(skipBox && skipBox.checked)
      };
    });
    return out;
  }

  function setStatus(message, isError) {
    var el = document.getElementById('submit-status');
    el.textContent = message;
    el.className = 'submit-status' + (isError ? ' status-error' : ' status-ok');
  }

  function loadSpec() {
    fetch(BOARD_BASE + '/spec')
      .then(function (resp) {
        if (!resp.ok) { throw new Error('spec fetch failed: ' + resp.status); }
        return resp.json();
      })
      .then(function (spec) {
        renderBoard(spec);
      })
      .catch(function (err) {
        var container = document.getElementById('questions');
        container.innerHTML = '';
        var p = document.createElement('p');
        p.className = 'empty-state status-error';
        p.textContent = 'Failed to load board: ' + ((err && err.message) ? err.message : 'unknown error');
        container.appendChild(p);
      });
  }

  function submitAnswers() {
    var btn = document.getElementById('submit-btn');
    btn.disabled = true;
    setStatus('Submitting...', false);
    fetch(BOARD_BASE + '/answer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answers: collectAnswers() })
    })
      .then(function (resp) {
        if (!resp.ok) { throw new Error('submit failed: ' + resp.status); }
        setStatus('Answers submitted. You can close this tab.', false);
      })
      .catch(function (err) {
        btn.disabled = false;
        setStatus('Failed to submit: ' + ((err && err.message) ? err.message : 'unknown error'), true);
      });
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.getElementById('submit-btn').addEventListener('click', submitAnswers);
    loadSpec();
  });
})();
</script>
</body>
</html>
"""

_script_match = re.search(r"<script>(.*?)</script>", BOARD_HTML, re.S)
_style_match = re.search(r"<style>(.*?)</style>", BOARD_HTML, re.S)
if not _script_match or not _style_match:
    raise RuntimeError("BOARD_HTML must contain exactly one <script> and one <style> block")
_script_body = _script_match.group(1)
_style_body = _style_match.group(1)


def _sha256_b64(body: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()


BOARD_CSP = (
    "default-src 'none'; "
    f"script-src '{_sha256_b64(_script_body)}'; "
    f"style-src '{_sha256_b64(_style_body)}'; "
    "connect-src 'self'; "
    # Rich embeds: same-origin /board/<id>/embed/<n> documents, rendered in
    # <iframe sandbox="allow-scripts"> (opaque origin — the sandbox is the
    # isolation boundary; this directive only permits the frame to LOAD).
    "frame-src 'self'"
)


def render_answers(answers: dict) -> str:
    """Format submitted decision-board answers as compact markdown for the agent.

    Expects the shape the board's inline JS POSTs to ``./answer``:
    ``{question_id: {"answer": str, "actions_confirmed": [str, ...], "skipped": bool}}``.
    Tolerates legacy/alternate keys (``text``/``actions``) and non-dict values
    so a malformed payload still renders something instead of raising.
    """
    if not answers:
        return "_No answers submitted._\n"

    lines = ["## Decision Board Answers", ""]
    for qid, value in answers.items():
        lines.append(f"### {qid}")
        if isinstance(value, dict):
            if value.get("skipped"):
                lines.append("- Skipped")
            answer_text = value.get("answer") or value.get("text")
            if answer_text:
                lines.append(f"- Answer: {answer_text}")
            actions = value.get("actions_confirmed") or value.get("actions") or []
            if actions:
                lines.append("- Confirmed actions:")
                for action in actions:
                    lines.append(f"  - {action}")
            if not value.get("skipped") and not answer_text and not actions:
                lines.append("- (no content submitted)")
        else:
            lines.append(f"- Answer: {value}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
