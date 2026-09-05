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

def test_unknown_or_stale_ref_is_rejected():
    with pytest.raises(HandsError) as ei:
        routing.route({"kind": "click", "ref": "zzz"}, _obs())
    assert ei.value.code == "stale_ref"
    with pytest.raises(HandsError):
        routing.route({"kind": "click", "ref": "b"}, None)

def test_scroll_window_needs_no_ref():
    r = routing.route({"kind": "scroll", "ref": "window", "dy": -3}, _obs())
    assert r.route == "pixel" and r.point is None and r.payload["dy"] == -3
