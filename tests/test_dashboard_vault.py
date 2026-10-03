"""Dashboard Vault tab — filter and a reveal that stays inside the card.

Reported 2026-10-03: the vault had no search, and Reveal drew the value as a
`white-space:nowrap` tooltip beside the button, so a long token ran off the
right edge (the user had to scroll sideways) and vanished after 5 s. Layout was
measured in a real browser at 800-1920 px wide (no horizontal overflow in the
list); these assertions pin the pieces that made that true so a refactor can't
quietly bring the tooltip back.
"""
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"


def _src() -> str:
    return HTML.read_text(encoding="utf-8")


def test_vault_has_a_filter_over_key_category_description_and_tags():
    src = _src()
    assert 'id="vaultSearch"' in src and 'id="vaultSearchCount"' in src
    assert "function vaultSecretMatches(" in src
    assert ".concat(secret.tags || [])" in src
    # The filter re-renders from the cached list; it must not refetch per keystroke.
    assert "$('vaultSearch').addEventListener('input', renderVaultSecrets)" in src


def test_reveal_opens_a_wrapping_row_not_a_nowrap_tooltip():
    src = _src()
    vault_js = src[src.index("function renderVaultSecrets()"):src.index("function hideVaultReveal(")]
    assert "vault-reveal-row" in vault_js and "td.colSpan = tr.children.length" in vault_js
    assert "white-space:nowrap;z-index:10" not in vault_js
    assert "box.textContent = val" in vault_js, "the secret must be set as text, never as HTML"
    css = src[src.index(".vault-reveal-value {"):]
    css = css[:css.index("}")]
    assert "overflow-wrap: anywhere" in css and "white-space: pre-wrap" in css


def test_revealed_value_stays_long_enough_to_copy():
    src = _src()
    assert "var VAULT_REVEAL_MS = 30000;" in src
    assert "navigator.clipboard.writeText(val)" in src


def test_vault_layout_gives_the_list_room_and_lets_it_shrink():
    src = _src()
    assert '<div class="two-col section vault-layout">' in src
    assert ".two-col.vault-layout { grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); }" in src
    assert ".two-col.vault-layout > * { min-width: 0; }" in src
