"""SP0 C7 (defect #15) — the BM25 pre-filter is deleted, not just unused.

It queried a fulltext index ('mc_fulltext') that nothing ever creates
(ensure_indexes creates 'node_fulltext'), swallowed the ClientError, and
returned [] on every call since it shipped. These tests guard against
reintroduction without a design."""

import inspect
import pathlib


def test_bm25_prefilter_removed_from_rag_engine():
    from app.engine.rag import RAGEngine

    assert not hasattr(RAGEngine, "_bm25_prefilter")


def test_get_domains_for_keywords_removed_from_graph():
    from app.db.graph import Neo4jClient

    assert not hasattr(Neo4jClient, "get_domains_for_keywords")
    # The public extract_keywords wrapper is deleted too — only the BM25
    # pre-filter ever called it.
    assert not hasattr(Neo4jClient, "extract_keywords")
    # The private keyword extractor stays — _fetch_resolutions uses it.
    assert hasattr(Neo4jClient, "_extract_keywords")


def test_filter_domains_param_removed_from_vector_search():
    from app.db.vector import VectorClient

    params = inspect.signature(VectorClient.search).parameters
    assert "filter_domains" not in params


def test_mc_fulltext_not_referenced_in_app_code():
    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = [
        str(p)
        for p in app_dir.rglob("*.py")
        if "mc_fulltext" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
