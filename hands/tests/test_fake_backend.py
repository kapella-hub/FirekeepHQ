from firekeep_hands.backends.base import Control, Rect, WindowInfo
from firekeep_hands.backends.fake import FakeBackend


def _scene():
    return [Control("c1", "Button", "Save", "", Rect(10, 10, 80, 30), "Notepad", ("Invoke",)),
            Control("c2", "Edit", "Text Editor", "", Rect(0, 50, 600, 400), "Notepad", ("Value",))]


def test_observe_find_invoke_and_set_value_are_recorded():
    be = FakeBackend(_scene(), WindowInfo("Notepad", "Untitled - Notepad", 1, Rect(0, 0, 800, 600)))
    obs = be.observe(app=None, region=None, max_nodes=200, text_budget=4000, screenshot=False, max_width=1280)
    assert [c.ref for c in obs.controls] == ["c1", "c2"] and obs.generation == 1
    assert be.find("save", role=None, app=None, limit=5)[0].ref == "c1"
    be.invoke(obs.controls[0]); be.set_value(obs.controls[1], "hello")
    assert be.calls[-2:] == [("invoke", "c1"), ("set_value", "c2", "hello")] and be.values["c2"] == "hello"


def test_max_nodes_truncates():
    be = FakeBackend(_scene())
    obs = be.observe(app=None, region=None, max_nodes=1, text_budget=4000, screenshot=False, max_width=1280)
    assert len(obs.controls) == 1 and obs.truncated is True
