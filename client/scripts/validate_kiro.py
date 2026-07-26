"""Validate the rendered ~/.kiro/agents/firekeep.json against a real kiro-cli.

Non-interactive checks (schema + MCP recognition). The runtime pre-edit BLOCKING behavior
cannot be asserted here — see docs/KIRO-VALIDATION.md for the manual blocking probe and the
kiro-cli 2.12.1 finding (the hook fires but the exit-2 block is not enforced).

Usage: python client/scripts/validate_kiro.py
Env:   KIRO_CLI overrides the binary path.
Exit:  0 if `agent validate` passes and firekeep-symdex is present, else 1.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def kiro_bin() -> str:
    override = os.environ.get("KIRO_CLI")
    if override:
        return override
    local = os.environ.get("LOCALAPPDATA", "")
    return str(Path(local) / "Kiro-Cli" / "kiro-cli.exe")


def run(*args: str) -> tuple[int, str]:
    # encoding/errors explicit: kiro emits UTF-8 + ANSI escapes, which the default Windows
    # locale codec (cp1252) can't decode — text=True alone raises UnicodeDecodeError. A stream
    # can also be None, so coalesce before concatenating.
    proc = subprocess.run(
        [kiro_bin(), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or ""))


def main() -> int:
    # kiro output carries emoji/ANSI; the default Windows console codec (cp1252) can't ENCODE
    # them, so printing raw kiro text raises UnicodeEncodeError. Force UTF-8 on our own stdout.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - best-effort; non-reconfigurable streams are already fine
        pass

    agent = Path.home() / ".kiro" / "agents" / "firekeep.json"
    if not agent.is_file():
        print(f"[skip] no rendered agent at {agent} — run `firekeep install --runtime kiro` first")
        return 1

    # #1/#2/#3/#5 schema: kiro-cli requires --path (NOT a positional argument).
    rc, out = run("agent", "validate", "--path", str(agent))
    print(f"[schema] `agent validate --path` rc={rc}\n{out.strip()}\n")

    rc2, out2 = run("mcp", "list")
    print(f"[mcp] `mcp list` rc={rc2}\n{out2.strip()}\n")

    servers = sorted(json.loads(agent.read_text(encoding="utf-8")).get("mcpServers", {}))
    print(f"[rendered servers] {servers}")
    symdex_ok = "firekeep-symdex" in servers  # always-on since the client-consolidation change

    hooks = json.loads(agent.read_text(encoding="utf-8")).get("hooks", {})
    pre = (hooks.get("preToolUse") or [{}])[0]
    print(f"[pre-edit hook] matcher={pre.get('matcher')!r} command={pre.get('command')!r}")
    print("[blocking] ADVISORY on kiro-cli 2.12.1 — hook fires but exit-2 is not enforced; "
          "see docs/KIRO-VALIDATION.md for the manual re-validation probe.")

    ok = rc == 0 and symdex_ok
    print(f"\n[result] {'PASS' if ok else 'FAIL'} "
          f"(agent validate rc={rc}, firekeep-symdex present={symdex_ok})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
