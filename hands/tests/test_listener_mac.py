import pytest

from firekeep_hands import HANDS_TAG
from firekeep_hands.broker.listeners.mac import ChordTracker, event_is_real, KEYCODES, FLAG_CONTROL, FLAG_ALT


def test_tagged_or_non_hid_events_are_not_real():
    assert event_is_real(0, 1)
    assert not event_is_real(HANDS_TAG, 1) and not event_is_real(0, 0)


def test_chord_from_flags_and_keycode():
    t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
    assert t.feed(KEYCODES["y"], FLAG_CONTROL | FLAG_ALT, True) == "approve"
    assert t.feed(KEYCODES["n"], FLAG_CONTROL | FLAG_ALT, True) == "deny"
    assert t.feed(KEYCODES["y"], FLAG_CONTROL, True) is None
    assert t.feed(KEYCODES["y"], FLAG_CONTROL | FLAG_ALT, False) is None


# --- additions -------------------------------------------------------------


def test_every_letter_has_a_keycode_and_they_are_distinct():
    assert set(KEYCODES) >= set("abcdefghijklmnopqrstuvwxyz")
    letters = {k: v for k, v in KEYCODES.items() if len(k) == 1 and k.isalpha()}
    assert len(set(letters.values())) == len(letters)
    assert KEYCODES["y"] == 16 and KEYCODES["n"] == 45      # the shipped default chords


def test_any_synthetic_source_state_is_rejected():
    """kCGEventSourceStateHIDSystemState is 1; a private or combined-session
    source is something a program created."""
    for state in (0, 2, -1, 99):
        assert not event_is_real(0, state)


def test_a_tagged_event_is_rejected_whatever_its_source_state():
    for state in (0, 1, 2):
        assert not event_is_real(HANDS_TAG, state)


def test_the_wrong_key_with_the_right_flags_is_not_a_decision():
    t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
    assert t.feed(KEYCODES["k"], FLAG_CONTROL | FLAG_ALT, True) is None


def test_extra_modifiers_do_not_block_the_chord():
    """Superset semantics, matching the Windows tracker: the required
    modifiers must be down, others are not the tracker's business."""
    from firekeep_hands.broker.listeners.mac import FLAG_SHIFT
    t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
    assert t.feed(KEYCODES["y"], FLAG_CONTROL | FLAG_ALT | FLAG_SHIFT, True) == "approve"


def test_a_chord_needing_command_needs_the_command_flag():
    from firekeep_hands.broker.listeners.mac import FLAG_COMMAND
    t = ChordTracker("cmd+shift+y", "cmd+shift+n")
    assert t.feed(KEYCODES["y"], FLAG_CONTROL | FLAG_ALT, True) is None
    from firekeep_hands.broker.listeners.mac import FLAG_SHIFT
    assert t.feed(KEYCODES["y"], FLAG_COMMAND | FLAG_SHIFT, True) == "approve"


def test_an_unparseable_chord_is_refused_at_construction():
    with pytest.raises(ValueError):
        ChordTracker("y", "n")


def test_nothing_macos_specific_is_imported_at_module_import():
    """The PLATFORM-MODULE RULE: pyobjc is resolved inside run_listener so the
    pure parts import on Windows and Linux CI."""
    import firekeep_hands.broker.listeners.mac as mac
    source = __import__("inspect").getsource(mac)
    module_level = source.split("def run_listener")[0]
    for banned in ("import Quartz", "import AppKit", "from Quartz"):
        assert banned not in module_level, f"{banned} must not appear before run_listener"
