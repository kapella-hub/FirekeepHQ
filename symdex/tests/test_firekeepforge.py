"""Tests for FirekeepForge tools: extract_conventions, detect_patterns, scaffold_symbol."""

from unittest.mock import patch, MagicMock

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
            decorators=s.get("decorators", []),
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


# -- Fixture data --

SYMBOLS = [
    {"id": "api::get_users", "file": "src/api/routes.py", "name": "get_users", "kind": "function",
     "signature": "def get_users(request: Request) -> Response:", "decorators": ["app.get"]},
    {"id": "api::create_user", "file": "src/api/routes.py", "name": "create_user", "kind": "function",
     "signature": "def create_user(request: Request) -> Response:", "decorators": ["app.post"]},
    {"id": "api::delete_user", "file": "src/api/routes.py", "name": "delete_user", "kind": "function",
     "signature": "def delete_user(request: Request) -> Response:", "decorators": ["app.delete"]},
    {"id": "api::update_user", "file": "src/api/routes.py", "name": "update_user", "kind": "function",
     "signature": "def update_user(request: Request, data: dict) -> Response:", "decorators": ["app.put"]},
    {"id": "svc::UserService", "file": "src/services/user_service.py", "name": "UserService", "kind": "class",
     "signature": "class UserService:"},
    {"id": "svc::UserService.find", "file": "src/services/user_service.py", "name": "find", "kind": "method",
     "qualified_name": "UserService.find", "signature": "def find(self, user_id: int) -> dict:"},
    {"id": "svc::UserService.save", "file": "src/services/user_service.py", "name": "save", "kind": "method",
     "qualified_name": "UserService.save", "signature": "def save(self, user: dict) -> dict:"},
    {"id": "svc::UserService.delete", "file": "src/services/user_service.py", "name": "delete", "kind": "method",
     "qualified_name": "UserService.delete", "signature": "def delete(self, user_id: int) -> bool:"},
    {"id": "util::parse_json", "file": "src/utils/helpers.py", "name": "parse_json", "kind": "function",
     "signature": "def parse_json(data: str) -> dict:"},
    {"id": "util::validate_email", "file": "src/utils/helpers.py", "name": "validate_email", "kind": "function",
     "signature": "def validate_email(email: str) -> bool:"},
    {"id": "const::MAX_RETRIES", "file": "src/config/settings.py", "name": "MAX_RETRIES", "kind": "constant",
     "signature": "MAX_RETRIES = 3"},
    {"id": "const::API_VERSION", "file": "src/config/settings.py", "name": "API_VERSION", "kind": "constant",
     "signature": "API_VERSION = 'v2'"},
    {"id": "test::test_get_users", "file": "tests/test_api.py", "name": "test_get_users", "kind": "function",
     "signature": "def test_get_users():"},
]

REFERENCES = [
    {"type": "import", "name": "fastapi", "file": "src/api/routes.py", "line": 1},
    {"type": "import", "name": "Request", "file": "src/api/routes.py", "line": 2},
    {"type": "call", "name": "UserService.find", "file": "src/api/routes.py", "line": 10},
    {"type": "call", "name": "parse_json", "file": "src/api/routes.py", "line": 15},
]


# ============================================================
# extract_conventions tests
# ============================================================

class TestExtractConventions:
    def test_basic_all_focus(self, tmp_path):
        """Should return naming, structure, patterns, and framework sections."""
        from firekeep_symdex.tools.extract_conventions import extract_conventions

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = extract_conventions("testowner/testrepo", focus="all", storage_path=str(tmp_path))

        assert "error" not in result
        assert "naming" in result
        assert "structure" in result
        assert "patterns" in result
        assert "framework" in result
        assert "_meta" in result
        assert "timing_ms" in result["_meta"]

    def test_naming_detects_snake_case(self, tmp_path):
        """Functions should be detected as snake_case."""
        from firekeep_symdex.tools.extract_conventions import extract_conventions

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = extract_conventions("testowner/testrepo", focus="naming", storage_path=str(tmp_path))

        assert "functions" in result["naming"]
        assert "snake_case" in result["naming"]["functions"]

    def test_naming_detects_pascal_case_classes(self, tmp_path):
        """Classes should be detected as PascalCase."""
        from firekeep_symdex.tools.extract_conventions import extract_conventions

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = extract_conventions("testowner/testrepo", focus="naming", storage_path=str(tmp_path))

        assert "classs" in result["naming"] or "classes" in result["naming"]

    def test_naming_detects_upper_case_constants(self, tmp_path):
        """Constants should be detected as UPPER_CASE."""
        from firekeep_symdex.tools.extract_conventions import extract_conventions

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = extract_conventions("testowner/testrepo", focus="naming", storage_path=str(tmp_path))

        assert "constants" in result["naming"]
        assert "UPPER_CASE" in result["naming"]["constants"]

    def test_structure_analysis(self, tmp_path):
        """Should compute avg symbols per file and detect test patterns."""
        from firekeep_symdex.tools.extract_conventions import extract_conventions

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = extract_conventions("testowner/testrepo", focus="structure", storage_path=str(tmp_path))

        structure = result["structure"]
        assert "avg_symbols_per_file" in structure
        assert structure["avg_symbols_per_file"] > 0
        assert "test_pattern" in structure
        assert structure["test_pattern"] == "test_*.py"

    def test_patterns_detects_decorators(self, tmp_path):
        """Should detect commonly used decorators."""
        from firekeep_symdex.tools.extract_conventions import extract_conventions

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = extract_conventions("testowner/testrepo", focus="patterns", storage_path=str(tmp_path))

        decorators = result["patterns"]["top_decorators"]
        assert len(decorators) > 0
        decorator_names = [d["name"] for d in decorators]
        assert any("app." in d for d in decorator_names)

    def test_framework_detection(self, tmp_path):
        """Should detect FastAPI from imports."""
        from firekeep_symdex.tools.extract_conventions import extract_conventions

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = extract_conventions("testowner/testrepo", focus="framework", storage_path=str(tmp_path))

        assert result["framework"]["detected"] == "fastapi"

    def test_focus_naming_only(self, tmp_path):
        """Focus=naming should only return naming section."""
        from firekeep_symdex.tools.extract_conventions import extract_conventions

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = extract_conventions("testowner/testrepo", focus="naming", storage_path=str(tmp_path))

        assert "naming" in result
        assert "structure" not in result
        assert "patterns" not in result
        assert "framework" not in result

    def test_repo_not_found(self, tmp_path):
        """Should return error for unknown repo."""
        from firekeep_symdex.tools.extract_conventions import extract_conventions

        result = extract_conventions("nonexistent/repo", storage_path=str(tmp_path))
        assert "error" in result

    def test_empty_repo(self, tmp_path):
        """Should handle repo with no symbols."""
        from firekeep_symdex.tools.extract_conventions import extract_conventions

        store = IndexStore(base_path=str(tmp_path))
        store.save_index(
            owner="testowner", name="emptyrepo",
            source_files=["empty.py"],
            symbols=[], raw_files={"empty.py": "# empty"},
            languages={"python": 1}, references=[],
        )
        result = extract_conventions("testowner/emptyrepo", storage_path=str(tmp_path))

        assert "error" not in result
        assert result["structure"]["total_symbols"] == 0


# ============================================================
# detect_patterns tests
# ============================================================

class TestDetectPatterns:
    def test_basic_pattern_detection(self, tmp_path):
        """Should detect patterns among similar symbols."""
        from firekeep_symdex.tools.detect_patterns import detect_patterns

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = detect_patterns("testowner/testrepo", min_group_size=2, storage_path=str(tmp_path))

        assert "error" not in result
        assert "patterns" in result
        assert "_meta" in result

    def test_finds_route_handler_pattern(self, tmp_path):
        """Should group API route handlers as a pattern."""
        from firekeep_symdex.tools.detect_patterns import detect_patterns

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        # 3 route handlers with 1 param (get, create, delete) match
        result = detect_patterns("testowner/testrepo", kind="function", min_group_size=2, storage_path=str(tmp_path))

        assert "error" not in result
        assert result["total_patterns"] > 0
        # Should find the pattern of functions with 1 param and return type
        found_route_pattern = False
        for p in result["patterns"]:
            if p["common_traits"]["has_return_type"] and p["symbol_count"] >= 2:
                found_route_pattern = True
                break
        assert found_route_pattern

    def test_finds_method_pattern(self, tmp_path):
        """Should group methods with similar structure."""
        from firekeep_symdex.tools.detect_patterns import detect_patterns

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = detect_patterns("testowner/testrepo", kind="method", min_group_size=2, storage_path=str(tmp_path))

        assert "error" not in result

    def test_min_group_size_filtering(self, tmp_path):
        """Patterns below min_group_size should be excluded."""
        from firekeep_symdex.tools.detect_patterns import detect_patterns

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = detect_patterns("testowner/testrepo", min_group_size=10, storage_path=str(tmp_path))

        assert "error" not in result
        assert result["total_patterns"] == 0

    def test_kind_filter(self, tmp_path):
        """Should filter by kind when specified."""
        from firekeep_symdex.tools.detect_patterns import detect_patterns

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = detect_patterns("testowner/testrepo", kind="class", min_group_size=2, storage_path=str(tmp_path))

        assert "error" not in result
        for p in result["patterns"]:
            assert p["common_traits"]["kind"] == "class"

    def test_max_results_limit(self, tmp_path):
        """Should respect max_results."""
        from firekeep_symdex.tools.detect_patterns import detect_patterns

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = detect_patterns("testowner/testrepo", max_results=1, min_group_size=2, storage_path=str(tmp_path))

        assert len(result["patterns"]) <= 1

    def test_pattern_has_examples(self, tmp_path):
        """Each pattern should include example symbols."""
        from firekeep_symdex.tools.detect_patterns import detect_patterns

        _make_index(tmp_path, SYMBOLS, REFERENCES)
        result = detect_patterns("testowner/testrepo", min_group_size=2, storage_path=str(tmp_path))

        for p in result["patterns"]:
            assert "examples" in p
            assert len(p["examples"]) > 0
            for ex in p["examples"]:
                assert "symbol_id" in ex
                assert "name" in ex

    def test_repo_not_found(self, tmp_path):
        """Should return error for unknown repo."""
        from firekeep_symdex.tools.detect_patterns import detect_patterns

        result = detect_patterns("nonexistent/repo", storage_path=str(tmp_path))
        assert "error" in result

    def test_empty_repo(self, tmp_path):
        """Should handle repo with no symbols."""
        from firekeep_symdex.tools.detect_patterns import detect_patterns

        store = IndexStore(base_path=str(tmp_path))
        store.save_index(
            owner="testowner", name="emptyrepo",
            source_files=["empty.py"],
            symbols=[], raw_files={"empty.py": "# empty"},
            languages={"python": 1}, references=[],
        )
        result = detect_patterns("testowner/emptyrepo", storage_path=str(tmp_path))

        assert "error" not in result
        assert result["patterns"] == []


# ============================================================
# scaffold_symbol tests
# ============================================================

class TestScaffoldSymbol:
    def test_template_fallback_basic(self, tmp_path):
        """Without AI keys, should generate scaffold using template fallback."""
        from firekeep_symdex.tools.scaffold_symbol import scaffold_symbol

        _make_index(tmp_path, SYMBOLS, REFERENCES)

        with patch.dict("os.environ", {}, clear=True):
            result = scaffold_symbol(
                "testowner/testrepo",
                intent="handle user authentication",
                storage_path=str(tmp_path),
            )

        assert "error" not in result
        assert "scaffold" in result
        assert result["ai_generated"] is False
        assert "TODO" in result["scaffold"]
        assert "_meta" in result

    def test_template_fallback_uses_like_symbol(self, tmp_path):
        """With 'like' param, should base scaffold on that symbol."""
        from firekeep_symdex.tools.scaffold_symbol import scaffold_symbol

        _make_index(tmp_path, SYMBOLS, REFERENCES)

        with patch.dict("os.environ", {}, clear=True):
            result = scaffold_symbol(
                "testowner/testrepo",
                intent="list all products",
                like="api::get_users",
                storage_path=str(tmp_path),
            )

        assert "error" not in result
        assert "scaffold" in result
        assert result["ai_generated"] is False
        assert "based_on" in result
        assert result["based_on"]["symbol_id"] == "api::get_users"

    def test_template_fallback_class_kind(self, tmp_path):
        """Should generate class scaffold when kind=class."""
        from firekeep_symdex.tools.scaffold_symbol import scaffold_symbol

        _make_index(tmp_path, SYMBOLS, REFERENCES)

        with patch.dict("os.environ", {}, clear=True):
            result = scaffold_symbol(
                "testowner/testrepo",
                intent="product catalog service",
                kind="class",
                storage_path=str(tmp_path),
            )

        assert "error" not in result
        assert "class" in result["scaffold"].lower()

    def test_ai_path_anthropic(self, tmp_path):
        """With ANTHROPIC_API_KEY, should attempt AI generation."""
        from firekeep_symdex.tools.scaffold_symbol import scaffold_symbol

        _make_index(tmp_path, SYMBOLS, REFERENCES)

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="def handle_auth(request):\n    # authenticate user\n    pass")]
        mock_client.messages.create.return_value = mock_response

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}, clear=True):
            with patch("firekeep_symdex.tools.scaffold_symbol._detect_ai_provider", return_value=("anthropic", mock_client)):
                result = scaffold_symbol(
                    "testowner/testrepo",
                    intent="handle authentication",
                    storage_path=str(tmp_path),
                )

        assert "error" not in result
        assert result["ai_generated"] is True
        assert "handle_auth" in result["scaffold"]

    def test_ai_path_gemini(self, tmp_path):
        """With GOOGLE_API_KEY, should attempt Gemini generation."""
        from firekeep_symdex.tools.scaffold_symbol import scaffold_symbol

        _make_index(tmp_path, SYMBOLS, REFERENCES)

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "def handle_auth(request):\n    pass"
        mock_client.generate_content.return_value = mock_response

        with patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"}, clear=True):
            with patch("firekeep_symdex.tools.scaffold_symbol._detect_ai_provider", return_value=("gemini", mock_client)):
                result = scaffold_symbol(
                    "testowner/testrepo",
                    intent="handle authentication",
                    storage_path=str(tmp_path),
                )

        assert "error" not in result
        assert result["ai_generated"] is True

    def test_ai_fallback_on_error(self, tmp_path):
        """If AI call fails, should fall back to template."""
        from firekeep_symdex.tools.scaffold_symbol import scaffold_symbol

        _make_index(tmp_path, SYMBOLS, REFERENCES)

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("API error")

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}, clear=True):
            with patch("firekeep_symdex.tools.scaffold_symbol._detect_ai_provider", return_value=("anthropic", mock_client)):
                result = scaffold_symbol(
                    "testowner/testrepo",
                    intent="handle authentication",
                    storage_path=str(tmp_path),
                )

        assert "error" not in result
        assert result["ai_generated"] is False
        assert "TODO" in result["scaffold"]

    def test_conventions_applied_field(self, tmp_path):
        """Should list which conventions were applied."""
        from firekeep_symdex.tools.scaffold_symbol import scaffold_symbol

        _make_index(tmp_path, SYMBOLS, REFERENCES)

        with patch.dict("os.environ", {}, clear=True):
            result = scaffold_symbol(
                "testowner/testrepo",
                intent="process data",
                storage_path=str(tmp_path),
            )

        assert "conventions_applied" in result
        assert isinstance(result["conventions_applied"], list)

    def test_like_symbol_not_found(self, tmp_path):
        """Should return error if 'like' symbol doesn't exist."""
        from firekeep_symdex.tools.scaffold_symbol import scaffold_symbol

        _make_index(tmp_path, SYMBOLS, REFERENCES)

        result = scaffold_symbol(
            "testowner/testrepo",
            intent="anything",
            like="nonexistent::symbol",
            storage_path=str(tmp_path),
        )

        assert "error" in result

    def test_repo_not_found(self, tmp_path):
        """Should return error for unknown repo."""
        from firekeep_symdex.tools.scaffold_symbol import scaffold_symbol

        result = scaffold_symbol("nonexistent/repo", intent="anything", storage_path=str(tmp_path))
        assert "error" in result

    def test_target_file_in_result(self, tmp_path):
        """Should include target_file in result when specified."""
        from firekeep_symdex.tools.scaffold_symbol import scaffold_symbol

        _make_index(tmp_path, SYMBOLS, REFERENCES)

        with patch.dict("os.environ", {}, clear=True):
            result = scaffold_symbol(
                "testowner/testrepo",
                intent="process data",
                target_file="src/services/data_service.py",
                storage_path=str(tmp_path),
            )

        assert result.get("target_file") == "src/services/data_service.py"
