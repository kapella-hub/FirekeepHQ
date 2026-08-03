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


def test_load_dataset_verifies(fixture_dataset):
    rows = common.load_dataset(fixture_dataset)
    assert len(rows) == 2


def test_verify_dataset_names_missing_key():
    with pytest.raises(ValueError, match="answer_session_ids"):
        common.verify_dataset([{"question_id": "x"}])


def test_is_abstention():
    assert common.is_abstention("q_skip_1_abs")
    assert not common.is_abstention("q_multi_1")
