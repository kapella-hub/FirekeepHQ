from corpus.chunker import chunk_content


class TestParagraphChunking:
    """Default text chunking splits on paragraph boundaries."""

    def test_short_text_single_chunk(self):
        text = "This is a short paragraph."
        chunks = chunk_content(text, source_type="text", chunk_size=1500)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_splits_on_double_newline(self):
        text = "Paragraph one about billing.\n\nParagraph two about provisioning."
        chunks = chunk_content(text, source_type="text", chunk_size=40)
        assert len(chunks) == 2
        assert "billing" in chunks[0]
        assert "provisioning" in chunks[1]

    def test_merges_small_paragraphs(self):
        text = "A.\n\nB.\n\nC."
        chunks = chunk_content(text, source_type="text", chunk_size=1500)
        assert len(chunks) == 1  # All fit in one chunk

    def test_respects_chunk_size(self):
        para = "Word " * 100  # ~500 chars
        text = f"{para}\n\n{para}\n\n{para}\n\n{para}"
        chunks = chunk_content(text, source_type="text", chunk_size=600)
        assert all(len(c) <= 700 for c in chunks)  # Allow some overflow for paragraph boundaries


class TestWikiChunking:
    """Wiki chunking splits on markdown headers."""

    def test_splits_on_h2(self):
        text = "## Overview\nSome overview text.\n\n## Architecture\nArch details."
        chunks = chunk_content(text, source_type="wiki", chunk_size=1500)
        assert len(chunks) == 2
        assert "## Overview" in chunks[0]
        assert "## Architecture" in chunks[1]

    def test_preserves_header_in_chunk(self):
        text = "## Billing System\nCSG handles all billing operations."
        chunks = chunk_content(text, source_type="wiki", chunk_size=1500)
        assert chunks[0].startswith("## Billing System")

    def test_long_section_splits_further(self):
        long_section = "## Big Section\n" + ("Detail. " * 200)
        chunks = chunk_content(long_section, source_type="wiki", chunk_size=500)
        assert len(chunks) > 1
        # First chunk keeps the header
        assert "## Big Section" in chunks[0]


class TestJiraChunking:
    """Jira chunking keeps the whole issue as one chunk if possible."""

    def test_short_issue_single_chunk(self):
        text = "PROJ-123: Fix billing sync\n\nThe billing sync fails when..."
        chunks = chunk_content(text, source_type="jira", chunk_size=1500)
        assert len(chunks) == 1

    def test_long_issue_splits(self):
        text = "PROJ-456: Big epic\n\n" + ("Comment text. " * 200)
        chunks = chunk_content(text, source_type="jira", chunk_size=500)
        assert len(chunks) > 1


class TestApiDocChunking:
    """API doc chunking splits on endpoint boundaries."""

    def test_splits_on_endpoints(self):
        text = (
            "### GET /api/customers\nReturns customer list.\n\n"
            "### POST /api/orders\nCreates a new order."
        )
        chunks = chunk_content(text, source_type="api-doc", chunk_size=1500)
        assert len(chunks) == 2
        assert "/api/customers" in chunks[0]
        assert "/api/orders" in chunks[1]


class TestEmptyInput:
    def test_empty_string(self):
        assert chunk_content("", source_type="text") == []

    def test_whitespace_only(self):
        assert chunk_content("   \n\n  ", source_type="text") == []
