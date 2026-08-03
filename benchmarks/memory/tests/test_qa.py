import pytest

from bench import qa


def test_refuse_cloud():
    with pytest.raises(ValueError, match="cloud"):
        qa.refuse_cloud("minimax-m2:cloud")
    qa.refuse_cloud("qwen3:14b")  # no raise


def test_parse_verdict():
    assert qa.parse_verdict("blah\nVERDICT: CORRECT") is True
    assert qa.parse_verdict("VERDICT: INCORRECT") is False
    assert qa.parse_verdict("no verdict here") is None
    # Last token wins when the model narrates both.
    assert qa.parse_verdict("VERDICT: CORRECT ... VERDICT: INCORRECT") is False


def test_score_abstention():
    assert qa.score_abstention("I don't know.")
    assert qa.score_abstention("There is no information about that.")
    assert not qa.score_abstention("Your dog's name is Rex.")


def test_reader_messages_pin_the_contract():
    msgs = qa.reader_messages("Q?", "2023/05/20", "CTX")
    joined = " ".join(m["content"] for m in msgs)
    assert "CTX" in joined and "Q?" in joined
    assert "I don't know." in joined  # abstention contract is in the prompt


def test_context_from_hits_prefixes_with_date():
    # With date: should prefix content
    hits_with_date = [
        {"content": "I'm planning a move...", "date": "2023/04/01 (Sat) 09:00"},
        {"content": "Just arrived...", "date": "2023/04/10 (Mon) 09:00"},
    ]
    ctx = qa._context_from_hits(hits_with_date)
    assert "[2023/04/01 (Sat) 09:00] I'm planning a move..." in ctx
    assert "[2023/04/10 (Mon) 09:00] Just arrived..." in ctx
    # Without date: no prefix
    hits_no_date = [
        {"content": "Some memory", "date": None},
    ]
    ctx = qa._context_from_hits(hits_no_date)
    assert ctx == "Some memory"
    assert "[" not in ctx
