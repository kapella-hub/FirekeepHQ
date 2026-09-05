"""Firekeep Hands — a screen-aware operator for the whole computer, behind a
local approval broker. This wheel imports firekeep_client (resolver, state,
hooklog) from the Client Kit's own venv at runtime and is installed alongside
it, never standalone (see README.md)."""
from __future__ import annotations

__version__ = "0.1.0"

# Tags every synthetic input event Hands generates so the broker's real-input
# guard can tell "Hands typed this" from "a human (or malware) typed this" —
# dwExtraInfo on Windows SendInput, kCGEventSourceUserData on macOS CGEvent.
# Spelled out: 0x46494B48 == b"FIKH" read as a big-endian uint32 (Firekeep
# Input Kit Hands) — arbitrary but distinctive, chosen to be vanishingly
# unlikely to collide with another process's tag.
HANDS_TAG = 0x46494B48
