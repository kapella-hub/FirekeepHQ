"""The licence gate must read all three metadata carriers.

html2text declares License-Expression with zero classifiers; markdownify
declares an MIT classifier with a null License-Expression. A gate reading
one field misses one package.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_licenses import classify


def test_denies_gpl_declared_only_in_license_expression():
    verdict, detail = classify({"License-Expression": ["GPL-3.0-or-later"]})
    assert verdict == "denied"
    assert "GPL-3.0-or-later" in detail


def test_allows_mit_declared_only_in_classifier():
    verdict, _ = classify({"Classifier": ["License :: OSI Approved :: MIT License"]})
    assert verdict == "ok"


def test_denies_agpl_in_classifier():
    meta = {"Classifier": ["License :: OSI Approved :: GNU Affero General Public License v3"]}
    verdict, _ = classify(meta)
    assert verdict == "denied"


def test_denies_sspl_in_legacy_license_field():
    verdict, _ = classify({"License": ["SSPL-1.0"]})
    assert verdict == "denied"


def test_reports_unknown_when_no_licence_metadata_at_all():
    verdict, _ = classify({})
    assert verdict == "unknown"


def test_permissive_expression_is_ok():
    verdict, _ = classify({"License-Expression": ["Apache-2.0"]})
    assert verdict == "ok"


def test_lgpl_is_not_matched_by_the_gpl_rule():
    """LGPL is weak copyleft and is reported, not denied — a deliberate
    policy choice, and a substring match on 'GPL' would get it wrong."""
    verdict, _ = classify({"License-Expression": ["LGPL-2.1-only"]})
    assert verdict == "unknown"


def test_denies_bare_gpl_classifier_with_no_version_token():
    """The real PyPI trove classifier "License :: OSI Approved :: GNU
    General Public License (GPL)" carries no version substring at all —
    a package declaring only this classifier, with no License-Expression,
    must still be denied rather than falling through to unknown."""
    meta = {
        "Classifier": ["License :: OSI Approved :: GNU General Public License (GPL)"]
    }
    verdict, _ = classify(meta)
    assert verdict == "denied"


def test_bare_gpl_classifier_lgpl_variant_is_still_the_override():
    """The sibling classifier for LGPL ("... Lesser General Public License
    (LGPL)") must still resolve as the LGPL override, not the bare-GPL
    rule — it contains "GPL" too (LGPL masking must remove exactly the
    override text and nothing else)."""
    meta = {
        "Classifier": [
            "License :: OSI Approved :: GNU Library or Lesser General Public License (LGPL)"
        ]
    }
    verdict, _ = classify(meta)
    assert verdict == "unknown"


def test_denies_gpl_sibling_classifier_alongside_an_lgpl_classifier():
    """A dual-license-shaped classifier list carrying both an LGPL
    classifier and a genuine GPL-3.0 classifier must be denied — the LGPL
    override must not mask a sibling denied classifier in the same field."""
    meta = {
        "Classifier": [
            "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
            "License :: OSI Approved :: GNU Library or Lesser General Public License (LGPL)",
        ]
    }
    verdict, _ = classify(meta)
    assert verdict == "denied"


def test_denies_dual_license_spdx_expression_with_lgpl_and_gpl():
    """A single dual-license SPDX License-Expression string mixing LGPL and
    GPL terms must be denied, not masked wholesale by the LGPL override."""
    verdict, _ = classify(
        {"License-Expression": ["LGPL-2.1-or-later OR GPL-3.0-or-later"]}
    )
    assert verdict == "denied"


def test_denies_gplv3_classifier_via_its_own_denied_entry():
    """DENIED carried "GPLv3"/"GPLv2" in mixed case while every comparison
    runs against an always-uppercased string -- those two entries could
    never match anything (dead code, silently masked by the separate bare
    "GPL" catch-all rule). Regression guard now that they're uppercased."""
    meta = {
        "Classifier": ["License :: OSI Approved :: GNU General Public License v3 (GPLv3)"]
    }
    verdict, _ = classify(meta)
    assert verdict == "denied"


def test_denies_gplv2_classifier_via_its_own_denied_entry():
    meta = {
        "Classifier": ["License :: OSI Approved :: GNU General Public License v2 (GPLv2)"]
    }
    verdict, _ = classify(meta)
    assert verdict == "denied"


def test_bundled_multi_license_text_does_not_override_a_clean_classifier():
    """matplotlib's License field embeds the *entire* bundled LICENSE file,
    including FreeType's dual-licensed "FTL OR GPL-2.0-or-later" sub-component
    text, even though matplotlib's own declared license — visible cleanly via
    its Classifier — is the permissive PSF license. A substring scan over the
    whole License blob must not out-vote the clean, structured Classifier
    signal, and the returned detail must not balloon to the full blob."""
    meta = {
        "License": [
            "License agreement for matplotlib versions 1.3.0 and later\n"
            "=========================================================\n"
            "... (thousands of characters of MDT licence terms) ...\n"
            "Name: FreeType\n"
            "Files: matplotlib/ft2font.*.so\n"
            "Description: Font rendering library\n"
            "License: FTL OR GPL-2.0-or-later\n"
            "  The FreeType 2 font engine is copyrighted work ...\n"
        ],
        "Classifier": [
            "License :: OSI Approved :: Python Software Foundation License"
        ],
    }
    verdict, detail = classify(meta)
    assert verdict == "ok"
    assert "GPL" not in detail
