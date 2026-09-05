import logging

import pytest

from firekeep_hands.broker import parse_chord
from firekeep_hands.broker.listeners.win import ChordTracker, kb_event_is_real, log_key_event


def test_injected_flags_are_not_real():
    assert kb_event_is_real(0x00) and kb_event_is_real(0x80)
    assert not kb_event_is_real(0x10) and not kb_event_is_real(0x02) and not kb_event_is_real(0x12)


def test_chord_requires_real_modifiers_and_trigger():
    t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
    assert t.feed(0xA2, True, True) is None and t.feed(0xA4, True, True) is None
    assert t.feed(ord("Y"), True, True) == "approve"
    assert t.feed(ord("Y"), False, True) is None
    assert t.feed(ord("N"), True, True) == "deny"


def test_injected_events_never_count_even_for_modifiers():
    t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
    t.feed(0xA2, True, False); t.feed(0xA4, True, False)
    assert t.feed(ord("Y"), True, False) is None
    assert t.feed(ord("Y"), True, True) is None      # modifiers were injected, so they do not count


# --- additions -------------------------------------------------------------


def test_the_trigger_alone_is_not_the_chord():
    t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
    assert t.feed(ord("Y"), True, True) is None
    t.feed(ord("Y"), False, True)
    t.feed(0xA2, True, True)                  # ctrl only
    assert t.feed(ord("Y"), True, True) is None


def test_releasing_a_modifier_breaks_the_chord():
    t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
    t.feed(0x11, True, True); t.feed(0x12, True, True)
    assert t.feed(ord("Y"), True, True) == "approve"
    t.feed(ord("Y"), False, True)             # a fresh press, not a repeat
    t.feed(0x12, False, True)                 # alt up
    assert t.feed(ord("Y"), True, True) is None


def test_holding_the_chord_answers_exactly_one_question():
    """Windows repeats WM_KEYDOWN ~30 times a second for a held key. One
    gesture must grant one permit, the way one tap on the phone does — a
    human holding the chord for half a second must not empty the queue."""
    t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
    t.feed(0xA2, True, True); t.feed(0xA4, True, True)
    assert t.feed(ord("Y"), True, True) == "approve"
    for _ in range(20):                       # auto-repeat while still held
        assert t.feed(ord("Y"), True, True) is None
    t.feed(ord("Y"), False, True)             # released
    assert t.feed(ord("Y"), True, True) == "approve"


def test_a_repeat_of_one_trigger_does_not_block_the_other():
    t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
    t.feed(0xA2, True, True); t.feed(0xA4, True, True)
    assert t.feed(ord("Y"), True, True) == "approve"
    assert t.feed(ord("Y"), True, True) is None
    assert t.feed(ord("N"), True, True) == "deny"


def test_left_and_right_modifiers_are_the_same_modifier():
    for ctrl, alt in ((0xA2, 0xA4), (0xA3, 0xA5), (0x11, 0x12)):
        t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
        t.feed(ctrl, True, True); t.feed(alt, True, True)
        assert t.feed(ord("Y"), True, True) == "approve"


def test_an_injected_modifier_release_cannot_disarm_a_real_chord():
    """Symmetric to the down case: synthetic input is ignored entirely, so
    malware cannot use it to manipulate the tracker's held set either way."""
    t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
    t.feed(0xA2, True, True); t.feed(0xA4, True, True)
    t.feed(0xA4, False, False)                # injected alt-up: ignored
    assert t.feed(ord("Y"), True, True) == "approve"


def test_an_unrelated_key_is_not_a_decision():
    t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
    t.feed(0xA2, True, True); t.feed(0xA4, True, True)
    assert t.feed(ord("K"), True, True) is None
    assert t.feed(0x0D, True, True) is None   # Enter


def test_a_chord_with_a_named_trigger_key():
    t = ChordTracker("ctrl+shift+f9", "ctrl+shift+f10")
    t.feed(0xA2, True, True); t.feed(0xA0, True, True)
    assert t.feed(0x78, True, True) == "approve"     # VK_F9
    assert t.feed(0x79, True, True) == "deny"        # VK_F10


def test_parse_chord_accepts_the_documented_aliases():
    assert parse_chord("ctrl+alt+y") == (frozenset({"ctrl", "alt"}), "y")
    assert parse_chord("Control+Option+Y") == (frozenset({"ctrl", "alt"}), "y")
    assert parse_chord("cmd+shift+n") == (frozenset({"cmd", "shift"}), "n")
    assert parse_chord("win+alt+f5") == (frozenset({"cmd", "alt"}), "f5")
    assert parse_chord(" meta + ctrl + space ") == (frozenset({"cmd", "ctrl"}), "space")


def test_parse_chord_refuses_anything_a_human_could_press_by_accident():
    for bad in ["", "   ", "y", "ctrl", "ctrl+", "+y", "ctrl+alt", "ctrl+alt+yy",
                "ctrl+alt+nope", "ctrl+ctrl+y", "ctrl+alt+y+n", "hyper+y"]:
        with pytest.raises(ValueError):
            parse_chord(bad)


def test_an_unparseable_chord_is_refused_at_construction():
    with pytest.raises(ValueError):
        ChordTracker("y", "n")


def test_debug_logging_does_not_record_which_key_was_pressed(caplog, monkeypatch):
    """The broker watches every keystroke on the machine and is meant to run
    autostarted for months. DEBUG must not turn it into a log of the human's
    passwords, so only the class of the key is recorded."""
    monkeypatch.delenv("FIREKEEP_HANDS_TRACE_KEYS", raising=False)
    t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
    with caplog.at_level(logging.DEBUG, logger="firekeep_hands.broker.listeners.win"):
        for vk in (ord("P"), ord("A"), ord("S"), ord("W"), 0x30, 0x31):   # a "password"
            log_key_event(t, vk, True, True, 0x00)
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert messages                                 # it did log something
    assert "vk=" not in messages
    for vk in (ord("P"), ord("A"), ord("S"), ord("W"), 0x30, 0x31):
        assert f"0x{vk:02X}" not in messages and str(vk) not in messages
    assert "kind=other" in messages and "real=True" in messages


def test_the_key_class_is_still_enough_to_debug_a_chord(caplog, monkeypatch):
    monkeypatch.delenv("FIREKEEP_HANDS_TRACE_KEYS", raising=False)
    t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
    with caplog.at_level(logging.DEBUG, logger="firekeep_hands.broker.listeners.win"):
        log_key_event(t, 0xA2, True, True, 0x00)
        log_key_event(t, ord("Y"), True, False, 0x10)
    assert "kind=modifier" in caplog.text
    assert "kind=trigger" in caplog.text and "flags=0x10 real=False" in caplog.text


def test_raw_key_codes_need_an_explicit_opt_in(caplog, monkeypatch):
    monkeypatch.setenv("FIREKEEP_HANDS_TRACE_KEYS", "1")
    t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
    with caplog.at_level(logging.DEBUG, logger="firekeep_hands.broker.listeners.win"):
        log_key_event(t, ord("P"), True, True, 0x00)
    assert "vk=0x50" in caplog.text


def test_classify_names_only_the_three_kinds():
    t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
    assert t.classify(0xA2) == "modifier" and t.classify(0x12) == "modifier"
    assert t.classify(ord("Y")) == "trigger" and t.classify(ord("N")) == "trigger"
    assert t.classify(ord("P")) == "other"


def test_nothing_windows_specific_is_imported_at_module_import():
    """The PLATFORM-MODULE RULE: this module's pure parts must import on
    Linux CI and macOS. Anything from user32 is resolved inside run_listener."""
    import firekeep_hands.broker.listeners.win as win
    source = __import__("inspect").getsource(win)
    module_level = source.split("def run_listener")[0]
    for banned in ("WinDLL", "windll", "ctypes.wintypes", "import ctypes.wintypes"):
        assert banned not in module_level, f"{banned} must not appear before run_listener"
