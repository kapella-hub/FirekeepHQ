"""FirekeepCortex exception hierarchy.

Maps to HTTP error responses via FastAPI exception handlers.
Never leak stack traces to clients.
"""


class FirekeepCortexError(Exception):
    """Base exception for all FirekeepCortex errors."""


class GraphConnectionError(FirekeepCortexError):
    """Neo4j connection or query failure."""


class VectorStoreError(FirekeepCortexError):
    """Qdrant operation failure."""


class LLMExtractionError(FirekeepCortexError):
    """LLM call or JSON parse failure."""


class StreamIngestionError(FirekeepCortexError):
    """Redis push failure."""


class ConfigurationError(FirekeepCortexError):
    """Raised when configuration is invalid or missing."""
