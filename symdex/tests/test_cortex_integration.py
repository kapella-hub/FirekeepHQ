"""Tests for FirekeepCortex integration tools."""
from unittest.mock import patch


class TestCortexClient:
    """Test CortexClient behavior."""

    def test_disabled_when_no_url(self):
        """Client is disabled when FIREKEEP_CORTEX_URL is not set."""
        with patch.dict("os.environ", {"FIREKEEP_CORTEX_URL": ""}):
            from firekeep_symdex.cortex.client import CortexClient
            client = CortexClient()
            assert not client.is_available

    def test_enabled_when_url_set(self):
        """Client is enabled when FIREKEEP_CORTEX_URL is set."""
        with patch.dict("os.environ", {"FIREKEEP_CORTEX_URL": "http://localhost:8000"}):
            from firekeep_symdex.cortex.client import CortexClient
            client = CortexClient()
            assert client.is_available

    async def test_learn_returns_disabled_when_no_url(self):
        """learn() returns disabled status when cortex not configured."""
        with patch.dict("os.environ", {"FIREKEEP_CORTEX_URL": ""}):
            from firekeep_symdex.cortex.client import CortexClient
            client = CortexClient()
            result = await client.learn("test action", "test outcome")
            assert result.get("status") == "disabled"

    async def test_recall_returns_disabled_when_no_url(self):
        """recall() returns disabled status when cortex not configured."""
        with patch.dict("os.environ", {"FIREKEEP_CORTEX_URL": ""}):
            from firekeep_symdex.cortex.client import CortexClient
            client = CortexClient()
            result = await client.recall("test task")
            assert result.get("status") == "disabled"

    async def test_stream_returns_disabled_when_no_url(self):
        """stream() returns disabled status when cortex not configured."""
        with patch.dict("os.environ", {"FIREKEEP_CORTEX_URL": ""}):
            from firekeep_symdex.cortex.client import CortexClient
            client = CortexClient()
            result = await client.stream("test-source", {"key": "value"})
            assert result.get("status") == "disabled"


class TestGetCortexClient:
    """Test the shared singleton factory."""

    def test_returns_same_instance(self):
        """get_cortex_client returns the same instance on repeated calls."""
        import firekeep_symdex.cortex.client as mod
        # Reset singleton for test isolation
        mod._shared_client = None
        try:
            a = mod.get_cortex_client()
            b = mod.get_cortex_client()
            assert a is b
        finally:
            mod._shared_client = None

    def test_reads_env_at_call_time(self):
        """Client picks up FIREKEEP_CORTEX_URL set after import."""
        import firekeep_symdex.cortex.client as mod
        mod._shared_client = None
        try:
            with patch.dict("os.environ", {"FIREKEEP_CORTEX_URL": "http://localhost:9999"}):
                client = mod.get_cortex_client()
                assert client.is_available
                assert "9999" in client._base_url
        finally:
            mod._shared_client = None


class TestLearnFromChanges:
    """Test learn_from_changes tool."""

    async def test_no_changes_returns_early(self, tmp_path):
        """When repo not indexed, returns an error."""
        from firekeep_symdex.tools.learn_from_changes import learn_from_changes
        result = await learn_from_changes("nonexistent/repo", storage_path=str(tmp_path))
        assert "error" in result or "no_changes" in result.get("status", "")


class TestRecallWithCode:
    """Test recall_with_code tool."""

    async def test_works_without_cortex(self, tmp_path):
        """Falls back to code-only context when cortex unavailable."""
        from firekeep_symdex.parser import parse_file
        from firekeep_symdex.storage import IndexStore

        content = 'def authenticate(user, password):\n    return True\n'
        symbols = parse_file(content, "auth.py", "python")
        store = IndexStore(base_path=str(tmp_path))
        store.save_index(
            owner="test", name="test-repo",
            source_files=["auth.py"],
            symbols=symbols,
            raw_files={"auth.py": content},
            languages={"python": 1},
            references=[],
        )

        from firekeep_symdex.tools.recall_with_code import recall_with_code
        import firekeep_symdex.cortex.client as cortex_mod
        original = cortex_mod._shared_client
        cortex_mod._shared_client = None
        try:
            with patch.dict("os.environ", {"FIREKEEP_CORTEX_URL": ""}):
                cortex_mod._shared_client = None
                result = await recall_with_code(
                    task="fix authentication",
                    repo="test/test-repo",
                    storage_path=str(tmp_path),
                )
        finally:
            cortex_mod._shared_client = original

        assert "error" not in result
        assert "code_context" in result
        assert result["_meta"]["cortex_available"] is False


class TestReviewWithHistory:
    """Test review_with_history tool."""

    async def test_works_without_cortex(self, tmp_path):
        """Review works even when cortex unavailable (just no history)."""
        from firekeep_symdex.parser import parse_file
        from firekeep_symdex.storage import IndexStore

        content = 'def handler(req):\n    return "ok"\n'
        symbols = parse_file(content, "app.py", "python")
        store = IndexStore(base_path=str(tmp_path))
        store.save_index(
            owner="test", name="test-repo",
            source_files=["app.py"],
            symbols=symbols,
            raw_files={"app.py": content},
            languages={"python": 1},
            references=[],
        )

        from firekeep_symdex.tools.review_with_history import review_with_history
        import firekeep_symdex.cortex.client as cortex_mod
        original = cortex_mod._shared_client
        cortex_mod._shared_client = None
        try:
            with patch.dict("os.environ", {"FIREKEEP_CORTEX_URL": ""}):
                cortex_mod._shared_client = None
                result = await review_with_history(
                    repo="test/test-repo",
                    changed_files=["app.py"],
                    storage_path=str(tmp_path),
                )
        finally:
            cortex_mod._shared_client = original

        assert "error" not in result
        assert "review" in result
        assert result["_meta"]["cortex_available"] is False


class TestInternalKeyInjection:
    """Symdex->Cortex calls carry X-API-Key from FIREKEEP_INTERNAL_KEY (SP1b §11)."""

    async def test_injects_internal_key_header(self, monkeypatch):
        monkeypatch.setenv("FIREKEEP_CORTEX_URL", "http://cortex:8000")
        monkeypatch.setenv("FIREKEEP_INTERNAL_KEY", "nxs_secret")
        from firekeep_symdex.cortex.client import CortexClient
        client = CortexClient()
        http = client._get_client()
        # httpx.Headers lookup is case-insensitive
        assert http.headers.get("X-API-Key") == "nxs_secret"
        await client.close()

    async def test_no_key_header_when_unset(self, monkeypatch):
        monkeypatch.setenv("FIREKEEP_CORTEX_URL", "http://cortex:8000")
        monkeypatch.delenv("FIREKEEP_INTERNAL_KEY", raising=False)
        from firekeep_symdex.cortex.client import CortexClient
        client = CortexClient()
        http = client._get_client()
        assert "x-api-key" not in http.headers  # case-insensitive membership
        await client.close()

    async def test_explicit_internal_key_overrides_env(self, monkeypatch):
        monkeypatch.setenv("FIREKEEP_CORTEX_URL", "http://cortex:8000")
        monkeypatch.setenv("FIREKEEP_INTERNAL_KEY", "nxs_env")
        from firekeep_symdex.cortex.client import CortexClient
        client = CortexClient(internal_key="nxs_explicit")
        http = client._get_client()
        assert http.headers.get("X-API-Key") == "nxs_explicit"
        await client.close()
