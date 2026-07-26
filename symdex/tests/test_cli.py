"""CLI behavior tests."""

import pytest

from firekeep_symdex.server import main


def test_main_help_exits_without_starting_server(capsys):
    """`--help` should print usage and exit cleanly."""
    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "firekeep-symdex" in out
    assert "Run the FirekeepSymdex MCP server" in out
