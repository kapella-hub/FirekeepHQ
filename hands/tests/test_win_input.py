"""Struct layout and event-builder tests for the Windows input layer.

Everything except `send()` is pure — the builders return ctypes structures
and never touch user32 — so this whole file runs on Linux CI too. That is the
point: a wrong field width is exactly the bug that cannot be seen on the
machine the struct was written on, because `c_long` is 4 bytes there and 8
bytes on Linux x64.
"""
import ctypes
import sys

import pytest

from firekeep_hands import HANDS_TAG
from firekeep_hands.backends import _win_input as wi
from firekeep_hands.backends.base import HandsError

_64BIT = ctypes.sizeof(ctypes.c_void_p) == 8


def test_input_struct_matches_win32_layout():
    assert ctypes.sizeof(wi.INPUT) == (40 if _64BIT else 28)
    assert ctypes.sizeof(wi.MOUSEINPUT) == (32 if _64BIT else 24)
    assert ctypes.sizeof(wi.KEYBDINPUT) == (24 if _64BIT else 16)


def test_every_built_event_carries_the_hands_tag():
    built = (
        wi.build_key_chord("ctrl+alt+y")
        + wi.build_text("hé")
        + wi.build_click(10, 20, "left", False)
        + wi.build_scroll(1, 2, -3)
    )
    assert built, "the builders produced nothing to check"
    for inp in built:
        assert inp.union.ki.dwExtraInfo == HANDS_TAG or inp.union.mi.dwExtraInfo == HANDS_TAG


def test_chord_builds_press_and_release_in_order():
    seq = wi.build_key_chord("ctrl+s")
    vks = [(i.union.ki.wVk, bool(i.union.ki.dwFlags & wi.KEYEVENTF_KEYUP)) for i in seq]
    assert vks == [(0x11, False), (ord("S"), False), (ord("S"), True), (0x11, True)]


def test_modifiers_release_in_reverse_order():
    seq = wi.build_key_chord("ctrl+shift+n")
    vks = [(i.union.ki.wVk, bool(i.union.ki.dwFlags & wi.KEYEVENTF_KEYUP)) for i in seq]
    assert vks == [
        (0x11, False), (0x10, False), (ord("N"), False),
        (ord("N"), True), (0x10, True), (0x11, True),
    ]


def test_a_bare_key_is_a_valid_chord_here():
    """The broker's `parse_chord` refuses a modifier-less chord on purpose —
    a bare approval key would disable the safety boundary. A backend action
    is the opposite case: `key("enter")` and `key("n")` (answering a dialog)
    are exactly what a runtime needs."""
    seq = wi.build_key_chord("enter")
    assert [(i.union.ki.wVk, bool(i.union.ki.dwFlags & wi.KEYEVENTF_KEYUP)) for i in seq] == [
        (0x0D, False), (0x0D, True),
    ]


def test_navigation_keys_are_marked_extended():
    """Without KEYEVENTF_EXTENDEDKEY the nav cluster is decided by NumLock,
    so a Delete arrives as the numpad '.' — silent and wrong."""
    for chord, vk in (("delete", 0x2E), ("home", 0x24), ("left", 0x25)):
        for inp in wi.build_key_chord(chord):
            assert inp.union.ki.wVk == vk
            assert inp.union.ki.dwFlags & wi.KEYEVENTF_EXTENDEDKEY
    for inp in wi.build_key_chord("backspace"):
        assert not inp.union.ki.dwFlags & wi.KEYEVENTF_EXTENDEDKEY


def test_a_modifier_named_twice_is_pressed_once():
    """"ctrl+control+s" names one physical key twice. Pressing it twice would
    leave a second key-down with no key-up behind the release sweep, which is
    a Ctrl stuck down for the human afterwards."""
    def shape(chord):
        return [(i.union.ki.wVk, bool(i.union.ki.dwFlags & wi.KEYEVENTF_KEYUP))
                for i in wi.build_key_chord(chord)]

    assert shape("ctrl+control+s") == shape("ctrl+s")
    assert shape("cmd+win+shift+a") == shape("cmd+shift+a")


def test_a_short_send_lifts_the_keys_the_delivered_prefix_left_down(monkeypatch):
    """SendInput stopping mid-chord leaves Ctrl held as far as Windows is
    concerned, and nothing else in the system will lift it — every key the
    human types next becomes a Ctrl-chord."""
    batches = []

    class ShortSendInput:
        def SendInput(self, count, array, size):
            batches.append([array[i] for i in range(count)])
            return 1 if len(batches) == 1 else count  # only the first event lands

    monkeypatch.setattr(wi, "user32", ShortSendInput)
    monkeypatch.setattr(wi, "_last_error", lambda: 5)

    with pytest.raises(HandsError) as exc:
        wi.send(wi.build_key_chord("ctrl+shift+s"))
    assert exc.value.code == "backend"
    assert "1/6" in str(exc.value)

    assert len(batches) == 2, "no release sweep was sent"
    assert [(i.union.ki.wVk, bool(i.union.ki.dwFlags & wi.KEYEVENTF_KEYUP))
            for i in batches[1]] == [(0x11, True)]


def test_a_complete_send_sweeps_nothing(monkeypatch):
    batches = []

    class FullSendInput:
        def SendInput(self, count, array, size):
            batches.append([array[i] for i in range(count)])
            return count

    monkeypatch.setattr(wi, "user32", FullSendInput)
    assert wi.send(wi.build_key_chord("ctrl+s")) == 4
    assert len(batches) == 1


def test_the_sweep_keeps_the_extended_flag_and_ignores_unicode(monkeypatch):
    """A nav key pressed with EXTENDEDKEY must be released with it, or the
    release names the numpad key instead. Unicode events carry VK_PACKET,
    which is not a key and latches nothing."""
    batches = []

    class ShortSendInput:
        def SendInput(self, count, array, size):
            batches.append([array[i] for i in range(count)])
            return 2 if len(batches) == 1 else count

    monkeypatch.setattr(wi, "user32", ShortSendInput)
    monkeypatch.setattr(wi, "_last_error", lambda: 0)
    with pytest.raises(HandsError):
        wi.send(wi.build_key_chord("ctrl+delete"))
    assert [(i.union.ki.wVk, i.union.ki.dwFlags) for i in batches[1]] == [
        (0x2E, wi.KEYEVENTF_EXTENDEDKEY | wi.KEYEVENTF_KEYUP),
        (0x11, wi.KEYEVENTF_KEYUP),
    ]

    batches.clear()
    with pytest.raises(HandsError):
        wi.send(wi.build_text("ab"))
    assert len(batches) == 1, "a Unicode packet is not a key and needs no sweep"


def test_unknown_modifier_and_unknown_key_are_refused():
    with pytest.raises(HandsError) as bad_modifier:
        wi.build_key_chord("hyper+s")
    assert bad_modifier.value.code == "invalid_action"
    with pytest.raises(HandsError):
        wi.build_key_chord("ctrl+nosuchkey")


def test_text_is_one_unicode_pair_per_utf16_code_unit():
    seq = wi.build_text("hé")
    assert len(seq) == 4
    assert [i.union.ki.wScan for i in seq] == [ord("h"), ord("h"), ord("é"), ord("é")]
    for inp in seq:
        assert inp.union.ki.wVk == 0
        assert inp.union.ki.dwFlags & wi.KEYEVENTF_UNICODE
    assert [bool(i.union.ki.dwFlags & wi.KEYEVENTF_KEYUP) for i in seq] == [False, True, False, True]


def test_text_sends_astral_characters_as_a_surrogate_pair():
    seq = wi.build_text("\U0001F525")  # one astral code point, two UTF-16 units
    assert len(seq) == 4
    assert [i.union.ki.wScan for i in seq] == [0xD83D, 0xD83D, 0xDD25, 0xDD25]


def test_text_turns_newlines_into_a_real_enter():
    """A '\\n' delivered as a Unicode event is swallowed by most edit
    controls; only VK_RETURN inserts a line."""
    seq = wi.build_text("a\r\nb")
    kinds = [(i.union.ki.wVk, i.union.ki.wScan) for i in seq]
    assert kinds == [(0, ord("a")), (0, ord("a")), (0x0D, 0), (0x0D, 0), (0, ord("b")), (0, ord("b"))]


# (left, top, width, height) of a virtual desktop. Not this machine's — the
# point of a pure helper is to test layouts the test host does not have.
_ONE_MONITOR = (0, 0, 1921, 1081)
# Primary 1920x1080 at the origin, a second the same size placed to its LEFT:
# the desktop starts at x = -1920 and every point on that monitor is negative.
_SECOND_MONITOR_LEFT = (-1920, 0, 3840, 1080)


def test_absolute_coords_on_a_single_monitor_at_the_origin():
    assert wi.absolute_coords(0, 0, 0, 0, 1920, 1080) == (0, 0)
    assert wi.absolute_coords(960, 540, 0, 0, 1920, 1080) == (
        round(960 * 65535 / 1919), round(540 * 65535 / 1079))


def test_absolute_coords_map_the_last_pixel_to_65535():
    """The divisor is size - 1: with `size`, the bottom-right pixel is
    unreachable and every click drifts short of where it was aimed."""
    assert wi.absolute_coords(1919, 1079, 0, 0, 1920, 1080) == (65535, 65535)
    assert wi.absolute_coords(1919, 1079, *_SECOND_MONITOR_LEFT[:2],
                              *_SECOND_MONITOR_LEFT[2:]) == (65535, 65535)


def test_absolute_coords_reach_a_monitor_left_of_the_primary():
    """A second monitor placed left of the primary has negative screen
    coordinates. Normalising against SM_CXSCREEN — the primary's size, from
    an origin assumed to be zero — sends every one of these to the primary."""
    vx, vy, vw, vh = _SECOND_MONITOR_LEFT
    assert wi.absolute_coords(-1920, 0, vx, vy, vw, vh) == (0, 0)          # its top-left
    assert wi.absolute_coords(-960, 540, vx, vy, vw, vh) == (
        round(960 * 65535 / 3839), round(540 * 65535 / 1079))              # its centre
    assert wi.absolute_coords(0, 0, vx, vy, vw, vh) == (
        round(1920 * 65535 / 3839), 0)                                     # the primary's origin


def test_click_moves_over_the_virtual_desktop_then_presses_and_releases():
    seq = wi.build_click(10, 20, "left", False, virtual=_ONE_MONITOR)
    assert [i.union.mi.dwFlags for i in seq] == [
        wi.MOVE_FLAGS, wi.MOUSEEVENTF_LEFTDOWN, wi.MOUSEEVENTF_LEFTUP,
    ]
    assert seq[0].union.mi.dx == round(10 * 65535 / 1920)
    assert seq[0].union.mi.dy == round(20 * 65535 / 1080)
    assert all(i.type == wi.INPUT_MOUSE for i in seq)


def test_the_move_event_declares_the_virtual_desktop():
    """Without MOUSEEVENTF_VIRTUALDESK, Windows reads the same normalised
    pair against the primary monitor and the pointer lands there."""
    assert wi.MOVE_FLAGS & wi.MOUSEEVENTF_VIRTUALDESK
    for builder in (lambda: wi.build_click(0, 0, "left", False, virtual=_ONE_MONITOR),
                    lambda: wi.build_scroll(0, 0, 1, virtual=_ONE_MONITOR)):
        assert builder()[0].union.mi.dwFlags & wi.MOUSEEVENTF_VIRTUALDESK


def test_double_click_repeats_the_button_pair():
    seq = wi.build_click(0, 0, "left", True, virtual=_ONE_MONITOR)
    assert [i.union.mi.dwFlags for i in seq] == [
        wi.MOVE_FLAGS,
        wi.MOUSEEVENTF_LEFTDOWN, wi.MOUSEEVENTF_LEFTUP,
        wi.MOUSEEVENTF_LEFTDOWN, wi.MOUSEEVENTF_LEFTUP,
    ]


def test_right_and_middle_buttons_have_their_own_flags():
    right = wi.build_click(0, 0, "right", False, virtual=_ONE_MONITOR)
    assert [i.union.mi.dwFlags for i in right[1:]] == [wi.MOUSEEVENTF_RIGHTDOWN, wi.MOUSEEVENTF_RIGHTUP]
    middle = wi.build_click(0, 0, "middle", False, virtual=_ONE_MONITOR)
    assert [i.union.mi.dwFlags for i in middle[1:]] == [wi.MOUSEEVENTF_MIDDLEDOWN, wi.MOUSEEVENTF_MIDDLEUP]
    with pytest.raises(HandsError) as exc:
        wi.build_click(0, 0, "pinky", False, virtual=_ONE_MONITOR)
    assert exc.value.code == "invalid_action"


def test_scroll_carries_signed_wheel_notches():
    seq = wi.build_scroll(1, 2, -3, virtual=_ONE_MONITOR)
    assert [i.union.mi.dwFlags for i in seq] == [wi.MOVE_FLAGS, wi.MOUSEEVENTF_WHEEL]
    assert ctypes.c_int32(seq[1].union.mi.mouseData).value == -3 * wi.WHEEL_DELTA


def test_send_text_paces_one_character_per_call(monkeypatch):
    """Not cosmetic: a burst of Unicode events reaches a Text Services
    Framework control with the right length and the wrong characters, so the
    pacing is the thing that makes typing correct. See TYPE_PACING_SECONDS."""
    batches, slept = [], []
    monkeypatch.setattr(wi, "send", lambda batch: batches.append(batch) or len(batch))
    monkeypatch.setattr(wi.time, "sleep", slept.append)

    assert wi.send_text("abc") == 6
    assert [len(b) for b in batches] == [2, 2, 2]
    assert [b[0].union.ki.wScan for b in batches] == [ord("a"), ord("b"), ord("c")]
    # One gap between characters, none before the first.
    assert slept == [wi.TYPE_PACING_SECONDS] * 2


@pytest.mark.skipif(sys.platform != "win32", reason="SendInput is Win32")
def test_send_returns_count_for_an_empty_batch():
    assert wi.send([]) == 0
