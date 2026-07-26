"""Smoke test: the package imports and exposes the frozen version anchor."""


def test_package_imports_and_exposes_frozen_version():
    import firekeep_client

    assert firekeep_client.__version__ == "0.1.23"


def test_frozen_module_layout_is_present():
    # Every module named in the frozen spine layout must exist as a submodule.
    import importlib
    import importlib.util

    for mod in (
        "firekeep_client.resolver",
        "firekeep_client.transport",
        "firekeep_client.state",
        "firekeep_client.hooklog",
        "firekeep_client.sidecar",
        "firekeep_client.cli",
        "firekeep_client.hooks",
        "firekeep_client.hooks.__main__",
        "firekeep_client.hooks.session_start",
        "firekeep_client.hooks.stop",
        "firekeep_client.hooks.prompt",
        "firekeep_client.hooks.pre_tool",
        "firekeep_client.hooks.post_tool",
        "firekeep_client.adapters",
        "firekeep_client.adapters.base",
        "firekeep_client.adapters.claude",
        "firekeep_client.adapters.codex",
        "firekeep_client.adapters.kiro",
        "firekeep_client.adapters.opencode",
        "firekeep_client.contract",
        "firekeep_client.contract.matrix",
    ):
        importlib.import_module(mod)

    # The shim is the ONE spine module allowed third-party imports (httpx/mcp/
    # anyio — the import-boundary contract confines them there), so in the
    # deps-free CI client job it cannot be EXECUTED. find_spec proves the module
    # exists in the layout without running it; its import-time health is covered
    # by the shim test suite wherever the deps are installed.
    assert importlib.util.find_spec("firekeep_client.shim") is not None
