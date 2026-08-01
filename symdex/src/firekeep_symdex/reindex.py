"""Headless one-shot index entry point: `python -m firekeep_symdex.reindex <path>`.

Why this exists: `server:main` (the stdio MCP server) was the package's ONLY entry
point, and a lifecycle hook cannot drive it — there is no MCP client on the other end
of a SessionStart hook, so `index_folder` was reachable only by an agent choosing to
call the tool. That is what made symdex's own SessionStart hook a nag instead of an
action: `claude-plugin/symdex/scripts/ensure-indexed.sh` prints "ACTION REQUIRED: call
index_folder" because printing was the only thing it could do.

This module is the missing seam. The client kit's background auto-index spawns it
(`firekeep_client.symdexindex`), and a human can run the exact same command to
reproduce what the hook did — the failure mode is inspectable rather than buried in a
detached process.

Deliberately NOT a console script: `python -m` is resolvable from `sys.executable`
alone, so the client can spawn it with no PATH dependency and no console-script
shim to keep in sync (`firekeep_client.autoupdate._firekeep_exe` needs the opposite
because `firekeep update` is also a user-facing command; this is not).

Defaults differ from the MCP tool on purpose:
  * `use_ai_summaries=False` — the tool defaults True, which bills an Anthropic/Gemini
    key per index. A background index the user did not ask for must not spend money
    or wait on a network round trip; docstring/signature fallback is enough for the
    symbol search this feeds.
  * `--incremental` opt-in, matching `index_folder`'s own default.
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    """Index a folder and print the result dict as JSON. Returns a process exit code:
    0 on success, 1 when indexing reported failure, 2 on an unexpected exception.

    The import is INSIDE main() so `--help` and argument errors don't pay for
    tree-sitter's import cost, and so an import failure surfaces as exit 2 with a
    readable message rather than a traceback at module load."""
    ap = argparse.ArgumentParser(
        prog="python -m firekeep_symdex.reindex",
        description="Index a local folder for symdex code intelligence (one-shot, headless).",
    )
    ap.add_argument("path", help="Path to the folder to index")
    ap.add_argument(
        "--incremental",
        action="store_true",
        help="Re-index only changed files when an index already exists",
    )
    ap.add_argument(
        "--storage-path",
        default=None,
        help="Index storage root (default: $CODE_INDEX_PATH, else ~/.code-index)",
    )
    ap.add_argument(
        "--ai-summaries",
        action="store_true",
        help="Generate AI symbol summaries (requires ANTHROPIC_API_KEY or GOOGLE_API_KEY)",
    )
    args = ap.parse_args(argv)

    try:
        from firekeep_symdex.tools.index_folder import index_folder

        result = index_folder(
            path=args.path,
            use_ai_summaries=args.ai_summaries,
            storage_path=args.storage_path,
            incremental=args.incremental,
        )
    except Exception as exc:  # noqa: BLE001 — report, never traceback
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 2

    print(json.dumps(result, default=str))
    return 0 if result.get("success", False) else 1


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess
    sys.exit(main())
