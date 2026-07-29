"""Adapter ABC + non-clobbering merge helpers (stdlib-only).

Adapters render each runtime's native MCP/hook config ONCE at install, merging ONLY
firekeep-owned keys so foreign MCP servers and foreign hooks SURVIVE (fixes today's
settings["hooks"] = {...} wholesale-overwrite bug in local-setup.*).
"""
from __future__ import annotations

import configparser
import json
import re
import sys
from abc import ABC, abstractmethod
from pathlib import Path

SERVICES = ("cortex", "bridge", "sentinel", "relay")
FIREKEEP_MCP_KEYS = (
    "firekeep-cortex", "firekeep-bridge", "firekeep-sentinel", "firekeep-relay",
    "firekeep-symdex", "firekeep-decision",
)
FIREKEEP_ENV_KEYS = ("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS",)
# Stable, venv-independent substring identifying a firekeep-owned hook command (used at unrender).
# No trailing dot: the rendered command is the dispatcher form `-m firekeep_client.hooks <core>`
# (module name, then core as a CLI arg) — a bare trailing-dot marker would no longer match.
HOOK_MARKER = "firekeep_client.hooks"

# The RETIRED bash hook layer (local-setup.sh era). These scripts no longer exist in the
# repo, so a machine upgraded from that installer opens every session with a "No such file
# or directory" hook error while the real hook core ALSO fires — two layers, one broken.
# They are firekeep-owned, not foreign: render() replaces them, unrender() removes them.
# Deliberately NOT listed: the legacy PreCompact `echo` hook. It still works, the kit
# renders no PreCompact hook of its own, and silently deleting a working behavior is worse
# than leaving one tidy artifact behind.
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
    # doubled distill enqueues), while the predecessor half points at a profile
    # that no longer resolves.
    "nexus_client.hooks",
)
# Retired by the resolver: URL/auth/TLS come from the active ~/.firekeep/config profile now.
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
    "nexus-cortex", "nexus-bridge", "nexus-sentinel", "nexus-relay",
    "nexus-symdex", "nexus-decision",
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


def shim_servers(venv_bin: Path) -> dict[str, tuple[str, list[str]]]:
    """Canonical firekeep MCP servers as (command, args) with ABSOLUTE venv script paths.
    Every HTTP service is reached via `firekeep-shim --service <svc>`. Two servers are
    stdio-local (their own console-scripts, NEVER through the shim, no --service) and
    ALWAYS included — firekeep-symdex (code intelligence) and firekeep-decision (clarification
    board): both are always-on client capabilities, not opt-in."""
    shim = console_script_path(venv_bin / "firekeep-shim")
    servers: dict[str, tuple[str, list[str]]] = {}
    for svc in SERVICES:
        servers[f"firekeep-{svc}"] = (shim, ["--service", svc])
    servers["firekeep-symdex"] = (console_script_path(venv_bin / "firekeep-symdex"), [])
    servers["firekeep-decision"] = (console_script_path(venv_bin / "firekeep-decision"), [])
    return servers


def hook_command(venv_bin: Path, core: str, *, extra_args: str = "") -> str:
    """Absolute command invoking the stdlib hook DISPATCHER (firekeep_client/hooks/__main__.py):
    `<venv>/python -m firekeep_client.hooks <core> [extra_args]`. The dispatcher is what actually
    reads stdin and calls the core's run() -- `-m firekeep_client.hooks.<core>` (importing the
    core module directly) has no `__main__` and would exit 0 without ever running the hook.
    `extra_args` is an opaque, already-formatted string appended verbatim (e.g. the claude
    adapter's pre_tool `--block-exit 2` remap)."""
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
    return cmd


def read_pin(runtime: str) -> str | None:
    """The profile pinned for `runtime` ([pins] in ~/.firekeep/config), or None. This is
    render()'s ONLY config dependency — introduced for per-runtime pins (2026-07-13).
    A missing/unreadable/malformed config (fresh machine, unrender-after-wipe, botched
    hand-edit) renders UNPINNED rather than failing the install. Charset guarantee: pinned_profile() only returns
    ^[A-Za-z0-9_-]+$ names, so rendered hook strings need no shell quoting."""
    from firekeep_client import resolver  # local import: keeps base import-light

    try:
        cfg = resolver.load_config()
    except (resolver.ConfigError, configparser.Error):
        # ConfigError covers a missing/unreadable file, but a present-and-MALFORMED INI
        # escapes load_config as a raw configparser.Error (ParsingError,
        # MissingSectionHeaderError, ...) — per the docstring that must render unpinned,
        # not fail the install.
        return None
    return resolver.pinned_profile(cfg, runtime)


def read_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


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
        """Write this runtime's native config: MCP servers wired to
        `{venv_bin}/firekeep-shim --service <svc>` plus the always-on stdio-local
        firekeep-symdex and firekeep-decision, and (where supported) lifecycle hooks."""
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
INSTRUCTIONS_BEGIN = "<!-- firekeep:instructions:begin — firekeep-owned block, do not edit; re-rendered by `firekeep install` -->"
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
  `ctx_get_shadow` before asking the user to repeat themselves.

**Write as you go, not at the end.**
- `ctx_update` after each meaningful step: category `plan` | `decision` |
  `file` (key=path) | `progress`. Three or more actions without one means you are
  behind — do it now.
- `memory_learn` the moment a fix works or a decision is made. Include what you
  tried first and why it failed; that is the part that saves the next session.
- `skill_create(trigger, symptoms, steps, gotchas, domain)` after a hard-won fix or
  a reusable technique. You hold the session context; the server does not
  synthesize this for you.
- Secrets (passwords, tokens, keys, connection strings) → `vault_store`, NEVER
  `memory_learn`. Non-secret operational facts (IPs, URLs, hostnames, paths) →
  `memory_learn(namespace="infrastructure")`.
- `ctx_complete_session(outcome=...)` when the work is done. Skipping it discards
  the session.
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


# Short form for the MCP `initialize` handshake (FastMCP's `instructions=`).
# Highest reach per line in the whole fix: it is served by the SERVER, so it
# arrives at every MCP client with no adapter involvement — including Codex, and
# including a user who deletes the rendered block from their CLAUDE.md. It is paid
# for once per session rather than per request, but keep it tight regardless.
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
"""


def upsert_marked_block(existing: str, content: str) -> str:
    """Replace the firekeep-owned marker block in `existing`, or append one.

    Only text BETWEEN the markers is ever touched — the user's own content is
    preserved byte-for-byte on both sides. Idempotent: rendering twice yields
    the same file.
    """
    block = f"{INSTRUCTIONS_BEGIN}\n{content}{INSTRUCTIONS_END}\n"
    begin = existing.find(INSTRUCTIONS_BEGIN)
    end = existing.find(INSTRUCTIONS_END)
    if begin != -1 and end != -1 and end > begin:
        after = existing[end + len(INSTRUCTIONS_END):]
        return existing[:begin] + block + after.lstrip("\n")
    if existing and not existing.endswith("\n"):
        existing += "\n"
    sep = "\n" if existing else ""
    return existing + sep + block


def strip_marked_block(existing: str) -> str:
    """Remove the firekeep-owned marker block; everything else survives unchanged."""
    begin = existing.find(INSTRUCTIONS_BEGIN)
    end = existing.find(INSTRUCTIONS_END)
    if begin == -1 or end == -1 or end < begin:
        return existing
    after = existing[end + len(INSTRUCTIONS_END):]
    return existing[:begin].rstrip("\n") + ("\n" if existing[:begin].strip() else "") + after.lstrip("\n")
