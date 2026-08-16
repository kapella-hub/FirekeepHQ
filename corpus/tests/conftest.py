"""Shared test fixtures for corpus module."""

from __future__ import annotations

import pytest


class RecordingRawClientFake:
    """The raw-Qdrant-client slice ``commit_generation`` reaches
    (``vector_client._client.set_payload`` — the workspace_migration
    transport), recorded verbatim like the upserts."""

    def __init__(self) -> None:
        self.set_payloads: list[dict] = []

    async def set_payload(self, **kwargs) -> None:
        self.set_payloads.append(kwargs)


class RecordingVectorFake:
    """VectorClient stand-in that records upsert kwargs verbatim.

    Phase V tests pin the exact upsert wire kwargs (point_id, metadata);
    a plain list of kwarg dicts keeps those assertions readable where an
    AsyncMock call_args chain would not. Carries the ``_client`` /
    ``_collection`` attributes of the real VectorClient so the pipeline's
    commit path runs against it instead of skipping through
    ``commit_generation``'s fail-closed guard.
    """

    def __init__(self) -> None:
        self.upserts: list[dict] = []
        self._collection = "firekeep_memory"
        self._client = RecordingRawClientFake()

    async def upsert(self, **kwargs) -> str:
        self.upserts.append(kwargs)
        return kwargs.get("point_id") or "point-id"


@pytest.fixture
def fake_vector():
    return RecordingVectorFake()
