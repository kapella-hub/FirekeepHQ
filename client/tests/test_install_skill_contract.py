"""The published install skill (skills/install-firekeep/SKILL.md) must stay true
to the CLI it instructs an agent to drive.

Why this file exists: that skill is shipped to third-party marketplaces and read
by agents on machines we never see. A command or flag renamed here becomes an
agent confidently running something that does not exist on a stranger's laptop,
with no error we would ever observe. The repo already has a documented history
of exactly this failure shape (the retired symdex plugin's SessionStart hook
telling agents to call a tool it could not reach), so the skill's claims are
pinned mechanically rather than by review.

Two kinds of assertion here:
  * SURFACE — every `firekeep <subcommand>` and every flag the skill names is
    parsed out of the prose and checked against the real argparse parser.
  * BEHAVIOR — the skill's entire safety design rests on one property:
    `firekeep install --non-interactive` cannot provision a server. That is
    asserted against the real code path, not against the prose that claims it.
"""
from __future__ import annotations

import re
import types
from pathlib import Path

import pytest

from firekeep_client import cli, wizard

REPO_ROOT = Path(__file__).resolve().parents[2]
# The repo IS the plugin: .claude-plugin/ holds both manifests and skills/
# sits at the plugin (= repo) root. That placement is deliberate — it is
# also the convention skills.sh auto-indexes (skills/<name>/SKILL.md at a
# public repo root), which has no submission process at all, so the layout
# buys a discovery channel for free.
PLUGIN_ROOT = REPO_ROOT
SKILL = REPO_ROOT / "skills" / "install-firekeep" / "SKILL.md"


def _skill_text() -> str:
    assert SKILL.exists(), f"the published skill is missing: {SKILL}"
    return SKILL.read_text(encoding="utf-8")


def _parser_subcommands() -> set[str]:
    parser = cli._build_parser()
    names: set[str] = set()
    for action in parser._actions:
        if getattr(action, "choices", None):
            names |= set(action.choices.keys())
    return names


def _flags_for(subcommand: str) -> set[str]:
    parser = cli._build_parser()
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if choices and subcommand in choices:
            flags: set[str] = set()
            for sub_action in choices[subcommand]._actions:
                flags |= set(sub_action.option_strings)
            return flags
    raise AssertionError(f"subcommand not in parser: {subcommand}")


def test_every_firekeep_subcommand_named_by_the_skill_exists():
    """Parse `firekeep <word>` out of the skill and check each against the
    parser. Catches a renamed or removed subcommand before it ships."""
    text = _skill_text()
    # `firekeep` followed by a word that isn't a flag. Excludes bare mentions
    # ("the firekeep command") via the lowercase-letter class.
    mentioned = set(re.findall(r"`?firekeep (?!install --)([a-z][a-z-]+)", text))
    # Prose words that follow "firekeep" without being subcommands.
    prose = {"doctor", "is", "the", "command", "tools", "update"} & set()
    mentioned -= prose
    real = _parser_subcommands()
    unknown = sorted(m for m in mentioned if m not in real)
    assert not unknown, (
        f"skill names subcommand(s) the CLI does not have: {unknown}\n"
        f"real subcommands: {sorted(real)}"
    )
    # Guard against the regex silently matching nothing and the test passing
    # vacuously -- the skill definitely drives several commands.
    assert len(mentioned) >= 5, f"only matched {mentioned}; regex likely broke"


@pytest.mark.parametrize(
    "subcommand,flag",
    [
        ("install", "--non-interactive"),
        ("install", "--host"),
        ("install", "--join"),
        ("uninstall", "--server"),
    ],
)
def test_flags_named_by_the_skill_exist(subcommand, flag):
    assert flag in _flags_for(subcommand), (
        f"the skill instructs `firekeep {subcommand} {flag}`, which the parser "
        f"does not accept"
    )


def test_doctor_output_quoted_by_the_skill_is_real():
    """The skill teaches an agent to recognize specific doctor output. If that
    wording changes, the skill teaches a stranger's agent to misdiagnose."""
    quoted = "This machine has a Firekeep client but no server to talk to"
    assert quoted in _skill_text(), "skill no longer quotes the no-server row"
    source = (REPO_ROOT / "client" / "firekeep_client" / "cli.py").read_text(encoding="utf-8")
    assert quoted in source, (
        "doctor's no-server message changed; skills/install-firekeep/SKILL.md "
        "quotes the old wording and must be updated with it"
    )


def test_install_non_interactive_cannot_provision_a_server():
    """THE load-bearing safety property.

    The skill tells an agent to always install with --non-interactive, and
    treats `firekeep init` as a separate step requiring explicit human consent.
    That is only safe because the non-interactive path cannot reach provisioning:
    `_configure` returns plan=None when not interactive (cli.py), and the
    provisioning branch is guarded by `plan is not None and plan.action ==
    PROVISION_HERE`.

    Asserted at the source level rather than by executing a real install (which
    would provision a server if it were broken -- a test that fails by doing the
    exact damage it is checking for is not a test worth running).
    """
    source = (REPO_ROOT / "client" / "firekeep_client" / "cli.py").read_text(encoding="utf-8")

    # The provisioning call exists exactly once, and is guarded by a plan check.
    provision_lines = [
        (i, line) for i, line in enumerate(source.splitlines(), 1)
        if "cmd_init(_init_args_for_self_provision" in line
    ]
    assert len(provision_lines) == 1, (
        f"expected exactly one self-provision call site, found {provision_lines}"
    )
    lineno, _ = provision_lines[0]
    guard = "\n".join(source.splitlines()[max(0, lineno - 5):lineno])
    assert "plan is not None" in guard and "PROVISION_HERE" in guard, (
        "the self-provision call is no longer guarded by "
        "`plan is not None and plan.action == PROVISION_HERE`. If provisioning "
        "can now be reached without an interactive plan, "
        "skills/install-firekeep/SKILL.md's safety design is void and must be "
        "rewritten before release."
    )

    # And plan is None on the non-interactive path.
    assert "interactive = wizard.is_interactive() and not getattr(args, \"non_interactive\", False)" in source
    assert "plan: wizard.Plan | None = None" in source


def test_non_interactive_flag_is_what_the_skill_says_it_is():
    """--non-interactive must actually suppress the routing question. If the
    wizard ever prompts on this path, an agent's shell would hang or answer
    the server question by accident (the docker-present default is '1',
    i.e. provision here)."""
    assert wizard.is_interactive(types.SimpleNamespace(isatty=lambda: False)) is False
    # A TTY-less stream is how every agent's subprocess looks.
    assert wizard.is_interactive(types.SimpleNamespace()) is False


def _frontmatter() -> str:
    text = _skill_text()
    assert text.startswith("---\n"), "SKILL.md must open with YAML frontmatter"
    return text[4:text.index("\n---\n", 3)]


def test_skill_frontmatter_matches_the_agentskills_spec():
    """Constraints from the agentskills.io specification, which every
    destination validates on submission."""
    front = _frontmatter()
    name = re.search(r"^name: (.+)$", front, re.M)
    assert name, f"missing name field:\n{front}"
    value = name.group(1).strip()
    assert len(value) <= 64, "name exceeds the spec's 64-character limit"
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", value), (
        f"name {value!r} must be lowercase alphanumerics and single hyphens, "
        "with no leading, trailing, or consecutive hyphen"
    )
    # Spec: "Must match the parent directory name."
    assert value == SKILL.parent.name, (
        f"frontmatter name {value!r} must equal its directory "
        f"{SKILL.parent.name!r} -- a mismatch is a validation failure at "
        "submission and changes the invoked command name"
    )
    desc = re.search(r"^description: (.+)$", front, re.M)
    assert desc, f"missing description field:\n{front}"
    assert len(desc.group(1)) <= 1024, "description exceeds the spec's 1024-char limit"
    # Description drives discovery/matching; an empty or stub one makes the
    # skill unfindable and is the most common review rejection.
    assert len(desc.group(1)) > 60, "description too short to match user intent"


def test_skill_frontmatter_uses_only_portable_spec_fields():
    """PORTABILITY GATE. Claude Code accepts ~20 extra frontmatter fields
    (when_to_use, argument-hint, disable-model-invocation, ...), but claude.ai
    uploads, the Skills API and package_skill.py accept ONLY the spec's six and
    fail with a HARD ERROR on anything else:

        Unexpected key(s) in SKILL.md frontmatter: argument-hint.

    This skill is published to multiple destinations, so a Claude-Code-only
    convenience field would silently cost every other destination. Keeping to
    the six loses nothing: Claude Code accepts all of them.
    """
    allowed = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    keys = set(re.findall(r"^([A-Za-z][A-Za-z0-9_-]*):", _frontmatter(), re.M))
    extra = sorted(keys - allowed)
    assert not extra, (
        f"non-portable frontmatter field(s): {extra}. Allowed: {sorted(allowed)}"
    )


def test_skill_body_stays_within_the_recommended_budget():
    """The spec recommends <500 lines / <5000 tokens; name+description of every
    installed skill is loaded at startup, and the body on invocation."""
    lines = _skill_text().splitlines()
    assert len(lines) < 500, f"SKILL.md is {len(lines)} lines; spec recommends under 500"


def test_skill_does_not_instruct_privacy_decisions_on_the_users_behalf():
    """docdex/maildex source selection is human-only by product design
    (docs/MCP-TOOLS.md: 'a privacy decision, so the tool is absent, not
    guarded'). The skill must keep saying so rather than drifting into
    telling agents to pick folders or mailboxes."""
    text = _skill_text()
    assert "Out of scope" in text
    assert "privacy decision" in text
    assert "firekeep docdex add" in text and "firekeep maildex add" in text


# --- plugin packaging -------------------------------------------------------
#
# Layout rules from the Claude Code plugin reference: plugin.json lives in the
# plugin's `.claude-plugin/`, every other directory (skills/, commands/, ...)
# sits at the PLUGIN ROOT, and marketplace.json lives at the REPOSITORY root.
# That last one is why this marketplace is installable as
# `/plugin marketplace add kapella-hub/FirekeepHQ` with no checkout -- the
# retired symdex plugin buried its marketplace.json in a subdirectory, so its
# published install line required cloning a repo whose own pyproject excluded
# that directory from the package.

import json  # noqa: E402


def test_plugin_manifest_is_valid_and_declares_its_name():
    manifest = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
    assert manifest.exists(), f"missing plugin manifest: {manifest}"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    # `name` is the only required field per the plugin reference.
    assert data.get("name"), "plugin.json must declare a name"
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", data["name"]), data["name"]


def test_agent_plugin_manifest_is_portable_and_reuses_the_install_skill():
    """One root Agent Plugins manifest is the compatibility floor for Kiro,
    Cursor, and other conforming clients. Keep it closed to the standard's
    fields so one vendor-specific convenience cannot invalidate every other
    destination."""
    manifest = PLUGIN_ROOT / "plugin.json"
    assert manifest.exists(), f"missing Agent Plugins manifest: {manifest}"
    data = json.loads(manifest.read_text(encoding="utf-8"))

    assert data["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert data["name"] == "firekeep"
    assert re.fullmatch(r"\d+\.\d+\.\d+", data["version"]), data["version"]
    assert data["description"]
    assert data["author"].get("name")
    assert data["keywords"]
    assert data["license"] == "BUSL-1.1"

    allowed = {
        "$schema", "name", "version", "description", "author", "homepage",
        "repository", "license", "keywords", "extensions",
    }
    assert not (set(data) - allowed), f"non-portable manifest fields: {set(data) - allowed}"
    assert (PLUGIN_ROOT / "skills" / "install-firekeep" / "SKILL.md").is_file()


def test_plugin_versions_match_across_portable_and_claude_manifests():
    """Both clients cache explicit versions, so a release must bump the two
    manifests together. The marketplace entry deliberately has no third copy:
    Claude treats plugin.json as authoritative when both declare a version."""
    claude_plugin = json.loads(
        (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    portable_plugin = json.loads(
        (PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8")
    )
    marketplace = json.loads(
        (PLUGIN_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    assert claude_plugin["version"] == portable_plugin["version"]
    assert "version" not in marketplace["plugins"][0]


def test_marketplace_manifest_is_at_the_repo_root_and_points_at_the_plugin():
    marketplace = REPO_ROOT / ".claude-plugin" / "marketplace.json"
    assert marketplace.exists(), (
        "marketplace.json must be at the REPOSITORY root, or "
        "`/plugin marketplace add <owner>/<repo>` cannot find it"
    )
    data = json.loads(marketplace.read_text(encoding="utf-8"))
    for required in ("name", "owner", "plugins"):
        assert required in data, f"marketplace.json missing required key: {required}"
    assert data["owner"].get("name"), "marketplace owner requires a name"
    assert data["plugins"], "marketplace declares no plugins"
    entry = data["plugins"][0]
    assert entry.get("name") and entry.get("source"), "plugin entry needs name + source"
    # The source must actually resolve to the plugin we ship.
    resolved = (REPO_ROOT / entry["source"]).resolve()
    assert resolved == PLUGIN_ROOT.resolve(), (
        f"marketplace source {entry['source']!r} resolves to {resolved}, "
        f"not the plugin root {PLUGIN_ROOT}"
    )


def test_skills_live_at_the_plugin_root_not_inside_dot_claude_plugin():
    """Documented layout rule: only plugin.json goes in `.claude-plugin/`."""
    assert (PLUGIN_ROOT / "skills").is_dir()
    assert not (PLUGIN_ROOT / ".claude-plugin" / "skills").exists(), (
        "skills/ must be at the plugin root, not inside .claude-plugin/"
    )


# --- the retired symdex plugin stays retired --------------------------------
#
# Mirrors test_bash_layer_retired.py / test_twin_removed.py: deleting something
# is only half the job; the other half is that no live surface still tells a
# user to use it. This one had teeth -- symdex/README.md is the PyPI long
# description for the published firekeep-symdex wheel, so its
# `/plugin marketplace add .../claude-plugin` line was a public instruction
# pointing at a directory that symdex/pyproject.toml excludes from both wheel
# and sdist.

RETIRED_PLUGIN = REPO_ROOT / "symdex" / "claude-plugin"


def test_retired_symdex_plugin_directory_is_gone():
    assert not RETIRED_PLUGIN.exists(), (
        f"{RETIRED_PLUGIN} is back. It shipped a SessionStart hook that could "
        "only print 'ACTION REQUIRED: call index_folder' and never index "
        "anything; client/firekeep_client/symdexindex.py replaced it."
    )


def test_no_live_surface_still_advertises_the_retired_plugin():
    """Historical records under docs/superpowers/** are excluded by
    construction -- they are design archaeology, not instructions."""
    live = [
        REPO_ROOT / "symdex" / "README.md",
        REPO_ROOT / "README.md",
        REPO_ROOT / "CLAUDE.md",
        REPO_ROOT / "client" / "README.md",
    ]
    offenders = []
    for path in live:
        if not path.exists():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "claude-plugin" in line and "firekeep-plugin" not in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{i}: {line.strip()[:90]}")
    assert not offenders, (
        "live docs still point at the retired symdex plugin:\n" + "\n".join(offenders)
    )


def test_site_copy_matches_the_repo_copy_when_the_site_checkout_is_present():
    """firekeep.ai serves this skill at /install-firekeep/SKILL.md so agents
    without a plugin system can curl it straight into ~/.claude/skills/. That
    is a SECOND copy in a SEPARATE repo with a manual deploy -- exactly the
    shape that drifts.

    Skipped when the site checkout is absent (CI, a contributor's clone); it
    runs on the machine where deploys actually happen, which is where a
    mismatch would otherwise ship silently.
    """
    site_copy = REPO_ROOT.parent / "firekeep-site" / "install-firekeep" / "SKILL.md"
    if not site_copy.exists():
        pytest.skip("firekeep-site checkout not present beside this repo")
    assert site_copy.read_text(encoding="utf-8") == _skill_text(), (
        f"{site_copy} has drifted from the repo copy. Re-copy it before "
        "deploying the site, or firekeep.ai serves a different skill than the "
        "plugin ships."
    )
