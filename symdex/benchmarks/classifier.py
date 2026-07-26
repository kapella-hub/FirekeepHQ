"""Question classifier for adaptive context strategy selection."""

import re

# Intent patterns
LOCATION_PATTERNS = [
    r"\bwhere\b", r"\bfind\b", r"\blocated\b", r"\bdefined\b",
    r"\bimplemented\b", r"\bwhich file\b", r"\bwhich class\b",
]
MECHANISM_PATTERNS = [
    r"\bhow does\b", r"\bhow is\b", r"\bworks?\b", r"\bprocess\b",
    r"\bhandle[sd]?\b", r"\bdetermine[sd]?\b", r"\bmanage[sd]?\b",
    r"\bconvert[sd]?\b", r"\bresolve[sd]?\b",
]
CHANGE_PATTERNS = [
    r"\bmodify\b", r"\badd\b", r"\bchange\b", r"\bwould you\b",
    r"\bhow would\b", r"\bsupport\b", r"\bimplement\b",
]


def classify_question(question: dict) -> str:
    """Classify a question's intent.

    Returns one of: 'location', 'mechanism', 'change'

    Also considers the 'category' field as a hint, but primarily
    uses text analysis of the question.
    """
    text = question.get("question", "").lower()
    category = question.get("category", "")

    # Score each intent
    scores = {
        "location": sum(1 for p in LOCATION_PATTERNS if re.search(p, text)),
        "mechanism": sum(1 for p in MECHANISM_PATTERNS if re.search(p, text)),
        "change": sum(1 for p in CHANGE_PATTERNS if re.search(p, text)),
    }

    # Category hints (add 0.5 to bias toward the expected intent)
    if category == "navigation":
        scores["location"] += 0.5
    elif category == "comprehension":
        scores["mechanism"] += 0.5
    elif category == "modification":
        scores["change"] += 0.5

    # Return highest scoring intent
    return max(scores, key=scores.get)


def classify_file_strategy(raw_tokens: int) -> str:
    """Select context strategy based on raw file token count.

    Returns one of: 'precise', 'enriched', 'surgical'
    """
    if raw_tokens < 8000:
        return "precise"
    elif raw_tokens < 20000:
        return "enriched"
    else:
        return "surgical"
