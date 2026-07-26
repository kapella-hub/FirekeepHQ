#!/usr/bin/env python3
"""Fail the build on copyleft/source-available dependencies.

Reads all three carriers of licence metadata, because packages populate
different ones: html2text sets License-Expression and no classifiers;
markdownify sets an MIT classifier and no License-Expression.

Exit 1 on any denied package. Unknown packages are reported, not fatal —
they need a human read, and failing on them would make the gate noise.

Also doubles as the attribution reader for NOTICE generation: run with
`--attributions` to emit one JSON record per installed third-party
distribution (JSONL to stdout) instead of gating. See
scripts/generate_notice.py, which invokes this in each isolated venv and
merges the results into the repository's NOTICE file.
"""
from __future__ import annotations

import sys
from importlib.metadata import distributions

DENIED = (
    "GPL-3.0", "GPL-2.0", "GPLV3", "GPLV2",
    # Bare, versionless classifier — e.g. the real trove classifier
    # "License :: OSI Approved :: GNU General Public License (GPL)" carries
    # no version token, so it would otherwise slip through as "unknown".
    "GPL",
    "AGPL", "AFFERO",
    "SSPL",
    "BUSL", "BSL-1.1", "BUSINESS SOURCE",
)

# Masked out of the text before DENIED is scanned: these contain a denied
# substring ("LGPL" contains "GPL") but are not themselves denied.
ALLOWED_OVERRIDES = ("LGPL",)

PERMISSIVE = (
    "MIT", "BSD", "APACHE", "ISC", "PSF", "PYTHON SOFTWARE FOUNDATION",
    "MOZILLA", "MPL", "UNLICENSE", "ZLIB", "CC0",
)

# Priority order for classify(): the first field that yields a determinate
# verdict wins. License-Expression and Classifier are short, structured
# strings; the legacy License field is free text that can embed an entire
# bundled LICENSE file covering sub-components (matplotlib's License field
# embeds FreeType's dual-licensed "FTL OR GPL-2.0-or-later" text even though
# matplotlib's own declared license — visible cleanly in its Classifier — is
# permissive). Checking it last means that kind of noise can only supply a
# verdict when nothing more authoritative already has.
FIELDS = ("License-Expression", "Classifier", "License")

# Cap on the field text quoted in printed CI output — a legacy License field
# can be tens of KB (a bundled LICENSE file); no single package's license
# statement needs that much of the log.
DETAIL_PRINT_MAX_CHARS = 300


def _verdict_for_field_text(upper: str) -> str | None:
    """Return "denied"/"ok" for one field's text, or None if inconclusive.

    LGPL is masked out of the text before the DENIED scan runs, rather than
    vetoing the whole field the instant "LGPL" appears anywhere in it. A
    blanket veto let an LGPL substring hide a *sibling* denied licence in
    the same field — e.g. a dual-license classifier list carrying both an
    LGPL classifier and a GPL-3.0 classifier, or a dual-license SPDX
    expression like "LGPL-2.1-or-later OR GPL-3.0-or-later" — because
    "LGPL" itself contains "GPL" as a substring. Masking only the override
    text (rather than bailing out on the whole field) preserves every
    other denied/permissive signal still present in the field, while a
    field that is *purely* an override still ends up with nothing left to
    match and correctly falls through as inconclusive.
    """
    masked = upper
    for override in ALLOWED_OVERRIDES:
        masked = masked.replace(override, " ")

    for bad in DENIED:
        if bad in masked:
            return "denied"

    if any(good in masked for good in PERMISSIVE):
        return "ok"

    return None


def classify(meta: dict[str, list[str]]) -> tuple[str, str]:
    """Return (verdict, detail) for one distribution's metadata.

    verdict is "denied", "ok", or "unknown". Fields are checked in FIELDS
    priority order and the first field to produce a determinate verdict
    wins — see the FIELDS comment for why order matters here.
    """
    collected: list[str] = []

    for field in FIELDS:
        values = [raw for raw in meta.get(field, []) if raw]
        if field == "Classifier":
            values = [v for v in values if v.startswith("License ::")]
        if not values:
            continue

        field_text = " ; ".join(values)
        collected.append(field_text)

        verdict = _verdict_for_field_text(field_text.upper())
        if verdict is not None:
            return verdict, field_text

    if not collected:
        return "unknown", "no licence metadata"

    return "unknown", " ; ".join(collected)


def _truncate_for_print(detail: str) -> str:
    if len(detail) <= DETAIL_PRINT_MAX_CHARS:
        return detail
    return detail[:DETAIL_PRINT_MAX_CHARS] + f"... [{len(detail)} chars total, truncated]"


# Packages excluded from attribution output: venv bootstrap tooling that is
# never imported by shipped application code (present in every venv
# regardless of what was actually requested), plus this repo's own
# first-party distributions, which install their own name into whatever
# venv installs them (`pip install ./client` -> "firekeep-client" shows up
# in distributions() alongside its real third-party dependencies) and carry
# the proprietary licence, not a third-party one.
ATTRIBUTION_EXCLUDE = frozenset(
    {"pip", "setuptools", "wheel", "firekeep-client", "firekeep-symdex"}
)


def collect_attribution(dist) -> dict[str, str]:
    """Return one third-party-attribution record for a distribution.

    Reuses classify() so the NOTICE generator and the CI gate agree on what
    a package's licence *is* — there is exactly one place that interprets
    License-Expression/Classifier/License, not two scanners that could
    silently drift apart.
    """
    name = dist.metadata.get("Name", "") or "<unnamed>"
    version = dist.metadata.get("Version", "") or "unknown"
    meta: dict[str, list[str]] = {f: dist.metadata.get_all(f) or [] for f in FIELDS}
    verdict, detail = classify(meta)
    home_page = dist.metadata.get("Home-page", "") or ""
    if not home_page:
        for project_url in dist.metadata.get_all("Project-URL") or []:
            label, _, url = project_url.partition(",")
            if label.strip().lower() in ("homepage", "home", "source", "repository"):
                home_page = url.strip()
                break
    return {
        "name": name,
        "version": version,
        "verdict": verdict,
        "license": _truncate_for_print(detail),
        "home_page": home_page,
    }


def print_attributions() -> int:
    """Emit one JSON record per line (JSONL) for every third-party
    distribution installed in the current interpreter, to stdout.

    Intended to be invoked once per isolated venv by
    scripts/generate_notice.py, which is the only piece that knows how to
    merge the per-component results into the repository's NOTICE file —
    this function's only job is reading the *current* environment, exactly
    like main()'s gate does.
    """
    import json

    for dist in distributions():
        name = dist.metadata.get("Name", "") or "<unnamed>"
        if name.lower() in ATTRIBUTION_EXCLUDE:
            continue
        print(json.dumps(collect_attribution(dist)))
    return 0


def main() -> int:
    denied: list[str] = []
    unknown: list[str] = []

    for dist in distributions():
        name = dist.metadata["Name"] or "<unnamed>"
        meta: dict[str, list[str]] = {f: dist.metadata.get_all(f) or [] for f in FIELDS}
        verdict, detail = classify(meta)
        detail = _truncate_for_print(detail)
        if verdict == "denied":
            denied.append(f"{name}: {detail}")
        elif verdict == "unknown":
            unknown.append(f"{name}: {detail}")

    if unknown:
        print("UNKNOWN licence (needs a human read):")
        for line in sorted(unknown):
            print(f"  {line}")

    if denied:
        print("\nDENIED licence — build fails:")
        for line in sorted(denied):
            print(f"  {line}")
        return 1

    print("\nNo denied licences found.")
    return 0


if __name__ == "__main__":
    if "--attributions" in sys.argv:
        sys.exit(print_attributions())
    sys.exit(main())
