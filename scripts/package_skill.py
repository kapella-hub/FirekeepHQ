"""Package a skill as a zip for marketplaces that take uploads (e.g. Agensi).

    python scripts/package_skill.py                    # all skills under skills/
    python scripts/package_skill.py install-firekeep   # just one

Writes to build/skills/<name>.zip (build/ is gitignored).

The archive contains the skill DIRECTORY, not a loose SKILL.md:

    install-firekeep/SKILL.md

That shape is not cosmetic. The agentskills.io spec requires the frontmatter
`name` to match its parent directory name, so an archive that unpacks to a bare
SKILL.md loses the only thing carrying that relationship, and a reviewer's
validator can reject it. Unpacking this zip into ~/.claude/skills or
~/.codex/skills also just works, with no renaming step.

Validates before writing, because a rejected upload costs a review cycle:
frontmatter parses, `name` matches the directory and the spec's charset, and
NO key outside the spec's six is present (claude.ai/API packaging hard-errors
on extras, and marketplaces validate against the same spec).
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
OUT = ROOT / "build" / "skills"
SPEC_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}


def validate(skill_dir: Path) -> str:
    md = skill_dir / "SKILL.md"
    if not md.exists():
        raise SystemExit(f"{skill_dir.name}: no SKILL.md")
    text = md.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise SystemExit(f"{skill_dir.name}: SKILL.md has no YAML frontmatter")
    front = text[4:text.index("\n---\n", 3)]

    keys = set(re.findall(r"^([A-Za-z][A-Za-z0-9_-]*):", front, re.M))
    if extra := sorted(keys - SPEC_FIELDS):
        raise SystemExit(
            f"{skill_dir.name}: non-spec frontmatter field(s) {extra}. "
            f"Uploads reject anything outside {sorted(SPEC_FIELDS)}."
        )

    name = re.search(r"^name: (.+)$", front, re.M)
    if not name:
        raise SystemExit(f"{skill_dir.name}: frontmatter has no name")
    value = name.group(1).strip()
    if value != skill_dir.name:
        raise SystemExit(
            f"{skill_dir.name}: frontmatter name is {value!r}; the spec requires "
            "it to match the directory name"
        )
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", value) or len(value) > 64:
        raise SystemExit(f"{skill_dir.name}: name {value!r} violates the spec's charset/length")
    if not re.search(r"^description: .+$", front, re.M):
        raise SystemExit(f"{skill_dir.name}: frontmatter has no description")
    return value


def package(skill_dir: Path) -> Path:
    name = validate(skill_dir)
    OUT.mkdir(parents=True, exist_ok=True)
    archive = OUT / f"{name}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(skill_dir.rglob("*")):
            if path.is_file():
                z.write(path, f"{name}/{path.relative_to(skill_dir).as_posix()}")
    return archive


def main() -> int:
    wanted = sys.argv[1:]
    dirs = [SKILLS / w for w in wanted] if wanted else sorted(
        d for d in SKILLS.iterdir() if d.is_dir()
    )
    if not dirs:
        raise SystemExit(f"no skills found under {SKILLS}")
    for skill_dir in dirs:
        if not skill_dir.is_dir():
            raise SystemExit(f"not a skill directory: {skill_dir}")
        archive = package(skill_dir)
        with zipfile.ZipFile(archive) as z:
            entries = z.namelist()
        print(f"{archive}  ({len(entries)} file(s): {', '.join(entries)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
