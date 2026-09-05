"""The approval broker: the only thing in Hands that can authorise a
protected step, and the only thing that can authorise it is a human.

It runs as its own process (`firekeep-hands-broker run`) rather than inside
the MCP server on purpose. The model drives the MCP server; if the permit
store lived there, a compromised or merely over-eager runtime would be one
function call away from approving its own actions. Across a process boundary
the only way in is the loopback HTTP API, which can create and consume
permits but has no route that grants one — approval enters only through an
OS input listener that has already rejected every synthetic keystroke, or
through a relay task a person answered from the dashboard on their phone.

This module holds just the chord grammar, because both platform listeners and
the config validator need it and neither may import the other.
"""
from __future__ import annotations

# Every spelling a human might reasonably write, folded onto four canonical
# names. `win`/`meta` fold onto `cmd` so one chord string means the same
# physical gesture on both platforms — the key next to the space bar.
_MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "option": "alt",
    "opt": "alt",
    "shift": "shift",
    "cmd": "cmd",
    "command": "cmd",
    "win": "cmd",
    "meta": "cmd",
    "super": "cmd",
}

# Trigger keys that are not a single character. Each platform listener maps
# these to its own key codes and raises for the ones it has no code for, so
# the grammar can stay one shared table.
NAMED_TRIGGER_KEYS = frozenset(
    {f"f{n}" for n in range(1, 13)}
    | {
        "space", "enter", "return", "tab", "escape", "esc", "backspace",
        "delete", "insert", "home", "end", "pageup", "pagedown",
        "up", "down", "left", "right",
    }
)


def parse_chord(chord: str) -> tuple[frozenset[str], str]:
    """`"ctrl+alt+y"` -> `(frozenset({"ctrl", "alt"}), "y")`.

    At least one modifier is mandatory. A bare `"y"` would turn every press
    of that letter into an approval, which is the one configuration mistake
    that quietly disables the whole safety boundary — so it is a ValueError
    here rather than a surprise later. A repeated modifier and an unknown
    trigger key are refused for the same reason: a chord the human thinks
    they set but cannot actually press is worse than no chord at all.
    """
    if not isinstance(chord, str):
        raise ValueError(f"chord must be a string, got {type(chord).__name__}")
    parts = [part.strip().lower() for part in chord.split("+")]
    if len(parts) < 2 or any(not part for part in parts):
        raise ValueError(f"not a chord: {chord!r} (want e.g. 'ctrl+alt+y')")
    *modifiers, trigger = parts
    canonical: list[str] = []
    for modifier in modifiers:
        name = _MODIFIER_ALIASES.get(modifier)
        if name is None:
            raise ValueError(f"unknown modifier {modifier!r} in {chord!r}")
        if name in canonical:
            raise ValueError(f"modifier {name!r} repeated in {chord!r}")
        canonical.append(name)
    is_single_key = len(trigger) == 1 and trigger.isascii() and trigger.isalnum()
    if not is_single_key and trigger not in NAMED_TRIGGER_KEYS:
        raise ValueError(f"unknown trigger key {trigger!r} in {chord!r}")
    return frozenset(canonical), trigger
