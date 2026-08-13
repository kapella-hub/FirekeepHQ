"""Adapter ABC + non-clobbering merge helpers (stdlib-only).

Adapters render each runtime's native MCP/hook config ONCE at install, merging ONLY
firekeep-owned keys so foreign MCP servers and foreign hooks SURVIVE (fixes today's
settings["hooks"] = {...} wholesale-overwrite bug in local-setup.*).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from abc import ABC, abstractmethod
from pathlib import Path

SERVICES = ("cortex", "bridge", "sentinel", "relay")
FIREKEEP_MCP_KEYS = ("firekeep",)
FIREKEEP_ENV_KEYS = ("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS",)
# Stable, venv-independent substring identifying a firekeep-owned hook command (used at unrender).
# No trailing dot: the rendered command is the dispatcher form `-m firekeep_client.hooks <core>`
# (module name, then core as a CLI arg) — a bare trailing-dot marker would no longer match.
HOOK_MARKER = "firekeep_client.hooks"

# The RETIRED bash hook layer (local-setup.sh era). These scripts no longer exist in the
# repo, so a machine upgraded from that installer opens every session with a "No such file
# or directory" hook error while the real hook core ALSO fires — two layers, one broken.
# They are firekeep-owned, not foreign: render() replaces them, unrender() removes them.
# Deliberately NOT listed: the legacy PreCompact `echo` hook. It still works, and
# the kit's own PreCompact group (rendered since the precompact core landed) is a
# SEPARATE, marker-identified group that coexists with it — so silently deleting a
# working behavior is still worse than leaving one tidy artifact behind.
# DO NOT RENAME THE STRINGS IN THIS BLOCK. They name artifacts left by PREVIOUS
# generations of the kit, so they must keep spelling the OLD thing forever. A
# repo-wide find-and-replace is exactly how this cleanup breaks: the predecessor
# rename turned LEGACY_ENV_KEYS into FIREKEEP_*_URL, which no machine has ever
# had, silently disarming the migration while every test stayed green. Renaming a
# legacy token is not a rename, it is a deletion.
LEGACY_HOOK_MARKERS = (
    # Generation 1 — the retired bash hook layer.
    "scripts/briefing.sh",
    "scripts/debrief.sh",
    "scripts/multi-agent-poll.sh",
    "scripts/multi-agent-precheck.sh",
    "scripts/multi-agent-postaction.sh",
    # Generation 2 — the predecessor Python kit. Same hazard as generation 1 and
    # the same remedy: without this, an upgraded machine keeps BOTH hook layers
    # and fires every lifecycle event twice (doubled presence registration,
    # doubled distill enqueues), while the predecessor half points at a config
    # that no longer resolves.
    "nexus_client.hooks",
)
# Retired by the resolver: URL/auth/TLS come from ~/.firekeep/config now.
# No client-kit code reads these; left in place they only mislead whoever reads the file next.
LEGACY_ENV_KEYS = (
    "NEXUS_CORTEX_URL",
    "NEXUS_BRIDGE_URL",
    "NEXUS_SENTINEL_URL",
    "NEXUS_RELAY_URL",
)
# Predecessor MCP server keys. Firekeep registers its own six under firekeep-*, so
# without this an upgraded machine carries TWELVE servers — six of them pointing at
# a config path that no longer exists, failing to connect on every session start.
LEGACY_MCP_KEYS = (
    "firekeep-cortex", "firekeep-bridge", "firekeep-sentinel", "firekeep-relay",
    "firekeep-symdex", "firekeep-decision",
    "nexus-cortex", "nexus-bridge", "nexus-sentinel", "nexus-relay",
    "nexus-symdex", "nexus-decision",
)

# Generation 2's instruction block, upserted into the user's global CLAUDE.md
# under the predecessor product's markers. Measured on a live machine 2026-07-30:
# 3,214 chars, 0.75-similar to FIREKEEP_INSTRUCTIONS (a near-duplicate of a
# SUBSET — firekeep's block carries a memory-protocol section this one lacks).
# The sibling `Agent Guidelines` block -- a distinct marker pair the predecessor
# also wrote to the same file -- is deliberately NOT listed here: at 0.03
# similarity it is not a duplicate, it is content the user still has, and
# removing it would be a plain deletion of their information.
# DO NOT RENAME (see the warning above): renaming these disarms the migration on
# every machine that actually has the block.
LEGACY_INSTRUCTION_MARKERS = (
    ("<!-- nexus:instructions:begin", "<!-- nexus:instructions:end -->"),
)


def console_script_path(path: Path) -> str:
    """Absolute path to a venv console-script/interpreter, platform-aware: pip installs
    Windows console-script entry points (and the interpreter itself) as `<name>.exe` under
    `Scripts\\`; the extensionless name may not resolve via CreateProcess depending on the
    invoking launcher. Appends `.exe` on win32 only; no-op elsewhere or if already suffixed.
    Reads `sys.platform` at call time so tests can `monkeypatch.setattr(sys, "platform", ...)`."""
    text = str(path)
    if sys.platform == "win32" and not text.lower().endswith(".exe"):
        return text + ".exe"
    return text


def shim_servers(venv_bin: Path, runtime: str | None = None) -> dict[str, tuple[str, list[str]]]:
    """The one local Firekeep MCP gateway entry rendered into every runtime.

    `runtime` (each adapter passes its own `.name`) renders `firekeep gateway
    --runtime <name>` so the gateway process — and the shim children it spawns —
    know which runtime launched them and can attach the X-Firekeep-* attribution
    headers (round-2 measurement contract). None (old rendered configs, direct
    callers) renders the bare `gateway` form: no runtime identity, no headers."""
    args = ["gateway"]
    if runtime:
        args += ["--runtime", runtime]
    return {"firekeep": (console_script_path(venv_bin / "firekeep"), args)}


def hook_command(venv_bin: Path, core: str, *, extra_args: str = "", runtime: str | None = None) -> str:
    """Absolute command invoking the stdlib hook DISPATCHER (firekeep_client/hooks/__main__.py):
    `<venv>/python -m firekeep_client.hooks <core> [extra_args]`. The dispatcher is what actually
    reads stdin and calls the core's run() -- `-m firekeep_client.hooks.<core>` (importing the
    core module directly) has no `__main__` and would exit 0 without ever running the hook.
    `extra_args` is an opaque, already-formatted string appended verbatim (e.g. the claude
    adapter's pre_tool `--block-exit 2` remap). `runtime` (the adapter's `.name`) appends
    `--runtime <name>` so the hook cores' server calls carry the X-Firekeep-* attribution
    headers; None keeps the old form (dispatcher defaults to no runtime — no headers)."""
    python = console_script_path(venv_bin / "python")
    # Hook commands are SHELL STRINGS (settings.json {"type":"command"}) that Claude Code
    # runs through bash -- on Windows too (`/usr/bin/bash -c ...`). In bash an unquoted
    # backslash is an escape char, so a native Windows path C:\Users\mogan\.firekeep\... has its
    # separators eaten and collapses to C:Usersmogan.firekeep... -> "command not found". Render
    # forward slashes: valid for both bash and Windows CreateProcess, and immune to escaping.
    python = python.replace("\\", "/")
    # A venv under e.g. C:/Users/First Last/ would still word-split unquoted. Quote when the
    # path contains whitespace (double quotes work on both cmd and POSIX shells).
    if any(ch.isspace() for ch in python):
        python = f'"{python}"'
    cmd = f"{python} -m {HOOK_MARKER} {core}"
    if extra_args:
        cmd = f"{cmd} {extra_args}"
    if runtime:
        cmd = f"{cmd} --runtime {runtime}"
    return cmd


def read_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def write_text_if_changed(path: Path, body: str) -> bool:
    """Write `body` to `path` only if it differs from what is already there.
    Returns True if a write happened.

    Rewriting byte-identical content still moves mtime, and that is not free.
    `firekeep update` re-execs `firekeep install`, which re-renders
    `~/.claude/CLAUDE.md` and `~/.claude/settings.json` — and background
    auto-update is on by default, so this happens MID-SESSION on a customer's
    machine. Those files sit in the prompt prefix; a host that re-reads a
    rendered instruction file because its mtime moved rebuilds that prefix and
    invalidates the prompt cache, re-billing the conversation at full rate for a
    zero-byte change. Whether a given host does that cannot be determined from
    this repo, which is exactly why touching mtime for nothing is indefensible.

    Fails toward writing: if the existing file cannot be read or decoded we
    cannot prove it matches, so we write. Never skips a real change.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and path.read_text(encoding="utf-8") == body:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    path.write_text(body, encoding="utf-8")
    return True


def write_json(path: Path, data: dict) -> None:
    write_text_if_changed(path, json.dumps(data, indent=2) + "\n")


def merge_owned(existing: dict, owned: dict) -> dict:
    """Dict-merge: set each owned key, leave foreign keys untouched. Returns existing."""
    existing.update(owned)
    return existing


def drop_owned(existing: dict, keys) -> None:
    """Remove only the named keys (no-op if absent). Foreign keys survive."""
    for k in keys:
        existing.pop(k, None)


def _is_firekeep_hook(entry: dict) -> bool:
    command = entry.get("command", "")
    if HOOK_MARKER in command:
        return True
    return any(marker in command for marker in LEGACY_HOOK_MARKERS)


def _is_firekeep_group(group: dict) -> bool:
    return any(_is_firekeep_hook(h) for h in group.get("hooks", []))


def upsert_hook_group(hooks: dict, event: str, group: dict) -> None:
    """Claude-shaped hooks map (event -> [ {matcher?, hooks:[{command}]} ]).
    Collapse ALL firekeep groups for the event down to the one rendered group, at the position
    of the first one; append if there were none. Foreign groups preserved.

    Collapsing all of them (rather than replacing just the first) is what migrates a machine
    carrying BOTH layers — a retired bash group and a current hook-core group, in that order.
    Replacing only the first would overwrite the bash group and leave the current one behind
    as a duplicate, firing every hook twice."""
    lst = hooks.setdefault(event, [])
    indices = [i for i, g in enumerate(lst) if _is_firekeep_group(g)]
    if not indices:
        lst.append(group)
        return
    for i in reversed(indices):
        del lst[i]
    lst.insert(indices[0], group)


def prune_hook_groups(hooks: dict) -> None:
    """Remove firekeep groups from every event; drop now-empty events. Foreign groups survive."""
    for event in list(hooks.keys()):
        kept = [g for g in hooks[event] if not _is_firekeep_group(g)]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]


def upsert_flat_hook(lst: list, entry: dict) -> None:
    """kiro-shaped inline hooks (event -> [ {command, matcher?} ]).
    Replace the existing firekeep entry in place, else append. Foreign entries preserved."""
    for i, h in enumerate(lst):
        if _is_firekeep_hook(h):
            lst[i] = entry
            return
    lst.append(entry)


def prune_flat_hooks(hooks: dict) -> None:
    for event in list(hooks.keys()):
        kept = [h for h in hooks[event] if not _is_firekeep_hook(h)]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]


def upsert_block(text: str, block: str, start: str, end: str) -> str:
    """Text-block merge for non-JSON configs (TOML). Replace region between markers
    (inclusive), else append. Uses a function replacement so backslashes in `block`
    (Windows paths) are treated literally, never as regex group refs."""
    wrapped = f"{start}\n{block.rstrip()}\n{end}"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if pattern.search(text):
        return pattern.sub(lambda _m: wrapped, text)
    if not text:
        return wrapped + "\n"
    sep = "" if text.endswith("\n") else "\n"
    return f"{text}{sep}\n{wrapped}\n"


def strip_block(text: str, start: str, end: str) -> str:
    """Remove the marked block (inclusive) and surrounding blank lines. Foreign text survives."""
    pattern = re.compile(r"\n*" + re.escape(start) + r".*?" + re.escape(end) + r"\n*", re.DOTALL)
    return pattern.sub("\n", text).lstrip("\n")


class Adapter(ABC):
    """Renders a runtime's native config, merging ONLY firekeep-owned keys (non-clobbering)."""

    name: str

    @abstractmethod
    def render(self, *, venv_bin: Path) -> None:
        """Write this runtime's native config with one local Firekeep gateway."""
        raise NotImplementedError

    @abstractmethod
    def unrender(self) -> None:
        """Remove only firekeep-owned keys (inverse of render). Foreign entries survive."""


# --- instruction-layer rendering (decision-board trigger) ---------------------
# The decision_board MCP tool only fires if an instruction layer TELLS the agent
# to prefer it over inline questions — a tool description alone never triggers
# proactive use (verified 2026-07-14: the sentence existed only in one project's
# CLAUDE.md, so every other project/runtime never opened a board). The claude
# adapter upserts this as a marker-delimited block inside the user's global
# ~/.claude/CLAUDE.md; kiro renders it as a firekeep-owned steering file.
# BEGIN is matched by PREFIX everywhere (upsert/strip/doctor): the live begin
# line carries a stamped tail (`v=<wheel version> h=<hash> — …`) that changes
# per release, and the pre-0.1.41 unstamped line carried a prose tail — the
# find_legacy_block_bounds precedent. Prefix matching is what makes a legacy
# unstamped block upsert/strip identically to a stamped one, so an old file is
# migrated to the stamped form on its next render. END stays an exact match.
# The stamped INSTRUCTIONS_BEGIN itself is defined below, after the hash
# constants it embeds.
INSTRUCTIONS_BEGIN_PREFIX = "<!-- firekeep:instructions:begin"
INSTRUCTIONS_END = "<!-- firekeep:instructions:end -->"

DECISION_INSTRUCTIONS = """\
## Firekeep Decision Board

When a clarification needs more than a couple of questions — requirements, scope,
preferences, anything where several answers shape the work ahead — call the
`decision_board(context, draft_questions)` MCP tool (firekeep-decision server) instead of
asking the questions inline. It opens a browser board for the human with evidence
retrieved from team memory, and lets them answer everything at once.

- **Format the questions.** Question text renders lightweight markdown — put answer
  options on their own `-` list lines (never crammed into one sentence), use
  `**bold**` for the decision itself and `` `code` `` for identifiers.
- **Show, don't describe.** Pass `embeds` for charts, diagrams, comparison tables, or
  mockups: each is a fully self-contained HTML document (inline ALL CSS/JS/SVG — no
  external URLs) rendered in a sandboxed iframe. Shape:
  `{"html": "<!doctype html>...", "title": "...", "question": 0, "height": 360}` —
  `question` indexes draft_questions; omit it for a board-level visual.
- A single quick question: just ask inline as usual.
- It returns `{status: "pending", board_id, board_url}` while the human is answering
  in the browser. **Wait for the board**: keep calling `decision_board_check(board_id)`
  in a loop — each call long-polls (~24s) server-side — until it returns the answers.
  Do not start work that depends on them, and do not re-ask the questions inline.
  If the response carries a `note` that the browser could not be opened, give the
  human the `board_url` to open manually.
- Treat the board as dead ONLY when a check returns `status: "unknown"`
  (expired/reaped) — then ask the questions inline.
- Headless / no browser: the tool degrades to returning the questions as text — ask
  them inline.
"""


KNOWLEDGE_INGEST_INSTRUCTIONS = """\
## Firekeep Knowledge Ingest (client-side)

To add a document, runbook, or web page to the team knowledge base, do the
intelligence yourself and upload the results — don't depend on server-side
classification (it needs a generation model the server may not have):

1. Get the content: paste text, or fetch the URL (WebFetch / browser tools).
2. `corpus_ingest(content, source_name)` — stores the full document so the whole
   team can find it via `memory_recall` (searchable immediately, needs only
   embedding).
3. Read the document and identify each distinct, self-contained procedure or
   runbook. For each one, call `skill_create(trigger, symptoms, steps, gotchas,
   domain, project, status)` — one team-visible skill per procedure. Pass
   `status="draft"` when a human should review before the team relies on it (it
   lands in the dashboard review queue, excluded from recall until approved), or
   the default `status="active"` to publish immediately.

You are the classifier: this makes document → searchable corpus + reusable
skills work everywhere, including a server with no generation model. The
`knowledge_ingest` / `knowledge_ingest_url` tools do the same server-side and are
fine on a deploy that has a generation model, but prefer the client-side flow
above when you're unsure.
"""


MEMORY_INSTRUCTIONS = """\
## Firekeep Memory

You have a persistent team memory. Most "I don't know" answers are already in it.

**Recall before you answer — not knowing something IS the trigger.**
Call `memory_recall(task=<the user's request, verbatim>)` BEFORE answering when:
- The user names a host, IP, path, service, credential, deploy target or convention
  you cannot name from THIS conversation — "my VPS", "our server", "the staging box".
- They use history words: "again", "still", "last time", "how did we", "like before".
- You are starting a non-trivial task, or you just hit an error.

Never say "I don't know" or "I don't have access to that" about the user's own
systems before calling it once. A miss costs a second; an unasked question costs
the session.

- A recalled memory naming a vault key rather than a value → `vault_retrieve(key)`.
- Operational or repeated-failure task → also `skill_recall(task)`. The
  session-start briefing only ever matched your ORIGINAL goal, never what the user
  asked on turn 7.
- Your own earlier plan or decisions missing from context (after compaction) →
  `ctx_get_shadow` before asking the user to repeat themselves. Pass
  `since=<shadow_cursor>` ONLY if the earlier shadow is still visible in your
  context; if you are unsure, omit it — omitting it is always correct.

**Write as you go, not at the end.**
- `ctx_update` after each meaningful step: category `plan` | `decision` |
  `file` (key=path) | `progress`. Three or more actions without one means you are
  behind — do it now.
- `memory_learn` the moment a fix works or a decision is made. Include what you
  tried first and why it failed; that is the part that saves the next session.
- `memory_feedback(memory_ids, useful, comment)` when recalled knowledge shaped
  what you DID — you acted on it and it held, or it sent you the wrong way.
  Report only knowledge you acted on, not everything recall showed you; the
  signal feeds ranking, so a thumb on unused results is noise.
- `skill_create(trigger, symptoms, steps, gotchas, domain)` after a hard-won fix or
  a reusable technique. You hold the session context; the server does not
  synthesize this for you.
- Secrets (passwords, tokens, keys, connection strings) → `vault_store`, NEVER
  `memory_learn`. Non-secret operational facts (IPs, URLs, hostnames, paths) →
  `memory_learn(namespace="infrastructure")`.
- `ctx_complete_session(outcome=...)` when the work is done. Skipping it discards
  the session.

**Declare consequential actions before taking them.** Before a risky or
hard-to-reverse action (schema change, deletion, deploy, bulk edit), call
`action_before(action_type, target, intent, success_criteria, confidence)` —
state what you expect and how sure you are. After it settles, call
`action_after(action_id, outcome)` with what actually happened. Your stated
confidence is scored against reality (calibration); routine single-file edits
are already gated by hooks and need no declaration.
"""


# The full firekeep-owned instruction block rendered into each runtime's instruction
# surface (Claude global CLAUDE.md, kiro steering, opencode AGENTS.md, codex
# AGENTS.md). Append new sections here.
#
# MEMORY_INSTRUCTIONS is first ON PURPOSE. It is the one section that governs
# ordinary turns; the other two fire on specific, rarer situations.
#
# WHY THIS SECTION EXISTS AT ALL — the failure it fixes, so nobody trims it back:
# a user asked their agent "deploy to my vps" and was told the agent did not know
# what the VPS was. The user said "look at your memories", the agent called
# memory_recall, and the answer was the FIRST result at 100% confidence, complete
# with the IP, ssh-as-root and the checkout path. Storage and retrieval were
# perfect. Nothing triggered them.
#
# It survived because this block previously held only the decision-board and
# knowledge-ingest sections: no "recall before", no memory_learn, no
# vault_retrieve. The session-start briefing does say "then memory_recall", but
# ONCE, before the agent has anything to recall against — it cannot look up a VPS
# before the user has mentioned one. And the author's own machine behaved better
# only because he had hand-written these rules into his personal CLAUDE.md years
# earlier, so the gap was invisible from the inside.
#
# The wording is deliberate in two places, and both are load-bearing:
#   1. The recall trigger is an OBSERVABLE TEST against the current turn ("can I
#      name this thing from THIS conversation?"), not an exhortation to remember.
#      "Recall when relevant" has no edge a model can evaluate.
#   2. The ctx_update rule is COUNTABLE ("three or more actions"). "As you work"
#      is unfalsifiable, so it gets skipped.
#
# Do NOT solve this by rewording tool descriptions. memory_recall's own
# description already states its trigger correctly and still does not fire; this
# repo proved the same thing for decision_board in client 0.1.11, which is the
# reason DECISION_INSTRUCTIONS exists.
FIREKEEP_INSTRUCTIONS = (
    f"{MEMORY_INSTRUCTIONS}\n\n{DECISION_INSTRUCTIONS}\n\n{KNOWLEDGE_INGEST_INSTRUCTIONS}"
)


# Short form for the MCP `initialize` handshake. Since the gateway collapsed the
# per-service shims, the ONLY handshake text an agent ever receives is
# GATEWAY_INSTRUCTIONS below (gateway.py discards backend `instructions=` during
# discovery) — so this block, which GATEWAY_INSTRUCTIONS embeds, IS the second
# delivery channel. The action_before paragraph was added here in 0.1.41 (round-2
# measurement contract, Correction 2): f23133a put it only in Cortex's FastMCP
# `_INSTRUCTIONS`, which no kit runtime ever sees, so the armed 0/32 experiment's
# "second channel" was dead until this release. It is paid for once per session
# rather than per request, but keep it tight regardless.
MCP_SERVER_INSTRUCTIONS = """\
Firekeep — persistent team memory for agents.

Recall BEFORE answering, and treat not knowing as the trigger: if the user names a
host, IP, path, service, credential or convention you cannot name from the current
conversation ("my VPS", "our server"), or uses history words ("again", "still",
"last time", "how did we"), call memory_recall(task=<their request>) first. Never
claim you don't know about the user's own systems before calling it once. If a
result names a vault key, follow up with vault_retrieve.

Write as you go: ctx_update after each meaningful step, memory_learn the moment a
fix works (including what failed first), skill_create after a hard-won fix,
ctx_complete_session when done. Secrets go to vault_store, never memory_learn.

When recalled knowledge shaped what you DID — you acted on it and it held, or it
sent you the wrong way — call memory_feedback(memory_ids=[...], useful=...) with a
one-line comment. Report only knowledge you acted on, not everything you saw.

Before a risky or hard-to-reverse action (deletion, deploy, schema change), call
action_before(action_type, target, intent, success_criteria, confidence); after it
settles, call action_after(action_id, outcome) — stated confidence is scored against reality.
"""

GATEWAY_INSTRUCTIONS = f"""\
{MCP_SERVER_INSTRUCTIONS.rstrip()}

When a clarification needs more than a couple of questions, call decision_board
instead of asking inline. If it returns pending, keep calling decision_board_check
until the human answers.
"""


def _hash12(text: str) -> str:
    """sha256(text utf-8), first 12 hex chars — the contract's hash shape."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# Content hashes of the two instruction artifacts (round-2 measurement contract).
# RENDERED covers ONLY the text BETWEEN the block markers — exactly the `content`
# string upsert_marked_block receives — so the stamped BEGIN line never hashes
# itself and re-rendering from the same wheel stays byte-identical. GATEWAY
# covers the handshake text served fresh from the running wheel every session.
RENDERED_INSTRUCTIONS_HASH = _hash12(FIREKEEP_INSTRUCTIONS)
GATEWAY_INSTRUCTIONS_HASH = _hash12(GATEWAY_INSTRUCTIONS)

# The stamped BEGIN marker: h= the hash of the content it wraps. Deliberately
# NO v= (wheel version): the stamp must be a pure function of the CONTENT, or
# every release would rewrite the rendered files even when the instruction
# text is unchanged — moving mtime on files that sit in the customer's prompt
# prefix, the exact cost write_text_if_changed's docstring calls indefensible
# (external review 2026-08-12). Which wheel rendered it is recoverable from
# the hash; version attribution rides X-Firekeep-Client, not the file.
INSTRUCTIONS_BEGIN = (
    f"{INSTRUCTIONS_BEGIN_PREFIX} h={RENDERED_INSTRUCTIONS_HASH}"
    " — firekeep-owned block, do not edit; re-rendered by `firekeep install` -->"
)


def _line_anchored_find(text: str, needle: str, start: int = 0) -> int:
    """First occurrence of `needle` at the START OF A LINE, or -1.

    Marker matching must be line-anchored: prose that merely MENTIONS the
    begin-marker prefix mid-sentence must never be mistaken for the block —
    with an unanchored find, one render would swallow every user line between
    the mention and the real block's END (external review 2026-08-12)."""
    pos = text.find(needle, start)
    while pos > 0 and text[pos - 1] != "\n":
        pos = text.find(needle, pos + 1)
    return pos


def has_marked_begin(text: str) -> bool:
    """Whether a firekeep begin-marker line (stamped or legacy) is present."""
    return _line_anchored_find(text, INSTRUCTIONS_BEGIN_PREFIX) != -1


def _find_marked_block(text: str) -> tuple[int, int] | None:
    """Bounds of the firekeep-owned block: (start of BEGIN, start of END).

    BEGIN matches by PREFIX, line-anchored, so stamped (0.1.41+) and legacy
    unstamped begin lines are found identically; END matches in full, searched
    AFTER the begin so a stray END earlier in the file can never invert the
    span. None when the pair is incomplete — the ORPHANED-BEGIN case (user
    deleted the END marker) is handled by upsert/strip themselves, not here,
    because _extract_block_content must treat a broken block as absent."""
    begin = _line_anchored_find(text, INSTRUCTIONS_BEGIN_PREFIX)
    if begin == -1:
        return None
    end = text.find(INSTRUCTIONS_END, begin)
    if end == -1:
        return None
    return begin, end


def _orphaned_begin_span(text: str) -> tuple[int, int] | None:
    """Span of a begin-marker LINE that has no END marker after it, or None.

    The heal path for a user-damaged block: the old code APPENDED a second
    block below the orphan, and the next render's span then ran from the
    orphan to the appended block's END — swallowing every user line between
    them on the second render (external review 2026-08-12, verified by
    execution). Replacing exactly the orphaned line heals in one render and
    can never claim user content: only the firekeep-owned marker line itself
    is inside the span."""
    begin = _line_anchored_find(text, INSTRUCTIONS_BEGIN_PREFIX)
    if begin == -1 or text.find(INSTRUCTIONS_END, begin) != -1:
        return None
    line_end = text.find("\n", begin)
    return begin, (len(text) if line_end == -1 else line_end)


def upsert_marked_block(existing: str, content: str) -> str:
    """Replace the firekeep-owned marker block in `existing`, or append one.

    Only text BETWEEN the markers is ever touched — the user's own content is
    preserved byte-for-byte on both sides. Idempotent: rendering twice yields
    the same file. A legacy UNSTAMPED block (pre-0.1.41 begin line) is found by
    the same prefix match and replaced by the stamped block — the migration
    path needs no separate code."""
    block = f"{INSTRUCTIONS_BEGIN}\n{content}{INSTRUCTIONS_END}\n"
    bounds = _find_marked_block(existing)
    if bounds is not None:
        begin, end = bounds
        after = existing[end + len(INSTRUCTIONS_END):]
        return existing[:begin] + block + after.lstrip("\n")
    orphan = _orphaned_begin_span(existing)
    if orphan is not None:
        # BEGIN without END: replace exactly the orphaned marker line. Any
        # leftover block body below it is indistinguishable from user content
        # without an END marker, so it is preserved — visible residue beats
        # silent deletion.
        begin, stop = orphan
        after = existing[stop:]
        return existing[:begin] + block + after.lstrip("\n")
    if existing and not existing.endswith("\n"):
        existing += "\n"
    sep = "\n" if existing else ""
    return existing + sep + block


def strip_marked_block(existing: str) -> str:
    """Remove the firekeep-owned marker block; everything else survives unchanged.
    Prefix-matched BEGIN: stamped and legacy unstamped blocks strip identically.
    An orphaned begin line (END deleted by hand) is removed too — it is
    firekeep-owned by definition; the content below it is not, and survives."""
    bounds = _find_marked_block(existing)
    if bounds is None:
        orphan = _orphaned_begin_span(existing)
        if orphan is None:
            return existing
        begin, end = orphan
        after = existing[end:]
        return existing[:begin].rstrip("\n") + ("\n" if existing[:begin].strip() else "") + after.lstrip("\n")
    begin, end = bounds
    after = existing[end + len(INSTRUCTIONS_END):]
    return existing[:begin].rstrip("\n") + ("\n" if existing[:begin].strip() else "") + after.lstrip("\n")


def rendered_block_stamp(text: str) -> str | None:
    """The h=<hash> claim stamped on the block's BEGIN line, or None when the
    line is legacy/unstamped (or there is no block). Lets doctor tell an intact
    older render (stamp == on-disk hash: stale) from a hand-edited block
    (stamp != on-disk hash: edited)."""
    begin = _line_anchored_find(text, INSTRUCTIONS_BEGIN_PREFIX)
    if begin == -1:
        return None
    line_end = text.find("\n", begin)
    line = text[begin:] if line_end == -1 else text[begin:line_end]
    match = re.search(r"\bh=([0-9a-f]{12})\b", line)
    return match.group(1) if match else None


def rendered_instructions_path(runtime_name: str) -> Path | None:
    """The file each runtime's adapter renders the instruction block into.

    Mirrors the adapters' own `_instructions_path`/`_steering_path` (kiro's is a
    whole-file steering doc, not a marker block — see _extract_block_content).
    Returns None for an unknown runtime name."""
    if runtime_name == "claude":
        return Path.home() / ".claude" / "CLAUDE.md"
    if runtime_name == "codex":
        return Path.home() / ".codex" / "AGENTS.md"
    if runtime_name == "kiro":
        return Path.home() / ".kiro" / "steering" / "firekeep-instructions.md"
    if runtime_name == "opencode":
        # Lazy import: opencode.py imports this module at its top, so a
        # module-level import here would be a cycle. _config_dir owns the
        # XDG_CONFIG_HOME resolution — duplicating it here would drift.
        from firekeep_client.adapters.opencode import _config_dir
        return _config_dir() / "AGENTS.md"
    return None


def _extract_block_content(text: str, runtime_name: str) -> str | None:
    """The exact content basis RENDERED_INSTRUCTIONS_HASH is defined over, read
    back from a rendered file: the text between the markers (claude/codex/
    opencode), or everything after the steering marker line (kiro's whole-file
    shape) — so a current file hashes equal to RENDERED_INSTRUCTIONS_HASH on
    every runtime. None when no block is present."""
    if runtime_name == "kiro":
        from firekeep_client.adapters.kiro import STEERING_MARKER  # lazy: cycle
        marker_line = f"<!-- {STEERING_MARKER} -->\n"
        start = text.find(marker_line)
        if start == -1:
            return None
        return text[start + len(marker_line):]
    bounds = _find_marked_block(text)
    if bounds is None:
        return None
    begin, end = bounds
    newline = text.find("\n", begin)
    if newline == -1 or newline >= end:
        return ""  # malformed begin line: hash the (empty) content honestly
    return text[newline + 1:end]


def read_rendered_instructions_hash(runtime_name: str) -> str | None:
    """Re-hash the instruction block actually ON DISK for `runtime_name`.

    Returns sha256[:12] of the block content, or None when the file/block is
    absent or unreadable. Deliberately hashes what is there rather than
    trusting the stamp — a hand-edited block reports its true hash."""
    path = rendered_instructions_path(runtime_name)
    if path is None:
        return None
    try:
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    content = _extract_block_content(text, runtime_name)
    if content is None:
        return None
    return _hash12(content)


def find_legacy_block_bounds(text: str, begin_prefix: str, end_marker: str) -> tuple[int, int] | None:
    """Locate a marker-delimited block whose BEGIN marker is matched by PREFIX (the
    live begin line carries a variable prose tail a later generation could reword —
    e.g. `<!-- nexus:instructions:begin — nexus-owned block, do not edit; ... -->`)
    and whose END marker is matched in full. Returns (start, stop) spanning the
    whole block including both markers, or None if the pair isn't present/ordered."""
    begin = text.find(begin_prefix)
    if begin == -1:
        return None
    end = text.find(end_marker, begin)
    if end == -1:
        return None
    return begin, end + len(end_marker)


def strip_span(text: str, begin: int, end: int) -> str:
    """Remove text[begin:end] and normalize surrounding blank lines the same way
    strip_marked_block does, so archiving a legacy block leaves the remaining
    prose looking hand-written rather than gapped."""
    before, after = text[:begin], text[end:]
    return before.rstrip("\n") + ("\n" if before.strip() else "") + after.lstrip("\n")
