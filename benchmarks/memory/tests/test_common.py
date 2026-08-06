import pytest
from bench import common


def test_sanitize_namespace_is_server_legal():
    ns = common.sanitize_namespace("q_Multi.1/weird id")
    assert ns == "lm_q_multi_1_weird_id"
    assert len(ns) <= 200


def test_session_tag_roundtrip():
    tag = common.session_tag("s_abc123")
    assert tag == "lm_session:s_abc123"
    assert common.parse_session_tag(["other", tag]) == "s_abc123"


def test_parse_session_tag_missing_returns_none():
    assert common.parse_session_tag(["lm_date:2023/04/01"]) is None


def test_parse_date_tag_roundtrip():
    tag = common.date_tag("2023/04/01 (Sat) 09:00")
    assert tag == "lm_date:2023/04/01 (Sat) 09:00"
    assert common.parse_date_tag(["other", tag]) == "2023/04/01 (Sat) 09:00"


def test_parse_date_tag_missing_returns_none():
    assert common.parse_date_tag(["lm_session:s_abc123"]) is None


def test_load_dataset_verifies(fixture_dataset):
    rows = common.load_dataset(fixture_dataset)
    assert len(rows) == 2


def test_verify_dataset_names_missing_key():
    with pytest.raises(ValueError, match="answer_session_ids"):
        common.verify_dataset([{"question_id": "x"}])


def test_is_abstention():
    assert common.is_abstention("q_skip_1_abs")
    assert not common.is_abstention("q_multi_1")


# --- run-label scoping ------------------------------------------------------

def test_sanitize_label_keeps_ordinary_labels_readable():
    # The readable stem survives verbatim; only a short digest of the raw label
    # is appended, so `work/` is still browsable by eye.
    for label in ("post-dream", "full_v1.2"):
        segment = common.sanitize_label(label)
        assert segment.startswith(f"{label}-")
        assert len(segment) == len(label) + 1 + 8


def test_sanitize_label_is_stable_across_calls():
    # The directory a label resolves to must not move between invocations, or
    # resume-within-a-label breaks.
    assert common.sanitize_label("pre-dream") == common.sanitize_label("pre-dream")
    # Shell whitespace is not a different leg.
    assert common.sanitize_label(" pre-dream ") == common.sanitize_label("pre-dream")


@pytest.mark.parametrize("one,two", [
    ("post dream", "post_dream"),      # space vs underscore
    ("post/dream", "post_dream"),      # separator vs underscore
    ("post dream", "post/dream"),      # two distinct hostile labels
    ("x" * 110 + "-a", "x" * 110 + "-b"),  # differ only past the 100-char cut
])
def test_distinct_labels_never_share_a_work_directory(one, two, tmp_path):
    """Finding 2. A shared directory is a shared `recall_<config>.jsonl`, which
    restores the cross-label resume leak label-scoping exists to prevent."""
    assert common.sanitize_label(one) != common.sanitize_label(two)
    assert (common.run_work_dir(one, work_dir=tmp_path)
            != common.run_work_dir(two, work_dir=tmp_path))


@pytest.mark.parametrize("hostile", ["../../etc", "a/b", "a\\b", "..", ".", "  "])
def test_sanitize_label_cannot_escape_the_work_dir(hostile, tmp_path):
    label = common.sanitize_label(hostile)
    assert "/" not in label and "\\" not in label
    assert label not in ("", ".", "..")
    resolved = common.run_work_dir(hostile, work_dir=tmp_path).resolve()
    assert resolved.parent == tmp_path.resolve()


def test_run_work_dir_differs_per_label(tmp_path):
    a = common.run_work_dir("pre-dream", work_dir=tmp_path)
    b = common.run_work_dir("post-dream", work_dir=tmp_path)
    assert a != b
    assert a.parent == b.parent == tmp_path


def test_legacy_unscoped_artefacts_lists_pre_scoping_files(tmp_path):
    (tmp_path / "recall_bench.jsonl").write_text("{}", encoding="utf-8")
    (tmp_path / "scores_defaults.json").write_text("{}", encoding="utf-8")
    (tmp_path / "qa_bench.jsonl").write_text("{}", encoding="utf-8")
    # Shared/unrelated files and the scoped dir itself must not be listed.
    (tmp_path / "ingest_ledger.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "post-dream").mkdir()
    (tmp_path / "post-dream" / "recall_bench.jsonl").write_text("{}", encoding="utf-8")

    names = [p.name for p in common.legacy_unscoped_artefacts(tmp_path)]
    assert names == ["qa_bench.jsonl", "recall_bench.jsonl", "scores_defaults.json"]


def test_legacy_unscoped_artefacts_empty_when_clean(tmp_path):
    assert common.legacy_unscoped_artefacts(tmp_path) == []
    assert common.legacy_unscoped_artefacts(tmp_path / "nope") == []
