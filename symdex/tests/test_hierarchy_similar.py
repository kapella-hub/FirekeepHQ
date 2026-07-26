"""Tests for get_type_hierarchy and get_similar_symbols tools."""

from firekeep_symdex.parser import parse_file
from firekeep_symdex.storage import IndexStore

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

HIERARCHY_PYTHON = {
    "models.py": '''\
class Animal:
    def speak(self):
        pass

class Dog(Animal):
    def speak(self):
        return "woof"

class Cat(Animal):
    def speak(self):
        return "meow"

class GuideDog(Dog):
    def guide(self):
        pass
''',
    "mixins.py": '''\
class Serializable:
    def to_json(self):
        pass

class Persistable:
    def save(self):
        pass

class Model(Serializable, Persistable):
    def validate(self):
        pass

class User(Model):
    def login(self):
        pass
''',
}


SIMILAR_PYTHON = {
    "services.py": '''\
class UserService:
    def get_user(self, user_id: int) -> dict:
        return {}

    def get_account(self, account_id: int) -> dict:
        return {}

    def delete_user(self, user_id: int) -> bool:
        return True

    def create_user(self, name: str, email: str) -> dict:
        return {}

    def list_users(self) -> list:
        return []
''',
    "utils.py": '''\
def fetch_record(record_id: int) -> dict:
    return {}

def process_data(data: list, limit: int) -> list:
    return []

def compute_hash(value: str) -> str:
    return ""
''',
}


def _build_index(files_dict, storage_path, lang="python"):
    """Parse files and save an index for testing."""
    all_symbols = []
    raw_files = {}
    for path, content in files_dict.items():
        symbols = parse_file(content, path, lang)
        all_symbols.extend(symbols)
        raw_files[path] = content

    store = IndexStore(base_path=storage_path)
    store.save_index(
        owner="test",
        name="test-repo",
        source_files=list(files_dict.keys()),
        symbols=all_symbols,
        raw_files=raw_files,
        languages={lang: len(files_dict)},
        references=[],
    )
    return store


def _find_symbol_id(store, name, kind=None):
    """Find a symbol ID by name (and optionally kind) from the test index."""
    index = store.load_index("test", "test-repo")
    for sym in index.symbols:
        if sym["name"] == name:
            if kind is None or sym["kind"] == kind:
                return sym["id"]
    raise ValueError(f"Symbol {name!r} (kind={kind}) not found in index")


# ---------------------------------------------------------------------------
# get_type_hierarchy tests
# ---------------------------------------------------------------------------

class TestGetTypeHierarchy:
    """Tests for get_type_hierarchy tool."""

    def test_single_parent(self, tmp_path):
        """Dog inherits from Animal, which has no parents."""
        from firekeep_symdex.tools.get_type_hierarchy import get_type_hierarchy

        store = _build_index(HIERARCHY_PYTHON, str(tmp_path))
        dog_id = _find_symbol_id(store, "Dog", kind="class")

        result = get_type_hierarchy("test/test-repo", dog_id, storage_path=str(tmp_path))

        assert "error" not in result
        assert result["name"] == "Dog"
        assert len(result["parents"]) == 1
        assert result["parents"][0]["name"] == "Animal"
        assert result["parents"][0]["symbol_id"] is not None

    def test_multiple_parents(self, tmp_path):
        """Model inherits from Serializable and Persistable."""
        from firekeep_symdex.tools.get_type_hierarchy import get_type_hierarchy

        store = _build_index(HIERARCHY_PYTHON, str(tmp_path))
        model_id = _find_symbol_id(store, "Model", kind="class")

        result = get_type_hierarchy("test/test-repo", model_id, storage_path=str(tmp_path))

        assert "error" not in result
        parent_names = [p["name"] for p in result["parents"]]
        assert "Serializable" in parent_names
        assert "Persistable" in parent_names

    def test_finds_children(self, tmp_path):
        """Animal should list Dog and Cat as children."""
        from firekeep_symdex.tools.get_type_hierarchy import get_type_hierarchy

        store = _build_index(HIERARCHY_PYTHON, str(tmp_path))
        animal_id = _find_symbol_id(store, "Animal", kind="class")

        result = get_type_hierarchy("test/test-repo", animal_id, storage_path=str(tmp_path))

        assert "error" not in result
        child_names = {c["name"] for c in result["children"]}
        assert "Dog" in child_names
        assert "Cat" in child_names
        # GuideDog inherits Dog, not Animal directly
        assert "GuideDog" not in child_names

    def test_no_parents_for_root_class(self, tmp_path):
        """Animal has no parents in this codebase."""
        from firekeep_symdex.tools.get_type_hierarchy import get_type_hierarchy

        store = _build_index(HIERARCHY_PYTHON, str(tmp_path))
        animal_id = _find_symbol_id(store, "Animal", kind="class")

        result = get_type_hierarchy("test/test-repo", animal_id, storage_path=str(tmp_path))

        assert "error" not in result
        assert result["parents"] == []

    def test_leaf_class_no_children(self, tmp_path):
        """GuideDog has no subclasses."""
        from firekeep_symdex.tools.get_type_hierarchy import get_type_hierarchy

        store = _build_index(HIERARCHY_PYTHON, str(tmp_path))
        guide_id = _find_symbol_id(store, "GuideDog", kind="class")

        result = get_type_hierarchy("test/test-repo", guide_id, storage_path=str(tmp_path))

        assert "error" not in result
        assert result["children"] == []
        parent_names = [p["name"] for p in result["parents"]]
        assert "Dog" in parent_names

    def test_rejects_non_class_symbol(self, tmp_path):
        """Should error if symbol is not a class or type."""
        from firekeep_symdex.tools.get_type_hierarchy import get_type_hierarchy

        store = _build_index(HIERARCHY_PYTHON, str(tmp_path))
        # "speak" is a method, not a class
        speak_id = _find_symbol_id(store, "speak", kind="method")

        result = get_type_hierarchy("test/test-repo", speak_id, storage_path=str(tmp_path))

        assert "error" in result
        assert "not a class or type" in result["error"]

    def test_symbol_not_found(self, tmp_path):
        """Error for nonexistent symbol ID."""
        from firekeep_symdex.tools.get_type_hierarchy import get_type_hierarchy

        _build_index(HIERARCHY_PYTHON, str(tmp_path))

        result = get_type_hierarchy(
            "test/test-repo", "nonexistent::id#class", storage_path=str(tmp_path)
        )

        assert "error" in result
        assert "not found" in result["error"]

    def test_repo_not_indexed(self, tmp_path):
        """Error for nonexistent repo."""
        from firekeep_symdex.tools.get_type_hierarchy import get_type_hierarchy

        result = get_type_hierarchy(
            "test/no-repo", "some::id#class", storage_path=str(tmp_path)
        )

        assert "error" in result

    def test_metadata_present(self, tmp_path):
        """Result should include _meta with timing_ms."""
        from firekeep_symdex.tools.get_type_hierarchy import get_type_hierarchy

        store = _build_index(HIERARCHY_PYTHON, str(tmp_path))
        animal_id = _find_symbol_id(store, "Animal", kind="class")

        result = get_type_hierarchy("test/test-repo", animal_id, storage_path=str(tmp_path))

        assert "_meta" in result
        assert "timing_ms" in result["_meta"]
        assert isinstance(result["_meta"]["timing_ms"], float)

    def test_transitive_chain(self, tmp_path):
        """GuideDog -> Dog -> Animal chain is navigable."""
        from firekeep_symdex.tools.get_type_hierarchy import get_type_hierarchy

        store = _build_index(HIERARCHY_PYTHON, str(tmp_path))

        guide_id = _find_symbol_id(store, "GuideDog", kind="class")
        result = get_type_hierarchy("test/test-repo", guide_id, storage_path=str(tmp_path))

        assert result["parents"][0]["name"] == "Dog"
        dog_parent_id = result["parents"][0]["symbol_id"]

        # Follow the chain up
        result2 = get_type_hierarchy("test/test-repo", dog_parent_id, storage_path=str(tmp_path))
        assert result2["parents"][0]["name"] == "Animal"


# ---------------------------------------------------------------------------
# get_type_hierarchy internal helpers tests
# ---------------------------------------------------------------------------

class TestParseBaseClasses:
    """Tests for _parse_base_classes helper."""

    def test_single_base(self):
        from firekeep_symdex.tools.get_type_hierarchy import _parse_base_classes
        assert _parse_base_classes("class Dog(Animal):") == ["Animal"]

    def test_multiple_bases(self):
        from firekeep_symdex.tools.get_type_hierarchy import _parse_base_classes
        assert _parse_base_classes("class Model(Serializable, Persistable):") == [
            "Serializable", "Persistable"
        ]

    def test_no_bases(self):
        from firekeep_symdex.tools.get_type_hierarchy import _parse_base_classes
        assert _parse_base_classes("class Foo:\n    pass") == []

    def test_dotted_base(self):
        from firekeep_symdex.tools.get_type_hierarchy import _parse_base_classes
        result = _parse_base_classes("class Foo(module.Bar):")
        assert result == ["Bar"]

    def test_generic_base(self):
        from firekeep_symdex.tools.get_type_hierarchy import _parse_base_classes
        result = _parse_base_classes("class Foo(Generic[T], Base):")
        assert result == ["Generic", "Base"]


# ---------------------------------------------------------------------------
# get_similar_symbols tests
# ---------------------------------------------------------------------------

class TestGetSimilarSymbols:
    """Tests for get_similar_symbols tool."""

    def test_finds_similar_by_params(self, tmp_path):
        """get_user and get_account have similar signatures."""
        from firekeep_symdex.tools.get_similar_symbols import get_similar_symbols

        store = _build_index(SIMILAR_PYTHON, str(tmp_path))
        get_user_id = _find_symbol_id(store, "get_user", kind="method")

        result = get_similar_symbols(
            "test/test-repo", get_user_id, storage_path=str(tmp_path)
        )

        assert "error" not in result
        assert result["similar_count"] > 0
        names = [s["name"] for s in result["similar_symbols"]]
        # get_account has same structure: (self, xxx_id: int) -> dict
        assert "get_account" in names

    def test_excludes_self(self, tmp_path):
        """The input symbol should not appear in results."""
        from firekeep_symdex.tools.get_similar_symbols import get_similar_symbols

        store = _build_index(SIMILAR_PYTHON, str(tmp_path))
        get_user_id = _find_symbol_id(store, "get_user", kind="method")

        result = get_similar_symbols(
            "test/test-repo", get_user_id, storage_path=str(tmp_path)
        )

        result_ids = {s["symbol_id"] for s in result["similar_symbols"]}
        assert get_user_id not in result_ids

    def test_similarity_score_ranking(self, tmp_path):
        """More similar symbols should have higher scores."""
        from firekeep_symdex.tools.get_similar_symbols import get_similar_symbols

        store = _build_index(SIMILAR_PYTHON, str(tmp_path))
        get_user_id = _find_symbol_id(store, "get_user", kind="method")

        result = get_similar_symbols(
            "test/test-repo", get_user_id, storage_path=str(tmp_path)
        )

        scores = [s["similarity_score"] for s in result["similar_symbols"]]
        # Scores should be in descending order
        assert scores == sorted(scores, reverse=True)

    def test_match_reasons_present(self, tmp_path):
        """Each result should include match_reasons list."""
        from firekeep_symdex.tools.get_similar_symbols import get_similar_symbols

        store = _build_index(SIMILAR_PYTHON, str(tmp_path))
        get_user_id = _find_symbol_id(store, "get_user", kind="method")

        result = get_similar_symbols(
            "test/test-repo", get_user_id, storage_path=str(tmp_path)
        )

        for sym in result["similar_symbols"]:
            assert "match_reasons" in sym
            assert isinstance(sym["match_reasons"], list)
            assert len(sym["match_reasons"]) > 0

    def test_max_results_limit(self, tmp_path):
        """Should respect max_results parameter."""
        from firekeep_symdex.tools.get_similar_symbols import get_similar_symbols

        store = _build_index(SIMILAR_PYTHON, str(tmp_path))
        get_user_id = _find_symbol_id(store, "get_user", kind="method")

        result = get_similar_symbols(
            "test/test-repo", get_user_id, max_results=2, storage_path=str(tmp_path)
        )

        assert len(result["similar_symbols"]) <= 2

    def test_kind_filter(self, tmp_path):
        """Results should only include symbols of the same kind."""
        from firekeep_symdex.tools.get_similar_symbols import get_similar_symbols

        store = _build_index(SIMILAR_PYTHON, str(tmp_path))
        get_user_id = _find_symbol_id(store, "get_user", kind="method")

        result = get_similar_symbols(
            "test/test-repo", get_user_id, storage_path=str(tmp_path)
        )

        for sym in result["similar_symbols"]:
            assert sym["kind"] == "method"

    def test_symbol_not_found(self, tmp_path):
        """Error for nonexistent symbol ID."""
        from firekeep_symdex.tools.get_similar_symbols import get_similar_symbols

        _build_index(SIMILAR_PYTHON, str(tmp_path))

        result = get_similar_symbols(
            "test/test-repo", "nonexistent::id#function", storage_path=str(tmp_path)
        )

        assert "error" in result

    def test_repo_not_indexed(self, tmp_path):
        """Error for nonexistent repo."""
        from firekeep_symdex.tools.get_similar_symbols import get_similar_symbols

        result = get_similar_symbols(
            "test/no-repo", "some::id#function", storage_path=str(tmp_path)
        )

        assert "error" in result

    def test_metadata_present(self, tmp_path):
        """Result should include _meta with timing_ms."""
        from firekeep_symdex.tools.get_similar_symbols import get_similar_symbols

        store = _build_index(SIMILAR_PYTHON, str(tmp_path))
        get_user_id = _find_symbol_id(store, "get_user", kind="method")

        result = get_similar_symbols(
            "test/test-repo", get_user_id, storage_path=str(tmp_path)
        )

        assert "_meta" in result
        assert "timing_ms" in result["_meta"]

    def test_same_directory_bonus(self, tmp_path):
        """Symbols in the same directory should get a bonus."""
        from firekeep_symdex.tools.get_similar_symbols import get_similar_symbols

        # Build index with files in different dirs
        files = {
            "api/handlers.py": '''\
def get_item(item_id: int) -> dict:
    return {}

def get_order(order_id: int) -> dict:
    return {}
''',
            "lib/helpers.py": '''\
def get_record(record_id: int) -> dict:
    return {}
''',
        }
        store = _build_index(files, str(tmp_path))
        get_item_id = _find_symbol_id(store, "get_item", kind="function")

        result = get_similar_symbols(
            "test/test-repo", get_item_id, storage_path=str(tmp_path)
        )

        # get_order (same dir) should rank higher than get_record (different dir)
        names = [s["name"] for s in result["similar_symbols"]]
        if "get_order" in names and "get_record" in names:
            assert names.index("get_order") < names.index("get_record")

    def test_result_fields(self, tmp_path):
        """Each result should contain all expected fields."""
        from firekeep_symdex.tools.get_similar_symbols import get_similar_symbols

        store = _build_index(SIMILAR_PYTHON, str(tmp_path))
        get_user_id = _find_symbol_id(store, "get_user", kind="method")

        result = get_similar_symbols(
            "test/test-repo", get_user_id, storage_path=str(tmp_path)
        )

        for sym in result["similar_symbols"]:
            assert "symbol_id" in sym
            assert "name" in sym
            assert "qualified_name" in sym
            assert "kind" in sym
            assert "file" in sym
            assert "line" in sym
            assert "signature" in sym
            assert "similarity_score" in sym
            assert "match_reasons" in sym


# ---------------------------------------------------------------------------
# get_similar_symbols internal helpers tests
# ---------------------------------------------------------------------------

class TestTokenizeName:
    """Tests for _tokenize_name helper."""

    def test_snake_case(self):
        from firekeep_symdex.tools.get_similar_symbols import _tokenize_name
        assert _tokenize_name("get_user_by_id") == {"get", "user", "by", "id"}

    def test_camel_case(self):
        from firekeep_symdex.tools.get_similar_symbols import _tokenize_name
        assert _tokenize_name("getUserById") == {"get", "user", "by", "id"}

    def test_pascal_case(self):
        from firekeep_symdex.tools.get_similar_symbols import _tokenize_name
        assert _tokenize_name("UserService") == {"user", "service"}

    def test_upper_case(self):
        from firekeep_symdex.tools.get_similar_symbols import _tokenize_name
        assert _tokenize_name("MAX_RETRY_COUNT") == {"max", "retry", "count"}


class TestExtractParams:
    """Tests for _extract_params helper."""

    def test_python_params(self):
        from firekeep_symdex.tools.get_similar_symbols import _extract_params
        result = _extract_params("def get_user(self, user_id: int) -> dict")
        assert result == ["user_id"]

    def test_no_params(self):
        from firekeep_symdex.tools.get_similar_symbols import _extract_params
        result = _extract_params("def foo()")
        assert result == []

    def test_multiple_params(self):
        from firekeep_symdex.tools.get_similar_symbols import _extract_params
        result = _extract_params("def create(name: str, email: str, age: int)")
        assert result == ["name", "email", "age"]

    def test_no_parens(self):
        from firekeep_symdex.tools.get_similar_symbols import _extract_params
        result = _extract_params("class Foo")
        assert result == []
