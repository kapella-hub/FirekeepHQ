"""Every dex scope the corpus reserved-prefix table demands must be MINTABLE.

`corpus.api`'s prefix table derives `dex:<id>` from `corpus.store.KNOWN_DEX_IDS`,
and `auth.keys.create_key` rejects any scope absent from `auth.keys.SCOPES`. If
those drift, a reserved `docdex:` source is guarded by a scope no legitimate dex
key can carry — so the gate blocks the dex client itself, silently. The review
found exactly this: `dex:docdex` was enforced but unmintable, and a test that
fabricated the principal dict directly never noticed.
"""
from __future__ import annotations

from auth.keys import SCOPES
from corpus.store import KNOWN_DEX_IDS


def test_every_reserved_dex_scope_is_in_the_auth_scope_set():
    demanded = {f"dex:{dex}" for dex in KNOWN_DEX_IDS}
    missing = demanded - SCOPES
    assert not missing, (
        f"unmintable dex scopes — add to auth.keys.SCOPES: {sorted(missing)}")
