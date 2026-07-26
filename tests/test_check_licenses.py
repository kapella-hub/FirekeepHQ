"""The licence gate must read all three metadata carriers.

html2text declares License-Expression with zero classifiers; markdownify
declares an MIT classifier with a null License-Expression. A gate reading
one field misses one package.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_licenses
from check_licenses import ATTRIBUTION_EXCLUDE, classify, collect_attribution, print_attributions


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


# --- Attribution mode (NOTICE generation) -----------------------------------
#
# collect_attribution() / print_attributions() back scripts/generate_notice.py,
# which is the only consumer of --attributions. These fakes stand in for
# importlib.metadata.Distribution, which check_licenses.py only ever touches
# through .metadata.get()/.get_all() — the same surface classify() already
# exercises via plain dicts above.


class _FakeMetadata:
    def __init__(self, fields: dict[str, list[str]]):
        self._fields = fields

    def get(self, key, default=None):
        values = self._fields.get(key)
        return values[0] if values else default

    def get_all(self, key):
        return self._fields.get(key)


class _FakeDist:
    def __init__(self, fields: dict[str, list[str]]):
        self.metadata = _FakeMetadata(fields)


def test_collect_attribution_shape_for_a_distribution_with_no_licence_metadata():
    """A distribution declaring none of the three licence fields (the caio
    0.9.25 case documented in scripts/generate_notice.py's
    MANUAL_LICENSE_OVERRIDES) must still produce a complete record rather
    than raising — the whole point of --attributions is that it never
    fails the way the gate's "unknown" path deliberately doesn't fail CI."""
    dist = _FakeDist({"Name": ["caio"], "Version": ["0.9.25"]})
    record = collect_attribution(dist)
    assert record == {
        "name": "caio",
        "version": "0.9.25",
        "verdict": "unknown",
        "license": "no licence metadata",
        "home_page": "",
    }


def test_collect_attribution_reads_home_page_field_directly():
    dist = _FakeDist(
        {
            "Name": ["attrs"],
            "Version": ["26.1.0"],
            "License-Expression": ["MIT"],
            "Home-page": ["https://www.attrs.org/"],
        }
    )
    record = collect_attribution(dist)
    assert record["home_page"] == "https://www.attrs.org/"
    assert record["verdict"] == "ok"


def test_collect_attribution_falls_back_to_project_url_homepage():
    """Home-page is legacy metadata many modern packages omit in favour of
    Project-URL entries; the first Homepage/Home/Source/Repository-labelled
    entry must be used when Home-page itself is absent."""
    dist = _FakeDist(
        {
            "Name": ["annotated-types"],
            "Version": ["0.8.0"],
            "License-Expression": ["MIT"],
            "Project-URL": [
                "Changelog, https://example.com/changelog",
                "Homepage, https://github.com/annotated-types/annotated-types",
            ],
        }
    )
    record = collect_attribution(dist)
    assert record["home_page"] == "https://github.com/annotated-types/annotated-types"


def test_attribution_exclude_covers_bootstrap_and_first_party_names():
    """pip/setuptools/wheel (venv bootstrap tooling, present regardless of
    what was requested) and firekeep-client/firekeep-symdex (this repo's own
    proprietary packages, which install their own name into whatever venv
    installs them) must never appear in a NOTICE — they are not third-party
    attribution material."""
    for name in ("pip", "setuptools", "wheel", "firekeep-client", "firekeep-symdex"):
        assert name in ATTRIBUTION_EXCLUDE


def test_print_attributions_excludes_bootstrap_and_first_party_but_keeps_real_deps(
    monkeypatch, capsys
):
    fake_dists = [
        _FakeDist({"Name": ["pip"], "Version": ["25.2"]}),
        _FakeDist({"Name": ["firekeep-client"], "Version": ["0.1.23"]}),
        _FakeDist(
            {
                "Name": ["httpx"],
                "Version": ["0.28.1"],
                "License-Expression": ["BSD-3-Clause"],
            }
        ),
    ]
    monkeypatch.setattr(check_licenses, "distributions", lambda: fake_dists)

    exit_code = print_attributions()

    assert exit_code == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    records = [json.loads(line) for line in lines]
    names = {r["name"] for r in records}
    assert names == {"httpx"}
