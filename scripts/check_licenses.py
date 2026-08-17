#!/usr/bin/env python3
"""Fail the build on copyleft/source-available dependencies.

Reads all three carriers of licence metadata, because packages populate
different ones: html2text sets License-Expression and no classifiers;
markdownify sets an MIT classifier and no License-Expression.

Exit 1 on any denied package. Unknown packages are reported, not fatal —
they need a human read, and failing on them would make the gate noise.

Also doubles as the attribution reader for NOTICE generation: run with
`--attributions` to emit one JSON record per installed third-party
distribution (JSONL to stdout) identifying its licence, or `--license-texts`
to emit one JSON record per distribution that has a locatable bundled
licence-text file (the actual text, not just its name) -- instead of
gating. See scripts/generate_notice.py, which invokes both in each isolated
venv and merges the results into the repository's NOTICE file.
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


# This repo's own distributions appear alongside their dependencies after
# `pip install ./client`, `./symdex` or `./docdex`. They are governed by the
# repository's licence checks, not the third-party dependency policy below.
FIRST_PARTY_DISTRIBUTIONS = frozenset(
    {"firekeep-client", "firekeep-symdex", "firekeep-docdex"}
)

# Packages excluded from attribution output: venv bootstrap tooling that is
# never imported by shipped application code (present in every venv
# regardless of what was actually requested), plus this repo's own
# first-party distributions.
ATTRIBUTION_EXCLUDE = frozenset(
    {"pip", "setuptools", "wheel"}
) | FIRST_PARTY_DISTRIBUTIONS


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


# Naming this scan targets: a pre-PEP-639 package (declares no License-File
# metadata at all, e.g. httpx 0.28.1) that still bundles a licence file
# under a recognizable name. Checked only as a fallback, after the
# metadata-declared list, so a package that DOES declare License-File never
# falls back to a guess.
LICENSE_FILENAME_PREFIXES = ("license", "licence", "copying")


def _match_declared_file(files: list, rel: str):
    """Resolve one PEP 639 `License-File` metadata value to the installed
    PackagePath it names.

    The declared value is a path relative to the dist-info's `licenses/`
    subdirectory (e.g. pywin32's `License-File: com/License.txt` names
    `pywin32-312.dist-info/licenses/com/License.txt`, not the also-installed
    `com/License.txt` runtime copy, which may not even be the same file for
    every package). Falls back to an exact installed-path match, then a
    bare basename match, for older layouts that don't use the `licenses/`
    subdirectory convention at all.
    """
    rel_norm = rel.replace("\\", "/")
    for f in files:
        if str(f).replace("\\", "/").endswith(f"licenses/{rel_norm}"):
            return f
    for f in files:
        if str(f).replace("\\", "/") == rel_norm:
            return f
    base = rel_norm.rsplit("/", 1)[-1].lower()
    for f in files:
        if f.name.lower() == base:
            return f
    return None


def _looks_like_top_level_license_file(path_str: str) -> bool:
    """True only for a licence-shaped basename sitting at the top level of
    the install (a bare `LICENSE`/`COPYING`-style file next to the
    package) or inside the distribution's own `*.dist-info/` directory.

    This is deliberately narrow. It exists only for the pre-PEP-639
    fallback case (no declared `License-File` metadata at all), where
    matching on basename alone is dangerously easy to get wrong: openapi-
    pydantic 0.5.1 ships a real source module at
    `openapi_pydantic/v3/v3_0/license.py` (plus its `__pycache__` .pyc) --
    a basename-only scan reads the compiled bytecode as "licence text" and
    corrupts the appendix with binary garbage. Requiring top-level-or-
    dist-info placement excludes that without needing an extension
    denylist that would also have to special-case real license-file
    naming conventions like cryptography's `LICENSE.APACHE`/`LICENSE.BSD`.
    """
    parts = path_str.split("/")
    if "__pycache__" in parts:
        return False
    if len(parts) == 1:
        return True
    return parts[0].endswith(".dist-info")


def _candidate_license_paths(dist) -> list:
    """Return the dist.files PackagePath entries most likely to hold this
    distribution's bundled licence text, preferring the files the
    distribution's own PEP 639 `License-File` metadata names."""
    files = list(getattr(dist, "files", None) or [])
    if not files:
        return []

    declared = dist.metadata.get_all("License-File") or []
    if declared:
        matches = []
        for rel in declared:
            match = _match_declared_file(files, rel)
            if match is not None:
                matches.append(match)
        if matches:
            return matches

    return [
        f for f in files
        if _looks_like_top_level_license_file(str(f).replace("\\", "/"))
        and any(f.name.lower().startswith(prefix) for prefix in LICENSE_FILENAME_PREFIXES)
    ]


def collect_license_text(dist) -> str | None:
    """Return the concatenated bundled licence-file text for a
    distribution, or None if no such file could be located in what was
    actually installed.

    This is the text-reproduction half of attribution that identifying a
    licence by name does not discharge: MIT/BSD conventionally require the
    copyright notice and permission text to travel with a redistribution,
    and Apache-2.0 §4(d) requires carrying forward any upstream NOTICE
    content. See scripts/generate_notice.py, which assembles these into
    NOTICE's licence-text appendix.
    """
    paths = _candidate_license_paths(dist)
    if not paths:
        return None

    blocks: list[str] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                text = path.read_binary().decode("utf-8", errors="replace")
            except (OSError, ValueError):
                continue
        except (OSError, ValueError):
            continue
        if text and text.strip():
            blocks.append(text.strip())

    return "\n\n".join(blocks) if blocks else None


def print_license_texts() -> int:
    """Emit one JSON record per line (JSONL) for every third-party
    distribution that has a locatable bundled licence-text file, to
    stdout. Companion to print_attributions(): that function identifies
    *which* licence a package carries; this one reproduces the actual text,
    read from the same isolated venv so scripts/generate_notice.py can
    bundle real licence text into NOTICE, not just the licence's name.
    Distributions with no locatable licence file are omitted here (not
    emitted with a null text) — generate_notice.py treats "no record" as
    "nothing found" and says so explicitly in the appendix.
    """
    import json

    for dist in distributions():
        name = dist.metadata.get("Name", "") or "<unnamed>"
        if name.lower() in ATTRIBUTION_EXCLUDE:
            continue
        text = collect_license_text(dist)
        if text is None:
            continue
        version = dist.metadata.get("Version", "") or "unknown"
        print(json.dumps({"name": name, "version": version, "license_text": text}))
    return 0


def main() -> int:
    denied: list[str] = []
    unknown: list[str] = []

    for dist in distributions():
        name = dist.metadata["Name"] or "<unnamed>"
        if name.lower() in FIRST_PARTY_DISTRIBUTIONS:
            continue
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
    if "--license-texts" in sys.argv:
        sys.exit(print_license_texts())
    sys.exit(main())
