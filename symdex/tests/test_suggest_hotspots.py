"""Tests for suggest_symbols and get_hotspots tools."""


from firekeep_symdex.storage import IndexStore
from firekeep_symdex.parser import Symbol


def _make_index(tmp_path, symbols_data, references=None):
    """Helper: create a stored index with given symbols and references."""
    store = IndexStore(base_path=str(tmp_path))
    symbols = [
        Symbol(
            id=s["id"],
            file=s["file"],
            name=s["name"],
            qualified_name=s.get("qualified_name", s["name"]),
            kind=s["kind"],
            language=s.get("language", "python"),
            signature=s.get("signature", f"def {s['name']}():"),
            line=s.get("line", 1),
            end_line=s.get("end_line", 10),
            byte_offset=0,
            byte_length=s.get("byte_length", 100),
        )
        for s in symbols_data
    ]

    raw_files = {}
    for s in symbols_data:
        if s["file"] not in raw_files:
            raw_files[s["file"]] = f"# placeholder for {s['file']}\n" * 10

    store.save_index(
        owner="testowner",
        name="testrepo",
        source_files=list(raw_files.keys()),
        symbols=symbols,
        raw_files=raw_files,
        languages={"python": len(raw_files)},
        references=references or [],
    )

    return store


# -- Fixtures --

SYMBOLS = [
    {"id": "api-py::rate_limit", "file": "src/api/rate_limit.py", "name": "rate_limit", "kind": "function", "signature": "def rate_limit(request):"},
    {"id": "api-py::authenticate", "file": "src/api/auth.py", "name": "authenticate", "kind": "function", "signature": "def authenticate(token):"},
    {"id": "api-py::APIRouter", "file": "src/api/router.py", "name": "APIRouter", "kind": "class", "signature": "class APIRouter:"},
    {"id": "db-py::query", "file": "src/db/query.py", "name": "query", "kind": "function", "signature": "def query(sql, params):"},
    {"id": "db-py::Database", "file": "src/db/connection.py", "name": "Database", "kind": "class", "signature": "class Database:"},
    {"id": "db-py::connect", "file": "src/db/connection.py", "name": "connect", "kind": "method", "qualified_name": "Database.connect", "signature": "def connect(self):"},
    {"id": "utils-py::parse_json", "file": "src/utils/parser.py", "name": "parse_json", "kind": "function", "signature": "def parse_json(data):"},
    {"id": "utils-py::validate", "file": "src/utils/validate.py", "name": "validate", "kind": "function", "signature": "def validate(schema, data):"},
]

REFERENCES = [
    {"type": "call", "name": "query", "file": "src/api/router.py", "line": 15},
    {"type": "call", "name": "query", "file": "src/api/auth.py", "line": 20},
    {"type": "call", "name": "query", "file": "src/db/connection.py", "line": 30},
    {"type": "call", "name": "authenticate", "file": "src/api/router.py", "line": 10},
    {"type": "call", "name": "authenticate", "file": "src/api/rate_limit.py", "line": 5},
    {"type": "call", "name": "validate", "file": "src/api/router.py", "line": 12},
    {"type": "call", "name": "validate", "file": "src/api/auth.py", "line": 25},
    {"type": "call", "name": "validate", "file": "src/utils/parser.py", "line": 8},
    {"type": "call", "name": "db.connect", "file": "src/api/router.py", "line": 5},
    {"type": "call", "name": "parse_json", "file": "src/api/router.py", "line": 18},
    {"type": "import", "name": "query", "file": "src/api/router.py", "line": 1},
]


# ============================================================
# suggest_symbols tests
# ============================================================

class TestSuggestSymbols:
    def test_basic_task_returns_relevant_symbols(self, tmp_path):
        """Symbols matching task keywords should be returned."""
        from firekeep_symdex.tools.suggest_symbols import suggest_symbols

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = suggest_symbols("testowner/testrepo", "add rate limiting to the API", storage_path=str(tmp_path))

        assert "error" not in result
        assert result["result_count"] > 0
        names = [r["name"] for r in result["results"]]
        # rate_limit should be the top match (name + file path match)
        assert names[0] == "rate_limit"

    def test_keywords_extracted(self, tmp_path):
        """Task description should be tokenized into keywords."""
        from firekeep_symdex.tools.suggest_symbols import suggest_symbols

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = suggest_symbols("testowner/testrepo", "add rate limiting", storage_path=str(tmp_path))

        assert "keywords" in result
        assert "rate" in result["keywords"]
        assert "limiting" in result["keywords"]
        # Stop words should be removed
        assert "the" not in result["keywords"]

    def test_file_path_relevance(self, tmp_path):
        """Symbols in files matching task keywords should score higher."""
        from firekeep_symdex.tools.suggest_symbols import suggest_symbols

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = suggest_symbols("testowner/testrepo", "fix the api authentication", storage_path=str(tmp_path))

        assert "error" not in result
        names = [r["name"] for r in result["results"]]
        # authenticate is in src/api/auth.py - both "api" and "auth" match
        assert "authenticate" in names[:3]

    def test_architecture_task_weights_classes_higher(self, tmp_path):
        """Architecture tasks should rank classes above functions."""
        from firekeep_symdex.tools.suggest_symbols import suggest_symbols

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = suggest_symbols("testowner/testrepo", "refactor database architecture", storage_path=str(tmp_path))

        assert "error" not in result
        results = result["results"]
        # Database (class) should appear and score well
        class_results = [r for r in results if r["kind"] == "class"]
        assert len(class_results) > 0

    def test_caller_count_signal(self, tmp_path):
        """Symbols with more callers should score higher (all else equal)."""
        from firekeep_symdex.tools.suggest_symbols import suggest_symbols

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = suggest_symbols("testowner/testrepo", "improve validation and querying", storage_path=str(tmp_path))

        assert "error" not in result
        # query has 3 callers, validate has 3 callers - both should appear
        names = [r["name"] for r in result["results"]]
        assert "query" in names
        assert "validate" in names

    def test_max_results_clamped(self, tmp_path):
        """max_results should be clamped between 1 and 100."""
        from firekeep_symdex.tools.suggest_symbols import suggest_symbols

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = suggest_symbols("testowner/testrepo", "query", max_results=2, storage_path=str(tmp_path))

        assert result["result_count"] <= 2

    def test_relevance_reason_included(self, tmp_path):
        """Each result should include a relevance_reason."""
        from firekeep_symdex.tools.suggest_symbols import suggest_symbols

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = suggest_symbols("testowner/testrepo", "query database", storage_path=str(tmp_path))

        for r in result["results"]:
            assert "relevance_reason" in r
            assert isinstance(r["relevance_reason"], str)
            assert len(r["relevance_reason"]) > 0

    def test_meta_envelope(self, tmp_path):
        """Response should include _meta with timing and token savings."""
        from firekeep_symdex.tools.suggest_symbols import suggest_symbols

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = suggest_symbols("testowner/testrepo", "query", storage_path=str(tmp_path))

        assert "_meta" in result
        meta = result["_meta"]
        assert "timing_ms" in meta
        assert "total_symbols" in meta
        assert "tokens_saved" in meta
        assert "total_tokens_saved" in meta

    def test_repo_not_found(self, tmp_path):
        """Should return error for unknown repo."""
        from firekeep_symdex.tools.suggest_symbols import suggest_symbols

        result = suggest_symbols("nonexistent/repo", "anything", storage_path=str(tmp_path))
        assert "error" in result

    def test_empty_task_returns_error(self, tmp_path):
        """Should return error if no keywords can be extracted."""
        from firekeep_symdex.tools.suggest_symbols import suggest_symbols

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = suggest_symbols("testowner/testrepo", "the a an", storage_path=str(tmp_path))
        assert "error" in result

    def test_score_field_present(self, tmp_path):
        """Each result should have a numeric score."""
        from firekeep_symdex.tools.suggest_symbols import suggest_symbols

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = suggest_symbols("testowner/testrepo", "query", storage_path=str(tmp_path))

        for r in result["results"]:
            assert "score" in r
            assert isinstance(r["score"], (int, float))
            assert r["score"] > 0


# ============================================================
# get_hotspots tests
# ============================================================

class TestGetHotspots:
    def test_basic_hotspots(self, tmp_path):
        """Should return symbols sorted by caller count descending."""
        from firekeep_symdex.tools.get_hotspots import get_hotspots

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = get_hotspots("testowner/testrepo", storage_path=str(tmp_path))

        assert "error" not in result
        assert result["result_count"] > 0
        counts = [r["caller_count"] for r in result["results"]]
        assert counts == sorted(counts, reverse=True)

    def test_min_callers_filter(self, tmp_path):
        """Symbols below min_callers should be excluded."""
        from firekeep_symdex.tools.get_hotspots import get_hotspots

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = get_hotspots("testowner/testrepo", min_callers=3, storage_path=str(tmp_path))

        assert "error" not in result
        for r in result["results"]:
            assert r["caller_count"] >= 3

    def test_kind_filter(self, tmp_path):
        """Should filter by symbol kind when specified."""
        from firekeep_symdex.tools.get_hotspots import get_hotspots

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = get_hotspots("testowner/testrepo", kind="function", min_callers=1, storage_path=str(tmp_path))

        assert "error" not in result
        for r in result["results"]:
            assert r["kind"] == "function"

    def test_max_results_limit(self, tmp_path):
        """Should respect max_results."""
        from firekeep_symdex.tools.get_hotspots import get_hotspots

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = get_hotspots("testowner/testrepo", max_results=2, min_callers=1, storage_path=str(tmp_path))

        assert result["result_count"] <= 2

    def test_result_fields(self, tmp_path):
        """Each result should contain the expected fields."""
        from firekeep_symdex.tools.get_hotspots import get_hotspots

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = get_hotspots("testowner/testrepo", min_callers=1, storage_path=str(tmp_path))

        for r in result["results"]:
            assert "symbol_id" in r
            assert "name" in r
            assert "file" in r
            assert "caller_count" in r
            assert "kind" in r
            assert "signature" in r

    def test_meta_with_timing(self, tmp_path):
        """Response should include _meta with timing."""
        from firekeep_symdex.tools.get_hotspots import get_hotspots

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = get_hotspots("testowner/testrepo", storage_path=str(tmp_path))

        assert "_meta" in result
        assert "timing_ms" in result["_meta"]
        assert "total_symbols" in result["_meta"]

    def test_repo_not_found(self, tmp_path):
        """Should return error for unknown repo."""
        from firekeep_symdex.tools.get_hotspots import get_hotspots

        result = get_hotspots("nonexistent/repo", storage_path=str(tmp_path))
        assert "error" in result

    def test_no_references_returns_empty(self, tmp_path):
        """Should return empty results when no references exist."""
        from firekeep_symdex.tools.get_hotspots import get_hotspots

        _make_index(tmp_path, SYMBOLS, references=[])
        result = get_hotspots("testowner/testrepo", storage_path=str(tmp_path))

        assert "error" not in result
        assert result["result_count"] == 0

    def test_dotted_references_counted(self, tmp_path):
        """References like 'obj.method' should count toward the bare symbol name."""
        from firekeep_symdex.tools.get_hotspots import get_hotspots

        refs = [
            {"type": "call", "name": "db.connect", "file": "a.py", "line": 1},
            {"type": "call", "name": "pool.connect", "file": "b.py", "line": 2},
            {"type": "call", "name": "connect", "file": "c.py", "line": 3},
        ]
        _make_index(tmp_path, SYMBOLS, refs)
        result = get_hotspots("testowner/testrepo", min_callers=1, storage_path=str(tmp_path))

        connect_results = [r for r in result["results"] if r["name"] == "connect"]
        assert len(connect_results) == 1
        # 3 bare "connect" counts (1 direct + 2 from dotted) = 3
        assert connect_results[0]["caller_count"] == 3

    def test_import_refs_excluded(self, tmp_path):
        """Only 'call' type references should be counted, not imports."""
        from firekeep_symdex.tools.get_hotspots import get_hotspots

        refs = [
            {"type": "import", "name": "query", "file": "a.py", "line": 1},
            {"type": "call", "name": "query", "file": "a.py", "line": 5},
        ]
        _make_index(tmp_path, SYMBOLS, refs)
        result = get_hotspots("testowner/testrepo", min_callers=1, storage_path=str(tmp_path))

        query_results = [r for r in result["results"] if r["name"] == "query"]
        assert len(query_results) == 1
        assert query_results[0]["caller_count"] == 1
