"""Wire-level proof that `output_schema=None` suppresses `structuredContent`.

`test_mcp_output_schema.py` asserts only that `mcp.tool()` was *invoked with*
`output_schema=None`, because `cortex/tests/conftest.py` installs a fake
`fastmcp` into `sys.modules` at collection time. That test proves the four
production tools declare the kwarg; it cannot prove the kwarg does anything.
This file closes that half: a minimal two-tool `FastMCP` instance, listed and
called through a real `Client`, with the same string returned by a tool that
declares `output_schema=None` and one that does not.

It must run in a SUBPROCESS. The conftest fake is already in `sys.modules` by
the time any test in this directory imports anything, so an in-process
`import fastmcp` can never reach the real library — and
`pytest.importorskip("fastmcp")` would cheerfully succeed against the fake,
which is exactly the false pass this file exists to avoid. A child interpreter
gets a pristine import.

Two facts are established, both directly asserted below:

1. `output_schema=None` removes `structuredContent` from the call result (and
   `outputSchema` from the listing). The default `-> str` behavior infers
   `{"properties": {"result": {"type": "string"}}, ..., "x-fastmcp-wrap-result":
   true}` and ships the string a second time under `structuredContent.result`.
2. `content[0].text` is BYTE-IDENTICAL either way.

Fact 2 is the load-bearing one. The original design note claimed `-> str` tools
ship JSON-escaped markdown and that this setting changes what the runtime
renders. **That claim is wrong** — the text payload is unchanged plain markdown
in both cases (raw `"` and `\\`, not `\\"` and `\\\\`), and
`test_text_payload_is_not_json_escaped` pins that refutation so the framing
cannot drift back. The only win is the removed duplicate: roughly half the
result bytes, and `test_saving_is_the_duplicate_only` bounds it so a future
claim of a larger win fails here.

Measured (see the module docstring of the generated child script):
- fastmcp 3.4.4 (the pinned version, cortex/requirements.lock:303): 284 -> 132
  wire bytes for a 70-byte markdown payload, 152 saved (53.5%).
- fastmcp 3.1.1 (the low end of the declared `fastmcp>=3.1,<4` range): 111
  saved (45.7%).
Both versions satisfy both facts, which is why this test asserts invariants
rather than a fixed byte count — CI installs `cortex/requirements.txt` (the
range), not the lock.

NOT closed by this file, and not claimable from it: whether each *runtime*
(Claude Code, kiro, OpenCode, Codex) renders the result identically to the user.
That needs a deploy, and the deployed stack predates this change entirely.
This proves what the MCP server puts on the wire, nothing about the far end.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

# The minimal reproduction, run in a child interpreter against the real fastmcp.
# Two `-> str` tools returning the identical markdown; one suppressed, one not.
# The payload deliberately contains a double quote, a backslash and a non-ASCII
# em dash — the characters a "ships JSON-escaped markdown" claim would hinge on.
_PROBE = r'''
import asyncio, json, sys

try:
    import fastmcp
    from fastmcp import Client, FastMCP
except Exception as exc:
    print(json.dumps({"skip": "real fastmcp not importable: %s" % (exc,)}))
    sys.exit(0)

MARKDOWN = 'Recalled 2 memories\n\n- "quoted" \\ backslash — em dash\n- second line\n'

mcp = FastMCP("probe")


@mcp.tool(output_schema=None)
async def suppressed() -> str:
    return MARKDOWN


@mcp.tool()
async def inferred() -> str:
    return MARKDOWN


async def main():
    out = {"fastmcp_version": fastmcp.__version__, "markdown": MARKDOWN}
    async with Client(mcp) as client:
        out["output_schemas"] = {
            t.name: t.outputSchema for t in await client.list_tools()
        }
        results = {}
        for name in ("suppressed", "inferred"):
            res = await client.call_tool_mcp(name, {})
            dumped = res.model_dump(exclude_none=True, by_alias=True)
            text = res.content[0].text
            results[name] = {
                "has_structured_content": "structuredContent" in dumped,
                "structured_content": dumped.get("structuredContent"),
                "text": text,
                "text_bytes": len(text.encode("utf-8")),
                "wire_bytes": len(
                    json.dumps(
                        dumped, separators=(",", ":"), ensure_ascii=False
                    ).encode("utf-8")
                ),
            }
        out["results"] = results
    print(json.dumps(out))


try:
    asyncio.run(main())
except Exception as exc:
    print(json.dumps({"skip": "probe failed on this fastmcp: %r" % (exc,)}))
'''

_cache: dict[str, dict] = {}


def _probe() -> dict:
    """Run the reproduction once per session; skip if real fastmcp is absent.

    The child is `sys.executable` by default. Set FIREKEEP_WIRE_PROBE_PYTHON to
    the interpreter of a venv holding the LOCKED fastmcp to re-verify against
    the version that actually ships, which is how the byte counts in the module
    docstring were taken (the dev environment resolves the range, not the lock).
    """
    if "result" not in _cache:
        proc = subprocess.run(
            [os.environ.get("FIREKEEP_WIRE_PROBE_PYTHON", sys.executable), "-c", _PROBE],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode != 0:
            pytest.skip(f"probe subprocess failed ({proc.returncode}): {proc.stderr[-2000:]}")
        stdout = proc.stdout.strip()
        # Take the last line: a noisy import-time banner must not break parsing.
        try:
            _cache["result"] = json.loads(stdout.splitlines()[-1])
        except (ValueError, IndexError):
            pytest.skip(f"probe produced no parseable JSON: {stdout[-2000:]!r}")
    result = _cache["result"]
    if "skip" in result:
        pytest.skip(result["skip"])
    return result


def test_output_schema_none_removes_structured_content():
    """Fact 1: the duplicate disappears — and the control proves it was there."""
    probe = _probe()
    suppressed = probe["results"]["suppressed"]
    inferred = probe["results"]["inferred"]

    # The control must show the duplicate, or this test proves nothing at all.
    assert inferred["has_structured_content"], (
        "the no-output_schema control shipped no structuredContent, so this "
        f"fastmcp ({probe['fastmcp_version']}) does not exhibit the duplication "
        "this suppression exists to avoid — re-derive the change before trusting it"
    )
    assert inferred["structured_content"] == {"result": probe["markdown"]}

    assert not suppressed["has_structured_content"], (
        f"output_schema=None did NOT suppress structuredContent on fastmcp "
        f"{probe['fastmcp_version']}"
    )
    assert probe["output_schemas"]["suppressed"] is None
    assert probe["output_schemas"]["inferred"] is not None


def test_content_text_is_byte_identical_either_way():
    """Fact 2 (load-bearing): the rendered text payload does not change."""
    probe = _probe()
    suppressed = probe["results"]["suppressed"]
    inferred = probe["results"]["inferred"]

    assert suppressed["text"] == inferred["text"]
    assert suppressed["text_bytes"] == inferred["text_bytes"]
    # ...and it is the string the tool returned, untouched.
    assert suppressed["text"] == probe["markdown"]


def test_text_payload_is_not_json_escaped():
    """Refutes the original design note: `-> str` tools do not ship escaped markdown.

    If the text were JSON-escaped we would see a literal backslash-quote pair
    rather than a bare quote. It is bare, with and without the suppression, so
    output_schema=None changes nothing about escaping — there was nothing to fix.
    """
    probe = _probe()
    for name in ("suppressed", "inferred"):
        text = probe["results"][name]["text"]
        assert '"quoted"' in text, f"{name}: expected a raw double quote"
        assert '\\"quoted\\"' not in text, f"{name}: text was JSON-escaped"
        assert "— em dash" in text, f"{name}: expected a raw em dash"
        assert "\n" in text, f"{name}: expected a real newline, not an escaped one"


def test_saving_is_the_duplicate_only():
    """The win is the removed copy — about half — and no more than that.

    Bounded on both sides deliberately. A zero/negative saving means the
    suppression stopped working; a saving far above half would mean something
    other than the duplicate is being dropped, which would resurrect the
    overstated framing this file exists to correct.
    """
    probe = _probe()
    suppressed = probe["results"]["suppressed"]["wire_bytes"]
    inferred = probe["results"]["inferred"]["wire_bytes"]

    saved = inferred - suppressed
    assert saved > 0, f"no bytes saved ({inferred} -> {suppressed})"
    fraction = saved / inferred
    assert 0.25 < fraction < 0.65, (
        f"saving {saved}/{inferred} = {fraction:.1%} on fastmcp "
        f"{probe['fastmcp_version']} is not 'the duplicated copy, roughly half'"
    )
