from corpus.config import CorpusSettings, get_corpus_settings


class TestCorpusSettings:
    def test_defaults(self):
        s = CorpusSettings(ENABLED=True)
        assert s.ENABLED is True
        assert s.CHUNK_SIZE == 1500
        assert s.CHUNK_OVERLAP == 200

    def test_env_prefix(self):
        """Settings load from CORPUS_ prefixed env vars."""
        import os

        os.environ["CORPUS_CHUNK_SIZE"] = "2000"
        try:
            s = CorpusSettings()
            assert s.CHUNK_SIZE == 2000
        finally:
            del os.environ["CORPUS_CHUNK_SIZE"]

    def test_singleton(self):
        s1 = get_corpus_settings()
        s2 = get_corpus_settings()
        assert s1 is s2
