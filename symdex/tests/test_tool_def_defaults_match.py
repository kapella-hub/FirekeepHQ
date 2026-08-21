"""Every tool's advertised MCP schema must agree with its Python signature.

Symdex hand-writes each tool's `TOOL_DEF["inputSchema"]` next to the function it
describes. Nothing tied the two together, so they could drift silently — and
they did: raising `get_context`'s `budget_tokens` default from 4000 to 8000 on
2026-08-21 left the schema still advertising `"default": 4000`, in the same file,
twenty lines apart.

That class of drift is worse than a stale comment. The schema is what the MODEL
reads to decide what to send, so a wrong default means the agent reasons about a
budget the tool does not have — it is a lie told to the only reader who cannot
check it against the source.

This is the cheap structural guard: for every discovered tool, every parameter
whose schema declares a `default` must declare the SAME value the function
signature uses.
"""

import inspect

import pytest

from firekeep_symdex.tools import discover_tools


TOOLS = discover_tools()


def _declared_defaults(tool) -> dict:
    props = (tool.get("inputSchema") or {}).get("properties") or {}
    return {
        name: spec["default"]
        for name, spec in props.items()
        if isinstance(spec, dict) and "default" in spec
    }


@pytest.mark.parametrize("tool_name", sorted(TOOLS))
def test_schema_defaults_match_the_signature(tool_name):
    tool = TOOLS[tool_name]
    declared = _declared_defaults(tool)
    if not declared:
        pytest.skip("no defaults declared in this tool's schema")

    params = inspect.signature(tool["handler"]).parameters
    mismatches = []
    for param_name, schema_default in declared.items():
        param = params.get(param_name)
        if param is None:
            mismatches.append(
                f"{param_name}: declared in schema but absent from the signature"
            )
            continue
        if param.default is inspect.Parameter.empty:
            mismatches.append(
                f"{param_name}: schema says default={schema_default!r} but the "
                "parameter is REQUIRED in the signature"
            )
            continue
        if param.default != schema_default:
            mismatches.append(
                f"{param_name}: schema default={schema_default!r} != "
                f"signature default={param.default!r}"
            )

    assert not mismatches, (
        f"{tool_name}'s advertised schema disagrees with its implementation — "
        "the model reads the schema, so this misleads the one reader who "
        "cannot check:\n  " + "\n  ".join(mismatches)
    )


@pytest.mark.parametrize("tool_name", sorted(TOOLS))
def test_declared_required_params_have_no_signature_default(tool_name):
    """A parameter cannot be both required and defaulted.

    The mirror of the above: the schema's `required` list must not name a
    parameter the function happily omits, or the model asks for information it
    never needed.
    """
    tool = TOOLS[tool_name]
    schema = tool.get("inputSchema") or {}
    required = schema.get("required") or []
    params = inspect.signature(tool["handler"]).parameters

    wrong = [
        name
        for name in required
        if name in params and params[name].default is not inspect.Parameter.empty
    ]
    assert not wrong, (
        f"{tool_name} lists {wrong} as required, but the signature gives them "
        "defaults — the schema demands arguments the tool does not need."
    )


def test_get_context_budget_default_is_the_post_fix_value():
    """Pin the specific number the 2026-08-21 accounting change depends on.

    The default and the accounting move together: charging the whole entry makes
    each symbol cost more, so 4000 would quietly halve what a caller receives.
    Measured on the live index, `focus="memory recall"` returned 17 symbols
    before the fix and returns 17 at 8000 after it. Lowering this without
    revisiting the accounting silently reduces what every caller gets back.
    """
    tool = TOOLS["get_context"]
    assert _declared_defaults(tool)["budget_tokens"] == 8000
    assert inspect.signature(tool["handler"]).parameters["budget_tokens"].default == 8000
