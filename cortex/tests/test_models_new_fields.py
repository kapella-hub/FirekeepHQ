"""Tests for new fields added in the memory-improvements phase."""

from app.models import ActionLog, ContextQuery, RecallResponse


def test_action_log_new_fields_have_defaults():
    log = ActionLog(action="did something", outcome="it worked")
    assert log.access_count == 0
    assert log.last_recalled_at is None
    assert log.importance_score == 0.0
    assert log.project is None


def test_action_log_project_lowercased():
    log = ActionLog(action="a", outcome="b", project="MyApp")
    assert log.project == "myapp"


def test_context_query_new_fields():
    q = ContextQuery(task="find auth bugs")
    assert q.project is None
    assert q.token_budget == 600
    assert q.format == "synthesized"


def test_context_query_project_lowercased():
    q = ContextQuery(task="find bugs", project="MyApp")
    assert q.project == "myapp"


def test_recall_response_new_fields():
    r = RecallResponse(context_block="ctx", sources=[], score=0.5)
    assert r.tokens_used == 0
    assert r.token_budget == 600
    assert r.format == "raw"
