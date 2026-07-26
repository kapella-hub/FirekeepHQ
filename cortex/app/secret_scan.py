"""Secret detection for memory ingest — prevents accidental storage of secrets.

Scans text for common secret patterns (API keys, tokens, passwords, private keys)
using regex and Shannon entropy analysis.

Config:
    SECRET_SCAN_ENABLED=true (default)
    SECRET_SCAN_MODE=warn|block (default: warn)

In "warn" mode, secrets are flagged but the memory is still stored.
In "block" mode, the request is rejected if secrets are found.
"""

from __future__ import annotations

import math
import re
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Secret patterns (regex)
# ---------------------------------------------------------------------------

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("AWS Secret Key", re.compile(r"(?i)aws[_\-]?secret[_\-]?(?:access[_\-]?)?key\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})")),
    ("GitHub Token", re.compile(r"gh[ps]_[A-Za-z0-9_]{36,}")),
    ("GitHub OAuth", re.compile(r"gho_[A-Za-z0-9_]{36,}")),
    ("Anthropic API Key", re.compile(r"sk-ant-[A-Za-z0-9_-]{40,}")),
    ("OpenAI API Key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("Slack Token", re.compile(r"xox[bpors]-[A-Za-z0-9-]{10,}")),
    ("Private Key Block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("Generic API Key", re.compile(r"(?i)(?:api[_\-]?key|apikey|secret[_\-]?key|auth[_\-]?token)\s*[:=]\s*['\"]?([A-Za-z0-9_\-/.+=]{20,})")),
    ("Password Assignment", re.compile(r"(?i)(?:password|passwd|pwd)\s*[:=]\s*['\"]([^'\"]{8,})['\"]")),
    ("Bearer Token", re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-/.+=]{20,}")),
    ("Connection String", re.compile(r"(?i)(?:mongodb|postgres|mysql|redis|amqp)://[^\s'\"]{10,}")),
]

# Minimum entropy threshold for high-entropy string detection
_ENTROPY_THRESHOLD = 4.5
_ENTROPY_MIN_LENGTH = 20


# ---------------------------------------------------------------------------
# Entropy calculation
# ---------------------------------------------------------------------------


def _shannon_entropy(s: str) -> float:
    """Calculate Shannon entropy of a string."""
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    length = len(s)
    return -sum((count / length) * math.log2(count / length) for count in freq.values())


def _find_high_entropy_strings(text: str) -> list[dict[str, Any]]:
    """Find high-entropy strings that might be secrets."""
    findings = []
    # Split on whitespace and common delimiters
    tokens = re.split(r'[\s,;:=\'"()\[\]{}]+', text)
    for token in tokens:
        if len(token) < _ENTROPY_MIN_LENGTH:
            continue
        # Skip common non-secret patterns
        if token.startswith("http") or token.startswith("/") or token.startswith("./"):
            continue
        entropy = _shannon_entropy(token)
        if entropy >= _ENTROPY_THRESHOLD:
            findings.append({
                "type": "High Entropy String",
                "match": token[:30] + "..." if len(token) > 30 else token,
                "entropy": round(entropy, 2),
            })
    return findings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scan_text(text: str) -> list[dict[str, Any]]:
    """Scan text for potential secrets.

    Returns a list of findings: [{type: str, match: str, ...}]
    Empty list means no secrets detected.
    """
    if not text:
        return []

    findings = []

    # Pattern matching
    for name, pattern in _PATTERNS:
        matches = pattern.findall(text)
        for match in matches:
            # Redact most of the match
            if isinstance(match, str) and len(match) > 8:
                redacted = match[:4] + "****" + match[-4:]
            else:
                redacted = "****"
            findings.append({
                "type": name,
                "match": redacted,
            })

    # Entropy analysis
    entropy_findings = _find_high_entropy_strings(text)
    findings.extend(entropy_findings)

    return findings


def scan_action_log(action: str, outcome: str, resolution: str | None = None) -> list[dict[str, Any]]:
    """Scan an ActionLog's text fields for secrets."""
    all_text = f"{action}\n{outcome}"
    if resolution:
        all_text += f"\n{resolution}"
    return scan_text(all_text)
