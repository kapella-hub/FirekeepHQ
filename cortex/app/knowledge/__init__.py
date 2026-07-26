"""Knowledge — docs-to-skills ingestion pipeline for Firekeep.

Classifies ingested documents (reference vs procedural vs mixed) and
extracts per-procedure titles for skill drafting. Layered over the
existing corpus (full-text search) and skills (draft/active) subsystems;
does not modify either.
"""
