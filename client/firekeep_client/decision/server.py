"""`firekeep-decision`: local stdio MCP server for the SP4 Decision Board.

When an agent needs to ask the human more than a couple of clarifying
questions, it calls the ``decision_board`` MCP tool instead of asking inline.
This module:

  1. asks Cortex (``POST /decision/synthesize``) to turn the agent's context +
     draft questions into a board spec (retrieved evidence + suggested
     answers/actions per question);
  2. serves that spec from a loopback ``ThreadingHTTPServer`` on an ephemeral
     port and opens the human's browser at ``/board/<id>``;
  3. long-polls (bounded, non-blocking) for the human's submitted answers and
     returns them as markdown.

Fail-soft posture: a Cortex/transport failure never fails the agent — it falls
back to a *local degraded board* built straight from the draft questions (no
evidence, no suggestions). A headless environment (no browser) skips the server
entirely and returns the board as inline text.

## Import boundary

This module MAY ``import mcp`` (the FastMCP entrypoint lives in ``main()`` and is
imported lazily there) — Task 6 widens the client import-boundary exemption to
cover it. It MUST NOT import ``httpx`` or any server package: all HTTP is stdlib
(``http.server`` for the board, ``firekeep_client.transport`` for the Cortex call).
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import anyio

from firekeep_client import hooklog, resolver, transport
from firekeep_client.stdio import force_utf8_stdio
from firekeep_client.decision.board import BOARD_CSP, BOARD_HTML, render_answers

# --------------------------------------------------------------------------- #
# Config (env-tunable)                                                        #
# --------------------------------------------------------------------------- #

# The server-side synthesize call's own timeout (informational default here — the
# client timeout below MUST exceed it so a slow-but-successful synth is not cut
# off by the caller). Kept env-tunable to mirror the server default.
_DEFAULT_SYNTH_TIMEOUT = 30.0
# Extra headroom the client timeout adds over the synth timeout.
_INGEST_TIMEOUT_HEADROOM = 15.0
# Bounded long-poll ceiling — kept under the MCP tool-call ceiling so a single
# decision_board / decision_board_check call returns before the runtime times out.
_DEFAULT_POLL_SECONDS = 24.0
# Poll granularity — MUST be a real ``await anyio.sleep`` (never a blocking wait),
# so the event loop stays responsive.
_POLL_INTERVAL = 0.5
# Abandoned-board reaper horizon (checked opportunistically on each core call).
_DEFAULT_BOARD_TTL_SECONDS = 1800.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _synth_timeout() -> float:
    return _env_float("DECISION_SYNTH_TIMEOUT_SECONDS", _DEFAULT_SYNTH_TIMEOUT)


def _ingest_client_timeout() -> float:
    """Client-side timeout for the Cortex synthesize call.

    Invariant: MUST be greater than the server synth timeout. If explicitly set
    via env we still clamp it above the synth timeout so the invariant can't be
    misconfigured away.
    """
    synth = _synth_timeout()
    raw = os.environ.get("DECISION_INGEST_CLIENT_TIMEOUT_SECONDS")
    if raw is not None and raw.strip() != "":
        try:
            explicit = float(raw)
        except ValueError:
            explicit = synth + _INGEST_TIMEOUT_HEADROOM
        return explicit if explicit > synth else synth + _INGEST_TIMEOUT_HEADROOM
    return synth + _INGEST_TIMEOUT_HEADROOM


def _poll_seconds() -> float:
    return _env_float("DECISION_POLL_SECONDS", _DEFAULT_POLL_SECONDS)


def _board_ttl_seconds() -> float:
    return _env_float("DECISION_BOARD_TTL_SECONDS", _DEFAULT_BOARD_TTL_SECONDS)


def _env_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() not in ("", "0", "false", "no", "off")


# --------------------------------------------------------------------------- #
# Per-process board store                                                      #
# --------------------------------------------------------------------------- #


class _Board:
    """One live board: its spec, the human's answers (once submitted), the HTTP
    server hosting it, and a monotonic birth stamp for TTL reaping."""

    __slots__ = ("board_id", "spec", "answers", "created", "server", "url", "embeds",
                 "opened")

    def __init__(self, board_id: str, spec: dict, embeds: list[dict] | None = None) -> None:
        self.board_id = board_id
        self.spec = spec
        self.answers: dict | None = None  # set by the answer handler thread
        self.created = time.monotonic()
        self.server: ThreadingHTTPServer | None = None
        self.url: str | None = None
        self.opened = False  # did a browser actually launch for this board?
        self.embeds: list[dict] = embeds or []  # normalized rich embeds (see _normalize_embeds)


_BOARDS: dict[str, _Board] = {}


def _shutdown_board(board: _Board) -> None:
    """Stop + close a board's HTTP server. Idempotent and exception-safe."""
    srv = board.server
    if srv is not None:
        try:
            srv.shutdown()  # unblocks serve_forever (called from a different thread)
            srv.server_close()
        except Exception:
            pass
        board.server = None


def _reap_expired() -> None:
    """Opportunistic TTL sweep: shut + drop boards older than the TTL horizon."""
    ttl = _board_ttl_seconds()
    now = time.monotonic()
    for board_id, board in list(_BOARDS.items()):
        if now - board.created > ttl:
            _shutdown_board(board)
            _BOARDS.pop(board_id, None)


# --------------------------------------------------------------------------- #
# Headless detection                                                           #
# --------------------------------------------------------------------------- #


def _is_headless() -> bool:
    """True when we cannot open a real browser for the human.

    - ``FIREKEEP_DECISION_HEADLESS`` truthy forces headless (opt-out / CI).
    - ``webbrowser.get()`` raising means no browser is registered.
    - On Linux only, a missing ``DISPLAY``/``WAYLAND_DISPLAY`` means no GUI.
      (Deliberately NOT applied off-Linux: macOS is POSIX with no DISPLAY yet
      has a browser.)
    """
    if _env_truthy(os.environ.get("FIREKEEP_DECISION_HEADLESS")):
        return True
    try:
        webbrowser.get()
    except Exception:
        return True
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return True
    return False


# --------------------------------------------------------------------------- #
# Board spec construction                                                       #
# --------------------------------------------------------------------------- #


# Rich embeds: agent-authored, self-contained HTML documents rendered by the
# board page in sandboxed iframes (design decided on board 2b2a7b59, 2026-07-14:
# self-contained HTML, scripts allowed inside the sandbox, per-question AND
# board-level placement).
_EMBED_MAX_BYTES = 512_000
_EMBED_HEIGHT_DEFAULT = 360
_EMBED_HEIGHT_MIN, _EMBED_HEIGHT_MAX = 120, 2000


def _normalize_embeds(embeds: list | None) -> list[dict]:
    """Validate + normalize the tool's ``embeds`` argument. Raises ValueError with
    an actionable message on a malformed entry — the agent authored it and can fix
    the call; silent dropping would just yield a mysteriously bare board."""
    out: list[dict] = []
    for n, item in enumerate(embeds or []):
        if not isinstance(item, dict):
            raise ValueError(f"embeds[{n}] must be an object, got {type(item).__name__}")
        html = item.get("html")
        if not isinstance(html, str) or not html.strip():
            raise ValueError(f"embeds[{n}].html must be a non-empty string of self-contained HTML")
        if len(html.encode("utf-8")) > _EMBED_MAX_BYTES:
            raise ValueError(
                f"embeds[{n}].html exceeds {_EMBED_MAX_BYTES} bytes — inline less data "
                f"or summarize; the board is a decision surface, not a data warehouse"
            )
        question = item.get("question")
        if question is not None and not isinstance(question, int):
            raise ValueError(
                f"embeds[{n}].question must be an integer index into draft_questions, or null "
                f"for a board-level embed"
            )
        height = item.get("height", _EMBED_HEIGHT_DEFAULT)
        if not isinstance(height, (int, float)):
            height = _EMBED_HEIGHT_DEFAULT
        height = max(_EMBED_HEIGHT_MIN, min(_EMBED_HEIGHT_MAX, int(height)))
        out.append(
            {
                "html": html,
                "title": str(item.get("title", "") or ""),
                "question": question,
                "height": height,
            }
        )
    return out


def _attach_embeds(spec: dict, embeds: list[dict]) -> None:
    """Publish embed METADATA (never the HTML itself) into the board spec.

    ``{"embeds": {"board": [...], "by_question": {qid: [...]}}}`` where each entry
    is ``{i, title, height}`` and ``i`` indexes the /embed/<i> route. A question
    index that doesn't resolve to a question id on the (possibly Cortex-rewritten)
    spec degrades to board-level rather than disappearing.
    """
    if not embeds:
        return
    known_ids = {str(q.get("id")) for q in spec.get("questions") or []}
    board_level: list[dict] = []
    by_question: dict[str, list[dict]] = {}
    for i, e in enumerate(embeds):
        meta = {"i": i, "title": e["title"], "height": e["height"]}
        qid = f"q{e['question']}" if e["question"] is not None else None
        if qid is not None and qid in known_ids:
            by_question.setdefault(qid, []).append(meta)
        else:
            board_level.append(meta)
    spec["embeds"] = {"board": board_level, "by_question": by_question}


def _question_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("text", ""))
    return str(item)


def _local_degraded_spec(context: str, draft_questions: list) -> dict:
    """Build a board spec locally when Cortex is unreachable/headless.

    No retrieval, no suggestions — just the agent's own draft questions with
    ``knowledge_found=False``. board_id is minted with ``secrets.token_urlsafe``.
    """
    questions = []
    for i, q in enumerate(draft_questions or []):
        questions.append(
            {
                "id": f"q{i}",
                "text": _question_text(q),
                "knowledge_found": False,
                "evidence": [],
                "suggested_answers": [],
                "suggested_actions": [],
            }
        )
    return {
        "board_id": secrets.token_urlsafe(16),
        "context": context,
        "questions": questions,
        "knowledge_found": False,
        "degraded": True,
        "note": "Cortex unreachable — retrieval and suggestions unavailable.",
    }


def _render_spec_inline(spec: dict) -> str:
    """Render a board spec as plain markdown for the headless path (no browser)."""
    lines = ["## Decision Board (headless — interactive board unavailable)", ""]
    context = spec.get("context")
    if context:
        lines.append(f"**Context:** {context}")
        lines.append("")
    if spec.get("degraded"):
        note = spec.get("note")
        lines.append(f"_Retrieval-only: {note}_" if note else "_Retrieval-only board._")
        lines.append("")
    questions = spec.get("questions") or []
    if not questions:
        lines.append("_No questions on this board._")
        return "\n".join(lines).rstrip() + "\n"
    lines.append("Answer these inline (no interactive board could be opened):")
    lines.append("")
    for q in questions:
        qid = q.get("id", "")
        lines.append(f"- **{qid}** {q.get('text', '')}")
        suggested = q.get("suggested_answers") or []
        if suggested:
            lines.append(f"    - suggested answers: {', '.join(str(s) for s in suggested)}")
        actions = q.get("suggested_actions") or []
        if actions:
            lines.append(
                "    - UNVERIFIED suggested actions: "
                + ", ".join(str(a) for a in actions)
            )
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Loopback board HTTP server                                                    #
# --------------------------------------------------------------------------- #


def _split_board_path(path: str) -> tuple[str | None, str]:
    """Parse ``/board/<id>[/spec|/answer|/embed/<n>]`` -> (board_id, subpath).

    The subpath is the JOINED remainder ("spec", "answer", "embed/0", ...) so
    multi-segment routes survive. Returns ``(None, "")`` for anything that
    isn't a /board/<id> route.
    """
    clean = urllib.parse.urlsplit(path).path
    parts = [p for p in clean.split("/") if p != ""]
    if len(parts) < 2 or parts[0] != "board":
        return None, ""
    board_id = urllib.parse.unquote(parts[1])
    sub = "/".join(parts[2:])
    return board_id, sub


class _BoardRequestHandler(BaseHTTPRequestHandler):
    # Quiet: the board server must not spam the agent's stderr.
    def log_message(self, *args: Any) -> None:  # noqa: D401
        return

    def _send(self, code: int, body: bytes = b"", content_type: str = "text/plain",
              extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        if body or content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        board_id, sub = _split_board_path(self.path)
        board = _BOARDS.get(board_id) if board_id else None
        if board is None:
            self._send(404, b"no such board", "text/plain")
            return
        if sub == "":
            self._send(
                200,
                BOARD_HTML.encode("utf-8"),
                "text/html; charset=utf-8",
                {"Content-Security-Policy": BOARD_CSP},
            )
        elif sub == "spec":
            self._send(200, json.dumps(board.spec).encode("utf-8"), "application/json")
        elif sub.startswith("embed/"):
            # Agent-authored self-contained HTML, rendered by the board page in
            # an <iframe sandbox="allow-scripts"> — the sandbox (opaque origin,
            # no allow-same-origin) is the isolation boundary, so the document
            # itself is served WITHOUT a CSP: it may inline whatever
            # scripts/styles it needs (charts, mermaid, SVG). The answer POST
            # guard independently rejects opaque-origin requests, so embedded
            # scripts can never forge answers.
            try:
                idx = int(sub.split("/", 1)[1])
            except (ValueError, IndexError):
                self._send(404, b"not found", "text/plain")
                return
            if 0 <= idx < len(board.embeds):
                self._send(
                    200,
                    board.embeds[idx]["html"].encode("utf-8"),
                    "text/html; charset=utf-8",
                    {"Cache-Control": "no-store"},
                )
            else:
                self._send(404, b"no such embed", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def _answer_is_safe(self) -> bool:
        """CSRF/same-origin guard for the answer POST.

        Accept only when ALL hold:
          - Content-Type is application/json;
          - Sec-Fetch-Site is same-origin / none / absent (never cross-site);
          - Origin is absent OR equals this server's own origin.
        """
        ctype = self.headers.get("Content-Type", "")
        if "application/json" not in ctype.lower():
            return False
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site.strip().lower() not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if origin is not None:
            host = self.headers.get("Host", "")
            if origin.strip() != f"http://{host}":
                return False
        return True

    def do_POST(self) -> None:  # noqa: N802
        board_id, sub = _split_board_path(self.path)
        board = _BOARDS.get(board_id) if board_id else None
        if board is None or sub != "answer":
            self._send(404, b"not found", "text/plain")
            return
        if not self._answer_is_safe():
            self._send(403, b"forbidden", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._send(400, b"invalid json", "text/plain")
            return
        if isinstance(payload, dict):
            answers = payload.get("answers", payload)
        else:
            answers = {}
        board.answers = answers if isinstance(answers, dict) else {}
        self._send(204)


def _serve(board_id: str, board: _Board) -> tuple[ThreadingHTTPServer, str]:
    """Bind a loopback ThreadingHTTPServer on an ephemeral port; serve in a daemon
    thread. Returns (server, board_url)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BoardRequestHandler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/board/{urllib.parse.quote(board_id, safe='')}"
    thread = threading.Thread(
        target=server.serve_forever,
        name=f"firekeep-decision-{board_id}",
        daemon=True,
    )
    thread.start()
    return server, url


# --------------------------------------------------------------------------- #
# Poll loop                                                                     #
# --------------------------------------------------------------------------- #


def _bypass_notice() -> str:
    """Returned (instead of synthesizing a board) when personal mode is on. Checked
    per call, so a mid-session `/personal` toggle suppresses the board immediately —
    no Cortex synthesize call is made, nothing is sent."""
    return (
        "## Decision Board suppressed — personal mode\n\n"
        "Firekeep is bypassed for this session, so no board was synthesized and "
        "nothing was sent to Cortex. Ask your clarification inline instead. Run "
        "`firekeep personal off` (or /personal) to rejoin team mode."
    )


def _open_browser(url: str) -> bool:
    """Open the human's browser at ``url``; True only when a launch was observed.

    Field report (2026-07-18, "board does not launch"): ``webbrowser`` on macOS
    rides on osascript, which is TCC/Automation-fragile when this process was
    spawned by an agent app rather than a terminal — and its False return was
    silently ignored, leaving zero trace. On darwin go straight to
    ``/usr/bin/open`` (LaunchServices, no AppleScript); fall back to
    ``webbrowser``; hooklog every failure. Never raises.
    """
    try:
        if sys.platform == "darwin":
            # Scoped try: open(1) RAISING (TimeoutExpired on a hung LaunchServices,
            # PermissionError under a sandboxed spawn) must still fall through to
            # the webbrowser carrier below — only a rc=0 short-circuits.
            try:
                proc = subprocess.run(
                    ["/usr/bin/open", url], capture_output=True, timeout=10,
                )
                if proc.returncode == 0:
                    return True
                hooklog.log_failure(
                    "decision",
                    f"open(1) rc={proc.returncode}: {proc.stderr!r} — trying webbrowser",
                )
            except Exception as e:  # noqa: BLE001 — fall through to webbrowser
                hooklog.log_failure(
                    "decision", f"open(1) raised: {e!r} — trying webbrowser",
                )
        ok = bool(webbrowser.open(url))
        if not ok:
            hooklog.log_failure(
                "decision", f"webbrowser.open reported no browser for {url}",
            )
        return ok
    except Exception as e:  # noqa: BLE001 — a launch failure must never fail the tool
        try:
            hooklog.log_failure("decision", f"browser launch raised: {e!r}")
        except Exception:  # noqa: BLE001
            pass
        return False


def _pending(board: _Board) -> dict:
    out = {
        "status": "pending",
        "board_id": board.board_id,
        "board_url": board.url,
        "next": (
            "WAIT for the human: keep calling decision_board_check(board_id) — "
            "each call long-polls — until it returns the answers. Do not start "
            "work that depends on them."
        ),
    }
    if not board.opened and board.url:
        out["note"] = (
            f"The browser could not be opened automatically — tell the human to "
            f"open {board.url} manually to answer the board."
        )
    return out


async def _poll_board(board: _Board):
    """Non-blocking bounded poll for the human's answers.

    Answered -> shut the server down and return the rendered-markdown answers.
    Otherwise -> a pending envelope pointing at decision_board_check.
    """
    deadline = time.monotonic() + _poll_seconds()
    while True:
        if board.answers is not None:
            _shutdown_board(board)
            answers = board.answers
            _BOARDS.pop(board.board_id, None)
            return render_answers(answers)
        if time.monotonic() >= deadline:
            return _pending(board)
        await anyio.sleep(_POLL_INTERVAL)


# --------------------------------------------------------------------------- #
# Tool cores (unit-testable without a live MCP client)                         #
# --------------------------------------------------------------------------- #


async def _run_decision_board(
    context: str,
    draft_questions: list | None = None,
    embeds: list | None = None,
    *,
    post_json: Callable[..., Any] = transport.post_json,
):
    """Synthesize a board via Cortex, serve it locally, open the browser, poll.

    Never raises on a Cortex/transport failure — falls back to a local degraded
    board. Headless environments skip the server and return inline text.
    Malformed ``embeds`` DO raise (ValueError): the agent authored them and a
    clear message beats a mysteriously bare board.
    """
    # Personal-mode guard FIRST: no Cortex call, no socket bound, nothing sent.
    if resolver.is_bypassed():
        return _bypass_notice()

    draft_questions = list(draft_questions or [])
    normalized_embeds = _normalize_embeds(embeds)
    _reap_expired()

    # Headless guard: no Cortex call, no socket bound.
    if _is_headless():
        spec = _local_degraded_spec(context, draft_questions)
        spec["headless"] = True
        text = _render_spec_inline(spec)
        if normalized_embeds:
            text += (
                f"\n_{len(normalized_embeds)} rich embed(s) were attached but cannot "
                f"be rendered headless — describe their content inline._\n"
            )
        return text

    spec: dict | None = None
    try:
        endpoint = resolver.resolve("cortex")
        body = {
            "context": context,
            "draft_questions": draft_questions,
            "agent_id": endpoint.headers.get("X-Agent-Id", "unknown"),
        }
        result = post_json(
            f"{endpoint.rest_base}/decision/synthesize",
            body,
            headers=endpoint.headers,
            timeout=_ingest_client_timeout(),
            verify=endpoint.verify,
        )
        if isinstance(result, dict):
            spec = result
    except resolver.ConfigMigrationConflict:
        # Ambiguous legacy connections are an operator decision, not a reason
        # to quietly synthesize against no server.
        raise
    except Exception:
        # ANY failure (config error, transport error, bad response) -> degraded.
        spec = None
    if spec is None:
        spec = _local_degraded_spec(context, draft_questions)

    board_id = spec.get("board_id") or secrets.token_urlsafe(16)
    spec["board_id"] = board_id
    _attach_embeds(spec, normalized_embeds)
    board = _Board(board_id, spec, embeds=normalized_embeds)

    try:
        server, url = _serve(board_id, board)
    except Exception:
        # Serving failed unexpectedly — never fail the agent; fall back to inline.
        spec["headless"] = True
        return _render_spec_inline(spec)

    board.server = server
    board.url = url
    _BOARDS[board_id] = board

    board.opened = _open_browser(url)

    return await _poll_board(board)


async def _run_decision_board_check(board_id: str):
    """Poll an already-started board for the human's answers.

    Unknown id -> ``{status: unknown}`` (distinct from pending). Known id ->
    the same answered / pending outcome as ``_run_decision_board``'s poll.
    """
    if resolver.is_bypassed():
        return _bypass_notice()
    _reap_expired()
    board = _BOARDS.get(board_id)
    if board is None:
        return {
            "status": "unknown",
            "board_id": board_id,
            "note": (
                "no such board in this process — it may have expired, already "
                "been answered, or was never started here"
            ),
        }
    return await _poll_board(board)


# --------------------------------------------------------------------------- #
# MCP entrypoint (thin wrappers)                                               #
# --------------------------------------------------------------------------- #


def main() -> int:
    # UTF-8 stdio before the MCP handshake — a board's context, questions and
    # answers are free text and routinely non-ASCII, and the Windows default
    # (cp1252) corrupts every such character on the wire. See
    # firekeep_client/stdio.py.
    force_utf8_stdio()

    # Resolve once before the MCP handshake so an ambiguous legacy config fails
    # this stdio server loudly instead of appearing as an ordinary degraded board.
    if not resolver.is_bypassed():
        try:
            resolver.load_config()
        except resolver.ConfigMigrationConflict as exc:
            print(f"firekeep-decision: config migration blocked — {exc}", file=sys.stderr)
            return 3
        except resolver.ConfigError:
            pass  # the board's established local-degraded path handles this

    # Imported lazily so the tool cores stay importable without mcp installed
    # and so the FastMCP server only spins up when actually run as a server.
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("firekeep-decision")

    @mcp.tool()
    async def decision_board(
        context: str,
        draft_questions: list[str] | None = None,
        embeds: list[dict] | None = None,
    ):
        """Open an interactive Decision Board in the human's browser for clarifications.

        Call this — instead of asking inline — whenever a clarification needs more
        than a couple of questions: the board retrieves prior knowledge, shows
        evidence, and lets the human answer several questions at once.

        Formatting: question text supports lightweight markdown — paragraphs,
        `**bold**`, `` `code` ``, fenced code blocks, and `-`/`1.` lists. Put
        answer OPTIONS on their own list lines, not crammed into one sentence.

        Rich embeds: pass ``embeds`` to show charts, diagrams, tables, or mockups
        alongside the questions. Each embed is a fully SELF-CONTAINED HTML document
        (inline all CSS/JS/SVG — no external URLs; the sandbox has no network
        guarantees) rendered in a sandboxed iframe:
        ``{"html": "<!doctype html>...", "title": "Throughput by option",
        "question": 0, "height": 360}`` — ``question`` is an index into
        draft_questions (omit/null for a board-level embed shown above the
        questions); ``height`` is CSS pixels (120–2000). Max 512KB per embed.

        Poll contract: this returns EITHER the human's answers (markdown) if they
        submit quickly, OR ``{status: "pending", board_id, board_url, next}``.
        On pending, WAIT for the human: keep calling
        ``decision_board_check(board_id)`` — each call long-polls — until it
        returns the answers; do not start work that depends on them. If the
        response carries a ``note`` that the browser could not be opened, give
        the human the ``board_url`` to open manually.

        Args:
            context: what you're trying to decide / why you're asking.
            draft_questions: the specific questions to put on the board.
            embeds: optional rich visuals (self-contained HTML), see above.
        """
        return await _run_decision_board(context, draft_questions or [], embeds)

    @mcp.tool()
    async def decision_board_check(board_id: str):
        """Collect answers from a Decision Board started earlier via ``decision_board``.

        Use the ``board_id`` returned by ``decision_board``'s pending response.
        Returns the submitted answers (markdown) once the human has submitted,
        ``{status: "pending", ...}`` if they haven't yet — keep calling this in a
        loop (each call long-polls) until the answers arrive; do not start work
        that depends on them. ``{status: "unknown"}`` means the board_id isn't
        known to this process (expired/answered/never started) — only then fall
        back to asking inline.
        """
        return await _run_decision_board_check(board_id)

    mcp.run()
    return 0


if __name__ == "__main__":
    main()
