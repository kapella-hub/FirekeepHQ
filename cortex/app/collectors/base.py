"""SourceAdapter protocol for scheduled collectors (SP3)."""
from __future__ import annotations
from typing import Awaitable, Callable, Protocol, TypedDict, runtime_checkable


class SourceItem(TypedDict):
    stable_id: str
    version: int
    label: str
    meta: dict


@runtime_checkable
class SourceAdapter(Protocol):
    name: str
    async def discover_changed(self, seen: Callable[[str], "Awaitable[int]"]) -> list[SourceItem]: ...
    async def fetch_content(self, item: SourceItem) -> tuple[str, str, str]: ...
    async def aclose(self) -> None: ...
