import pytest
from firekeep_hands import routing
from firekeep_hands.backends.base import Control, HandsError, Observation, Rect

def _obs():
    return Observation(1, None, [
        Control("b", "Button", "OK", "", Rect(100, 100, 50, 20), "App", ("Invoke",)),
        Control("e", "Edit", "Name", "", Rect(0, 0, 200, 30), "App", ("Value",)),
        Control("p", "Pane", "Canvas", "", Rect(0, 0, 400, 400), "App", ()),
    ], "", None, False)

def test_invoke_prefers_accessibility_and_click_uses_centre():
    r = routing.route({"kind": "invoke", "ref": "b"}, _obs())
    assert (r.route, r.point) == ("accessibility", None)
    r = routing.route({"kind": "click", "ref": "b"}, _obs())
    assert (r.route, r.point) == ("pixel", (125, 110))

def test_invoke_without_pattern_falls_back_to_pixel():
    assert routing.route({"kind": "invoke", "ref": "p"}, _obs()).route == "pixel"

def test_set_value_routes_by_pattern():
    assert routing.route({"kind": "set_value", "ref": "e", "value": "x"}, _obs()).route == "accessibility"
    assert routing.route({"kind": "set_value", "ref": "p", "value": "x"}, _obs()).route == "pixel+type"

@pytest.mark.parametrize("bad", [
    {"kind": "click", "x": 10, "y": 10},
    {"kind": "click", "ref": "b", "point": [1, 2]},
    {"kind": "teleport"},
    {"kind": "wait", "seconds": 99},
    {"kind": "click"},
])
def test_invalid_actions_are_rejected(bad):
    with pytest.raises(HandsError) as ei:
        routing.route(bad, _obs())
    assert ei.value.code == "invalid_action"

@pytest.mark.parametrize("bad", [
    ["kind", "wait"],
    "wait",
    42,
    None,
    {"kind": {"nested": "object"}},
    {"kind": 7},
    {"kind": None},
])
def test_a_malformed_envelope_is_an_invalid_action_not_a_type_error(bad):
    """The envelope before its contents: a list made the forbidden-key scan
    raise TypeError and a dict `kind` made the lookup raise "unhashable
    type", both of which reached the caller as an internal failure."""
    with pytest.raises(HandsError) as ei:
        routing.route(bad, _obs())
    assert ei.value.code == "invalid_action"


def test_unknown_or_stale_ref_is_rejected():
    with pytest.raises(HandsError) as ei:
        routing.route({"kind": "click", "ref": "zzz"}, _obs())
    assert ei.value.code == "stale_ref"
    with pytest.raises(HandsError):
        routing.route({"kind": "click", "ref": "b"}, None)

def test_type_is_capped_so_a_long_string_is_not_a_long_window_of_stray_keys():
    """Typed text is paced character by character, so its length is a
    duration during which keystrokes land wherever the foreground is. Past
    the cap the answer is set_value on a field that exposes a value pattern,
    which delivers the text to one named control in one call."""
    assert routing.route({"kind": "type", "text": "x" * routing.MAX_TYPE_CHARS}, _obs()).route == "input"
    with pytest.raises(HandsError) as ei:
        routing.route({"kind": "type", "text": "x" * (routing.MAX_TYPE_CHARS + 1)}, _obs())
    assert ei.value.code == "invalid_action"
    assert "set_value" in str(ei.value)

def test_the_pixel_fallback_for_set_value_is_capped_the_same_way_type_is():
    """`pixel+type` is click + select-all + type_text — the same paced
    injection the `type` cap exists to bound, reached by the very escape hatch
    that cap points people at. Uncapped, a 4000 character value routed here
    with len == 4000: about a hundred seconds of keystrokes landing wherever
    the foreground goes."""
    long_value = "x" * (routing.MAX_TYPE_CHARS + 1)
    with pytest.raises(HandsError) as ei:
        routing.route({"kind": "set_value", "ref": "p", "value": long_value}, _obs())
    assert ei.value.code == "invalid_action"
    assert str(routing.MAX_TYPE_CHARS) in str(ei.value)
    assert "no Value/AXValue pattern" in str(ei.value)

    at_the_cap = "x" * routing.MAX_TYPE_CHARS
    assert routing.route({"kind": "set_value", "ref": "p", "value": at_the_cap},
                         _obs()).route == "pixel+type"

def test_the_accessibility_route_for_set_value_stays_uncapped():
    """Nothing is typed there — the whole string goes to the control in one
    call — so capping it would refuse a document for no reason. This is the
    route the `type` cap's own advice sends people to."""
    r = routing.route({"kind": "set_value", "ref": "e", "value": "x" * 4000}, _obs())
    assert r.route == "accessibility" and len(r.payload["value"]) == 4000

def test_scroll_window_needs_no_ref():
    r = routing.route({"kind": "scroll", "ref": "window", "dy": -3}, _obs())
    assert r.route == "pixel" and r.point is None and r.payload["dy"] == -3
