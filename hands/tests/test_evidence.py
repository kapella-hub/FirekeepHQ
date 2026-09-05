import datetime as dt, hashlib, json
from firekeep_hands import evidence, paths


def test_ledger_writes_chained_lines_and_images(isolated_home):
    led = evidence.Ledger("t1", goal="g", apps=["Notepad"], machine_id="m", session_id="s")
    assert (led.dir / "task.json").exists() and led.dir.parent == paths.evidence_root()
    l1 = led.record(step_index=0, action={"kind": "wait", "seconds": 1}, route="none", classes=(), permit=None,
                    before_png=b"\x89PNG1", after_png=None, outcome="ok", error=None)
    l2 = led.record(step_index=1, action={"kind": "key", "chord": "ctrl+s"}, route="shortcut", classes=(), permit=None,
                    before_png=None, after_png=b"\x89PNG2", outcome="ok", error=None)
    lines = (led.dir / "steps.jsonl").read_text().splitlines()
    assert len(lines) == 2 and (led.dir / "000-before.png").read_bytes() == b"\x89PNG1" and (led.dir / "001-after.png").exists()
    assert l1["before"] == hashlib.sha256(b"\x89PNG1").hexdigest() and l1["after"] is None
    body1 = json.dumps({k: v for k, v in l1.items() if k != "chain"}, sort_keys=True, separators=(",", ":"))
    assert l1["chain"] == hashlib.sha256(("" + body1).encode()).hexdigest()
    body2 = json.dumps({k: v for k, v in l2.items() if k != "chain"}, sort_keys=True, separators=(",", ":"))
    assert l2["chain"] == hashlib.sha256((l1["chain"] + body2).encode()).hexdigest()
    led.close("done", "saved")
    assert json.loads((led.dir / "task.json").read_text())["outcome"] == "done"


def test_prune_removes_only_old_tasks(isolated_home):
    root = paths.evidence_root(); root.mkdir(parents=True)
    for name, started in (("old", "2026-01-01T00:00:00Z"), ("new", "2026-09-04T00:00:00Z")):
        d = root / name; d.mkdir(); (d / "task.json").write_text(json.dumps({"started": started}))
    now = dt.datetime(2026, 9, 5, tzinfo=dt.timezone.utc)
    assert evidence.prune(root, older_than_days=14, now=now) == 1
    assert not (root / "old").exists() and (root / "new").exists()


def test_ledger_steps_returns_recorded_lines(isolated_home):
    led = evidence.Ledger("t2", goal="g", apps=[], machine_id="m", session_id="s")
    assert led.steps() == []
    l1 = led.record(step_index=0, action={"kind": "wait"}, route="none", classes=(), permit=None,
                    before_png=None, after_png=None, outcome="ok", error=None)
    assert led.steps() == [l1]


def test_prune_leaves_unreadable_or_missing_task_json_alone(isolated_home):
    root = paths.evidence_root(); root.mkdir(parents=True)
    (root / "no-task-json").mkdir()
    bad = root / "bad-json"; bad.mkdir(); (bad / "task.json").write_text("not json")
    now = dt.datetime(2026, 9, 5, tzinfo=dt.timezone.utc)
    assert evidence.prune(root, older_than_days=14, now=now) == 0
    assert (root / "no-task-json").exists() and (root / "bad-json").exists()


def test_record_with_permit_dict_round_trips(isolated_home):
    led = evidence.Ledger("t3", goal="g", apps=[], machine_id="m", session_id="s")
    permit = {"challenge": "c1", "verdict": "approve"}
    line = led.record(step_index=0, action={"kind": "click"}, route="permit", classes=("send",), permit=permit,
                      before_png=None, after_png=None, outcome="ok", error=None)
    assert line["permit"] == permit and line["classes"] == ["send"]
