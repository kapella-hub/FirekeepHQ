"""Backend selection — the one place that decides which platform module
backs `Backend` for this process.

Platform-module rule: the win/mac imports live INSIDE the branches below, so
merely importing this package never pulls in `uiautomation` or `Quartz`.
That keeps the package importable on Linux CI and lets `load_backend()`
itself be the only thing that can fail on an unsupported platform.
"""
from __future__ import annotations

import sys

from .base import Backend, UnsupportedBackend


def load_backend() -> Backend:
    if sys.platform == "win32":
        from .win import WinBackend
        return WinBackend()
    if sys.platform == "darwin":
        from .mac import MacBackend
        return MacBackend()
    return UnsupportedBackend()
