#!/usr/bin/env python3
"""Regenerate NOTICE (root, client/, symdex/) from the actual dependency set.

Builds one throwaway venv per shipped component -- exactly the three targets
scripts/check_licenses.py is already gated against in CI (see
.github/workflows/ci.yml's `licenses` job) -- installs each component's base
dependencies into its own clean environment (never the ambient interpreter,
which accumulates packages from unrelated projects), runs both
`check_licenses.py --attributions` (which licence each package carries) and
`check_licenses.py --license-texts` (that licence's actual bundled text,
where locatable) inside each venv to read what actually got installed, and
merges the results into NOTICE -- written identically to all three of
NOTICE_PATHS, not just the repo root, since PEP 639 license-files globs
can't reach outside a package's own directory.

This intentionally does NOT re-implement licence classification or licence-
file discovery: all of that logic lives in check_licenses.py (`classify`,
`collect_attribution`, `collect_license_text`) and is imported/invoked, not
duplicated, so the CI gate and this generator can never silently disagree
about what licence a package carries or what text backs it.

Usage:
    python scripts/generate_notice.py

Requires network access (pip install) and takes a minute or two -- it is a
maintenance script run by a human when dependencies change, not part of CI.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_LICENSES = REPO_ROOT / "scripts" / "check_licenses.py"

# NOTICE is vendored into three places, all with identical content: PEP 639
# `license-files` globs cannot reach outside a package's own directory (the
# same constraint documented in docs/LICENSING.md for LICENSE itself), so
# client/ and symdex/ each need their own copy for it to land in their
# wheel's METADATA, and the root copy is what COPY NOTICE . in the root
# Dockerfile puts into the shipped server image. Writing all three from one
# generator (rather than hand-copying, the way LICENSE is kept in sync
# today) is what keeps them from drifting apart.
NOTICE_PATHS = [
    REPO_ROOT / "NOTICE",
    REPO_ROOT / "client" / "NOTICE",
    REPO_ROOT / "symdex" / "NOTICE",
]

# (component label, install args passed to `pip install`) -- mirrors the
# `licenses` CI job's three venvs exactly: cortex/requirements.txt covers
# bridge/relay/sentinel too (their deps are a subset of it); client/ and
# symdex/ are the two packages shipped to every customer as wheels. Base
# installs only (no [all]/[test]/[anthropic]/[gemini]/[benchmark] extras) --
# base is what a customer's `pip install` actually resolves.
COMPONENTS: list[tuple[str, str, list[str]]] = [
    (
        "Server (Cortex / Bridge / Relay / Sentinel)",
        "cortex",
        ["-q", "-r", str(REPO_ROOT / "cortex" / "requirements.txt")],
    ),
    (
        "Client kit (firekeep-client)",
        "client",
        ["-q", str(REPO_ROOT / "client")],
    ),
    (
        "Symdex (firekeep-symdex)",
        "symdex",
        ["-q", str(REPO_ROOT / "symdex")],
    ),
]


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kwargs)


def _read_jsonl(result: subprocess.CompletedProcess) -> list[dict]:
    records = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


def build_venv_and_collect(tmpdir: Path, label: str, name: str, pip_args: list[str]) -> list[dict]:
    """Install one component's dependency set into a fresh venv and collect
    both halves of attribution from it: which licence each package carries
    (`--attributions`) and, where locatable, that licence's actual bundled
    text (`--license-texts`). Merged here (as `license_text`, present or
    None) rather than left as two separate lists, so render_notice() only
    ever has one record per package to reason about.
    """
    print(f"[{label}]")
    venv_dir = tmpdir / name
    _run([sys.executable, "-m", "venv", str(venv_dir)])
    venv_python = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")

    _run([str(venv_python), "-m", "pip", "install", *pip_args])

    attributions = _read_jsonl(_run(
        [str(venv_python), str(CHECK_LICENSES), "--attributions"],
        capture_output=True,
        text=True,
    ))
    license_texts = _read_jsonl(_run(
        [str(venv_python), str(CHECK_LICENSES), "--license-texts"],
        capture_output=True,
        text=True,
    ))
    text_by_name = {rec["name"].lower(): rec["license_text"] for rec in license_texts}

    records = []
    with_text = 0
    for rec in attributions:
        rec = dict(rec)
        text = text_by_name.get(rec["name"].lower())
        rec["license_text"] = text
        if text is not None:
            with_text += 1
        records.append(rec)

    print(f"  -> {len(records)} third-party distributions "
          f"({with_text} with a locatable bundled licence text)")
    return records


# Packages where automated metadata scanning (License-Expression /
# Classifier / License, in that priority order — see check_licenses.py)
# cannot produce a verdict, but a human has actually opened the installed
# dist-info's bundled licence file and confirmed the licence. Recorded here
# (not just fixed by hand in NOTICE) so a regeneration after a dependency
# bump doesn't silently regress the entry back to "needs human review" and
# get missed.
#
# caio 0.9.25 (transitive dep, pulled in via the cortex requirements set):
# declares no License-Expression and no "License ::" classifier at all —
# only a bare `License-File: COPYING` pointing at a bundled file. Read
# directly from the installed venv
# (.../caio-0.9.25.dist-info/licenses/COPYING) on 2026-07-26: full Apache
# License, Version 2.0 text, with the boilerplate copyright notice
# "Copyright 2025 Dmitry Orlov <me@mosquito.su>" filled in at the bottom.
# Permissive; safe to attribute as Apache-2.0.
MANUAL_LICENSE_OVERRIDES: dict[str, str] = {
    "caio": "Apache-2.0 (confirmed by reading the bundled COPYING file — "
    "no scannable metadata field declares it; see comment in this script)",
}


def render_notice(component_records: list[tuple[str, list[dict]]]) -> str:
    lines = [
        "Firekeep — Third-Party Software Notices",
        "=" * 60,
        "",
        "Firekeep is source-available under BUSL-1.1 (see LICENSE). It incorporates the",
        "third-party open-source components listed below, each governed by",
        "its own licence, independent of the Firekeep licence terms.",
        "",
        "This file covers Python dependencies installed into the shipped",
        "server image (cortex/requirements.txt, which also covers bridge,",
        "relay, and sentinel) and the two client wheels distributed to every",
        "customer (firekeep-client, firekeep-symdex). It does not cover the",
        "bundled datastore container images (Neo4j, Redis, Qdrant, Ollama) —",
        "see docs/THIRD-PARTY-DATASTORES.md for those.",
        "",
        "Below, each dependency is identified by name, version, and the",
        "licence its own package metadata declares. The Appendix at the end",
        "of this file reproduces the actual bundled licence text for every",
        "dependency where one could be located in the installed package —",
        "naming a licence does not by itself satisfy the copyright-notice-",
        "and-permission-text requirement most permissive licences attach to",
        "redistribution, so this file carries both halves.",
        "",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')} by "
        "scripts/generate_notice.py. Regenerate after any dependency change;",
        "do not hand-edit the package list or the appendix below. This exact",
        "file is vendored, unchanged, at client/NOTICE and symdex/NOTICE too",
        "(PEP 639 license-files globs cannot reach outside a package's own",
        "directory, so each wheel needs its own copy to carry it) — the",
        "generator writes all three from this one render, so they cannot",
        "drift apart.",
        "",
    ]

    unknown_flagged: list[str] = []

    for label, records in component_records:
        lines.append("-" * 60)
        lines.append(label)
        lines.append("-" * 60)
        lines.append("")
        for rec in sorted(records, key=lambda r: r["name"].lower()):
            home = f" — {rec['home_page']}" if rec["home_page"] else ""
            lines.append(f"* {rec['name']} {rec['version']}{home}")
            override = MANUAL_LICENSE_OVERRIDES.get(rec["name"].lower())
            if override:
                lines.append(f"    Licence: {override}")
            else:
                lines.append(f"    Licence: {rec['license']}")
                if rec["verdict"] == "unknown":
                    unknown_flagged.append(f"{label}: {rec['name']} {rec['version']}")
        lines.append("")

    if unknown_flagged:
        lines.append("-" * 60)
        lines.append("NEEDS HUMAN REVIEW — licence could not be classified")
        lines.append("-" * 60)
        lines.append("")
        lines.append(
            "The following packages did not carry a recognizable licence"
        )
        lines.append(
            "signal in License-Expression, Classifier, or License metadata."
        )
        lines.append(
            "scripts/check_licenses.py does not fail the build on these"
        )
        lines.append(
            "(failing on 'unknown' would make the CI gate noise), but they"
        )
        lines.append("have not been confirmed permissive:")
        lines.append("")
        for entry in unknown_flagged:
            lines.append(f"  - {entry}")
        lines.append("")

    lines.append("=" * 60)
    lines.append("Appendix: Bundled Third-Party Licence Texts")
    lines.append("=" * 60)
    lines.append("")
    lines.append(
        "The texts below are read directly from each dependency's own"
    )
    lines.append(
        "installed licence file(s) — not retyped, summarised, or"
    )
    lines.append(
        "paraphrased. Reproduced to satisfy the obligations that naming a"
    )
    lines.append(
        "licence in the section above does not discharge: MIT/BSD-style"
    )
    lines.append(
        "licences conventionally require the copyright notice and"
    )
    lines.append(
        "permission text to travel with a redistribution; Apache-2.0 §4(d)"
    )
    lines.append(
        "requires carrying forward any upstream NOTICE file content."
    )
    lines.append("")

    no_text_found: list[str] = []

    for label, records in component_records:
        lines.append("-" * 60)
        lines.append(label)
        lines.append("-" * 60)
        lines.append("")
        for rec in sorted(records, key=lambda r: r["name"].lower()):
            text = rec.get("license_text")
            if text is None:
                no_text_found.append(f"{label}: {rec['name']} {rec['version']}")
                continue
            lines.append(f"--- {rec['name']} {rec['version']} ---")
            lines.append("")
            lines.append(text)
            lines.append("")

    if no_text_found:
        lines.append("-" * 60)
        lines.append("No bundled licence file located — text not reproduced above")
        lines.append("-" * 60)
        lines.append("")
        lines.append(
            "The following packages carry no licence file that could be"
        )
        lines.append(
            "found in the installed distribution (checked via the PEP 639"
        )
        lines.append(
            "License-File metadata field, then a fallback scan for a"
        )
        lines.append(
            "LICENSE/LICENCE/COPYING-named file among its installed"
        )
        lines.append(
            "contents) — only their declared licence name, above, is"
        )
        lines.append("available; this is a known gap, not silently dropped:")
        lines.append("")
        for entry in no_text_found:
            lines.append(f"  - {entry}")
        lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="firekeep-notice-") as tmp:
        tmpdir = Path(tmp)
        component_records = []
        for label, name, pip_args in COMPONENTS:
            records = build_venv_and_collect(tmpdir, label, name, pip_args)
            component_records.append((label, records))

    notice_text = render_notice(component_records)
    for path in NOTICE_PATHS:
        path.write_text(notice_text, encoding="utf-8", newline="\n")
    total = sum(len(records) for _, records in component_records)
    paths_str = ", ".join(str(p.relative_to(REPO_ROOT)) for p in NOTICE_PATHS)
    print(f"\nWrote {paths_str} ({total} third-party distributions across "
          f"{len(component_records)} components).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
