"""`firekeep` CLI — install, connect, doctor, update, and dormancy controls.

Stdlib-only (plus firekeep_client submodules). Never imports mcp/httpx.
Native runtime config is written once at install and refreshed idempotently.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import ssl
import stat
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from firekeep_client import (
    __version__,
    dexes,
    pathenv,
    resolver,
    serverinit,
    state,
    updater,
    wizard,
)
from firekeep_client.adapters import get_adapter
from firekeep_client.adapters.base import (
    RENDERED_GENERIC_INSTRUCTIONS_HASH,
    RENDERED_INSTRUCTIONS_HASH,
    has_marked_begin,
    read_rendered_instructions_hash,
    rendered_block_stamp,
    rendered_instructions_path,
)
from firekeep_client.adapters.codex import mcp_block_is_current
from firekeep_client.transport import get_json, TransportError


def _config_path() -> Path:
    """Path to ~/.firekeep/config, overridable via env FIREKEEP_CONFIG (tests)."""
    env = os.environ.get("FIREKEEP_CONFIG")
    return (Path(env) if env else resolver.CONFIG_PATH).expanduser().resolve()


# --- retired profile command -------------------------------------------------

def cmd_profile_removed(args) -> int:
    path = _config_path().expanduser().resolve()
    print(
        "firekeep: `firekeep profile` was removed — there is now exactly one "
        "server connection.\n"
        f"Your config was migrated to [server]; see {path}.\n"
        "To point this machine at a different server: run `firekeep join <code>`, "
        "or set FIREKEEP_CONFIG=<path> to use a separate config file.",
        file=sys.stderr,
    )
    return 2


# --- version (skew status added in Task 28) ----------------------------------

def cmd_version(args) -> int:
    print(f"firekeep-client {__version__}")
    try:
        cfg = resolver.load_config(_config_path())
    except resolver.ConfigMigrationConflict as exc:
        print(f"firekeep: {exc}", file=sys.stderr)
        return 3
    except resolver.ConfigError:
        print("skew: no config (run firekeep install)")
        return 0
    _, _, detail = _check_versions(cfg)
    print(detail)
    return 0


# --- install: venv, pip, ~/.firekeep bootstrap, adapter render -----------------

# `configured = false` is the sentinel that makes "never set up" a REPRESENTABLE
# state. Without it this skeleton is a complete, syntactically valid [server]
# block pointing at 127.0.0.1: resolver.resolve() succeeds, health checks run,
# and a machine that was never connected to anything is indistinguishable from a
# deliberate localhost deployment. `agent_id` had a CHANGEME sentinel for exactly
# this reason and the server connection had none, which is why doctor could only
# report four socket errors instead of "you have no server".
#
# It clears itself: config_write.upsert_server replaces the whole [server]
# section, so the first successful join/connect drops the key with no second
# place to remember to unset it.
_CONFIG_SKELETON = """\
[identity]
agent_id = CHANGEME

[server]
kind = ports
scheme = http
host = 127.0.0.1
verify_tls = false
api_key =
configured = false
"""


def _firekeep_home() -> Path:
    return _config_path().parent


def _venv_bin(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def _venv_python(venv: Path) -> Path:
    return _venv_bin(venv) / ("python.exe" if os.name == "nt" else "python")


# --- side-by-side venv layout (client 0.1.35) --------------------------------
# ~/.firekeep/venvs/<version>/ holds one full venv per installed client version,
# provisioned AT that final path and never moved (a uv venv is not relocatable:
# pyvenv.cfg and every console-script interpreter line bake the absolute path —
# the recorded 0.1.26 failure in client/bootstrap/install.sh). ~/.firekeep/current
# is the alias every rendered surface routes through: a junction on Windows
# (works without admin, unlike a directory symlink) and a symlink on POSIX.
# Updating = provision the new venvs/<V> beside whatever is running, flip
# `current`, re-render. Live sessions keep executing the old version untouched —
# their open handles pin the real files, not the link — which is what retires
# the "close every agent session" requirement the in-place rebuild imposed.
# ~/.firekeep/venv (legacy, pre-0.1.35) is left alone while held and GC'd by a
# later update's bootstrap.

VENVS_DIR_NAME = "venvs"
CURRENT_LINK_NAME = "current"


def _current_link(home: Path | None = None) -> Path:
    return (home if home is not None else _firekeep_home()) / CURRENT_LINK_NAME


def _venv_root(home: Path | None = None) -> Path:
    """The venv path rendered surfaces should reference and doctor should inspect.

    Prefers the `current` link; falls back to the legacy single venv so a
    pre-0.1.35 install (or a hand-built `pip install -e client` into one) keeps
    working until its first side-by-side update.
    """
    home = home if home is not None else _firekeep_home()
    current = home / CURRENT_LINK_NAME
    # lexists, not exists: exists() FOLLOWS the link, so a dangling `current`
    # (crashed GC, hand-deleted venvs/<V>) would silently fall back to the
    # legacy path — and a re-render would then rewrite every adapter, hook and
    # launcher against a ~/.firekeep/venv that never existed on this machine,
    # exiting 0. A dangling alias must stay authoritative: rendering through it
    # keeps the configs correct for the moment the link is repaired, and
    # doctor's current-link row is what names the dangle.
    if os.path.lexists(current) or current.exists():
        return current
    return home / "venv"


def _point_current(home: Path, venv: Path) -> None:
    """Create or retarget the `current` alias to point at `venv`.

    Windows: junction, recreated via os.rmdir + _winapi.CreateJunction. os.rmdir
    removes the LINK NODE only — never use a recursive delete on a junction; a
    traversal that follows the reparse point deletes the target venv's files.
    The two-step flip leaves a millisecond window where `current` is absent; a
    spawn in that window fails file-not-found and the retry succeeds (hooks fail
    open by design). POSIX: symlink swapped with os.replace — atomic rename(2),
    no window at all.
    """
    current = home / CURRENT_LINK_NAME
    venv = venv.resolve()
    if os.name == "nt":
        import _winapi
        # lexists sees the LINK NODE. A DANGLING junction reports False for
        # both is_symlink() and exists() (probe-confirmed on this repo's own
        # box), so guarding on those skips the rmdir and CreateJunction dies
        # with WinError 183 — making the exact broken state doctor sends users
        # here to repair the one state this function cannot repair.
        if os.path.lexists(current) or current.is_symlink() or current.exists():
            os.rmdir(current)  # removes the junction node, not the target
        _winapi.CreateJunction(str(venv), str(current))
        return
    tmp = home / f".current.tmp.{os.getpid()}"
    if os.path.lexists(tmp):
        tmp.unlink()
    tmp.symlink_to(venv)
    os.replace(tmp, current)


def _kit_version(kit: Path) -> str:
    """The version a checkout install provisions venvs/<version> under.

    Parsed textually rather than with tomllib: checkout installs support system
    Pythons back to 3.10, and tomllib arrived in 3.11.
    """
    text = (kit / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    if match is None:
        raise RuntimeError(f"no version field in {kit / 'pyproject.toml'}")
    return match.group(1)


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no", "off")


# Bounded, generous, env-overridable (Global Constraints: never a silent hang;
# but pip installs legitimately take minutes on slow links).
_INSTALL_TIMEOUT = float(os.environ.get("FIREKEEP_INSTALL_TIMEOUT", "600"))

# Provisioning a SERVER is a different order of magnitude from installing a
# wheel, and sharing _INSTALL_TIMEOUT with it was a real defect, not a rough
# edge: `firekeep init` wrapped the whole of install.sh in 600s while that
# script pulls four service images, starts thirteen containers, and warms
# ~3.3GB of Ollama models -- ten to twenty minutes on a fresh VPS with a fast
# link. The documented happy path therefore timed out on success, printing
# "firekeep init timed out" over a stack that was still legitimately working.
#
# One hour, still bounded (a hung install must never wait forever), and
# separately overridable. FIREKEEP_INSTALL_TIMEOUT stays honoured as a floor so
# anyone who already raised it for slow links keeps that behaviour.
_SERVER_INSTALL_TIMEOUT = max(
    float(os.environ.get("FIREKEEP_SERVER_INSTALL_TIMEOUT", "3600")),
    _INSTALL_TIMEOUT,
)


def _run(cmd, **kwargs) -> None:
    kwargs.setdefault("timeout", _INSTALL_TIMEOUT)
    subprocess.run(list(cmd), check=True, **kwargs)


def _venv_has_pip(venv: Path) -> bool:
    python = _venv_python(venv)
    if not python.exists():
        return False
    try:
        subprocess.run([str(python), "-m", "pip", "--version"],
                       check=True, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _create_venv(venv: Path) -> None:
    if _venv_bin(venv).exists():
        if _venv_has_pip(venv):
            return
        # A pip-less venv is a half-built bootstrap leftover: uv venvs ship no pip, so a
        # bootstrap that died between `uv venv` and `uv pip install` leaves exactly this.
        # Short-circuiting here used to crash the checkout install later in _pip_install;
        # rebuild in place instead (--clear reprovisions, pip included via ensurepip).
        _run([sys.executable, "-m", "venv", "--clear", str(venv)])
        return
    _run([sys.executable, "-m", "venv", str(venv)])


def _pip_install(python: Path, *pkgs, find_links=None) -> None:
    cmd = [str(python), "-m", "pip", "install"]
    if find_links is not None:
        cmd += ["--find-links", str(find_links)]
    cmd += list(pkgs)
    _run(cmd)


def _generic_is_configured() -> bool:
    """Whether the user opted a generic MCP client in (`[generic] agents_md`).

    Delegates to the resolver's RAW read — never resolver.load_config(), which
    migrates and rewrites the config when `[server]` is absent. This runs on
    every install and every uninstall; asking a question must not edit the file."""
    return resolver.generic_agents_md() is not None


def _selected_runtimes(runtime: str, *, include_generic: bool = False) -> list[str]:
    """PURE: a function of its arguments only, never of the config on disk.

    `generic` joins the "all" fan-out only when the caller says so, so an
    unconfigured user gets exactly the four — the invariant test_cli_install and
    test_cli_uninstall pin by count."""
    if runtime == "all":
        return ["claude", "codex", "kiro", "opencode"] + (["generic"] if include_generic else [])
    return [runtime]


def _kit_dir() -> Path | None:
    # client/ — the dir holding pyproject.toml for the local firekeep-client kit.
    # cli.py lives at client/firekeep_client/cli.py, so parent.parent is client/.
    #
    # None once the package is INSTALLED: parent.parent is then site-packages/, which has
    # no pyproject.toml. Handing that to pip is what made the documented
    # `firekeep install --runtime claude` (run from the venv, to re-render one runtime) die
    # with "Directory '.../site-packages' is not installable". The presence of pyproject.toml
    # is the honest test for "am I running from the unpacked kit or from site-packages".
    kit = Path(__file__).resolve().parent.parent
    return kit if (kit / "pyproject.toml").is_file() else None


def _bootstrap_home(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    (home / ".gitignore").write_text("*\n", encoding="utf-8")  # never commit the key
    cfg = _config_path()
    if not cfg.exists():  # idempotent: never clobber an existing config
        cfg.write_text(_CONFIG_SKELETON, encoding="utf-8")


def _apply_flags(cfg, args) -> bool:
    """Non-interactive path: land --agent-id / --host in the config directly.
    Returns True if anything was set (so the caller knows to write)."""
    if not cfg.has_section("identity"):
        cfg.add_section("identity")
    if not cfg.has_section("server"):
        cfg.add_section("server")
    touched = False
    if getattr(args, "agent_id", None):
        cfg.set("identity", "agent_id", args.agent_id)
        touched = True
    if getattr(args, "host", None):
        cfg.set("server", "kind", "ports")
        cfg.set("server", "scheme", "http")
        cfg.set("server", "verify_tls", "false")
        cfg.set("server", "host", args.host)
        cfg.remove_option("server", "base_url")
        cfg.remove_option("server", "ca_path")
        touched = True
    if getattr(args, "dist_base", None):
        wizard.set_dist_base(cfg, args.dist_base)
        touched = True
    return touched


def _configure(args) -> tuple[bool, wizard.Plan | None]:
    """Build ~/.firekeep/config, interactively when there's a human to ask.

    Returns (needs_edit, plan). `needs_edit` is True if the config still holds the
    CHANGEME placeholder afterwards — the caller prints the hand-edit NEXT STEPS only
    in that case. A teammate who answered the prompts should never be told to go edit
    the file they just filled in. `plan` is what the human asked for at the routing
    question, or None on the non-interactive path, where there was nobody to ask."""
    path = _config_path()
    cfg = resolver.load_config(path)
    interactive = wizard.is_interactive() and not getattr(args, "non_interactive", False)

    # A normal install prepares every shipped runtime. The user should not have to predict
    # which client they will use later, and selecting one here used to leave the others
    # looking broken. Explicit --runtime remains the targeted re-render/repair path.
    if getattr(args, "runtime", None) is None:
        args.runtime = "all"

    plan: wizard.Plan | None = None
    if interactive:
        print("firekeep: configuring ~/.firekeep/config (Enter accepts the [default])")
        plan = wizard.prompt_config(
            cfg,
            agent_id=getattr(args, "agent_id", None),
            host=getattr(args, "host", None),
        )
        if getattr(args, "dist_base", None):
            wizard.set_dist_base(cfg, args.dist_base)
        changed = True
    else:
        changed = _apply_flags(cfg, args)
        # No human to route, so the skeleton's `configured = false` stands unless
        # a flag supplied a real connection. That is the honest record of a
        # headless install: the kit is here, nothing is connected yet, and
        # doctor will say exactly that instead of reporting four socket errors.
        if getattr(args, "host", None):
            cfg.remove_option("server", wizard.UNCONFIGURED_MARKER)

    # ORDERING IS LOAD-BEARING: this must land BEFORE the render loop, which
    # builds `generic` from the persisted path (get_adapter takes no argument).
    # Persist afterwards and the first `--agents-md` run renders print-only and
    # drops the flag. Written through the same cfg round trip, so [server] and
    # [identity] survive.
    if getattr(args, "runtime", None) == "generic" and getattr(args, "agents_md", None):
        if not cfg.has_section("generic"):
            cfg.add_section("generic")
        cfg.set("generic", "agents_md", str(Path(args.agents_md).expanduser().resolve()))
        changed = True

    if changed:
        with open(path, "w", encoding="utf-8") as handle:
            cfg.write(handle)

    needs_edit = (
        cfg.get("identity", "agent_id", fallback="").strip() == wizard.PLACEHOLDER_AGENT_ID
    )
    return needs_edit, plan


def cmd_install(args) -> int:
    # argparse cannot express "--agents-md only with --runtime generic", so the
    # check is manual — and it happens FIRST, before anything is created: a flag
    # we would otherwise ignore must fail visibly, not leave the user believing
    # a rules file is being managed.
    if getattr(args, "agents_md", None) and getattr(args, "runtime", None) != "generic":
        print("firekeep: --agents-md is only valid with --runtime generic", file=sys.stderr)
        return 2

    # Fail-loud per step: a teammate's FIRST command must never dump a raw
    # traceback or hang unbounded (the <5 min onboarding promise).
    step = "bootstrap ~/.firekeep"
    join_code = getattr(args, "join", None) or os.environ.get("FIREKEEP_JOIN", "").strip()
    join_result = 0
    try:
        # Seed the dex registry FIRST — before _bootstrap_home, and the ordering
        # is load-bearing, not stylistic. _bootstrap_home writes a config
        # SKELETON that already carries a [server] section, so a migration
        # running after it would read every fresh machine as an existing
        # install, grandfather symdex, and the opt-in this registry exists for
        # would never once happen. Never raises; asks nothing (ROADMAP §5: no
        # new install-time questions).
        dexes.ensure_migrated(installing=True)

        home = _firekeep_home()
        _bootstrap_home(home)

        # Ask BEFORE the venv/pip work: a teammate should not sit through a multi-minute
        # pip install only to then be asked who they are.
        step = "configure ~/.firekeep/config"
        if join_code:
            # The code carries every answer. A TTY must not re-enable prompts.
            args.non_interactive = True
        needs_edit, plan = _configure(args)
        # A code typed at the routing question is the same thing as one passed
        # with --join; from here on there is exactly one join path.
        if plan is not None and plan.action == wizard.JOIN_WITH_CODE and plan.join_code:
            join_code = plan.join_code
        # The wizard's generic answer becomes config here — BEFORE the render
        # loop, which builds the generic adapter from exactly this value. The
        # wizard itself deliberately performs nothing.
        if plan is not None and plan.generic_agents_md:
            resolver.set_generic_agents_md(plan.generic_agents_md)

        step = "create venv"
        # kit resolved BEFORE the venv step: when kit is None the process is EXECUTING
        # from the installed venv (the bootstrap's wizard hand-off, or a documented
        # re-render), and with no kit dir there is nothing to reinstall afterwards —
        # so never create or rebuild here. The pip-less-venv rebuild in _create_venv
        # exists for half-built CHECKOUT installs; run against the bootstrap's uv venv
        # (which ships no pip BY DESIGN) it wiped the very install it belonged to,
        # leaving a bare pip-only venv (release-breaking bug found live in the 0.1.2
        # bootstrap acceptance, 2026-07-13). The venv's existence is self-evident when
        # we are running from it — and its PATH is derived from sys.executable, not
        # assumed, because side-by-side layouts put it at venvs/<version>, legacy
        # installs at venv/, and this code must never guess wrong about which venv
        # it is standing in.
        kit = _kit_dir()
        if kit is not None:
            # Checkout install: provision a fresh versioned venv at its FINAL path
            # (never moved — venvs aren't relocatable) and flip `current` to it below.
            venv = home / VENVS_DIR_NAME / _kit_version(kit)
            _create_venv(venv)
        else:
            venv = Path(sys.executable).resolve().parent.parent
        python = _venv_python(venv)

        step = "pip install firekeep-client"
        # Install the LOCAL kit directory, never the bare "firekeep-client" name
        # — that name is owned by a third party on PyPI, so resolving it
        # there would silently install foreign code into a teammate's venv.
        if kit is None:
            # Running from the installed venv (`firekeep install --runtime claude`), not from
            # the unpacked kit: the code IS the installed code, so there is nothing to
            # install. Re-render the adapters and say so — don't pretend an upgrade happened.
            print("firekeep: running from the installed venv — skipping pip "
                  "(re-rendering adapters only; to UPGRADE, unpack a newer kit "
                  "and run ./install from it)")
        else:
            _pip_install(python, str(kit))

        # Symdex is an always-on client MCP server (like firekeep-decision). From a
        # CHECKOUT, install its sibling source dir BY LOCAL PATH — never
        # `pip install firekeep-symdex` (that name may belong to a third party on PyPI,
        # the same hazard as firekeep-client). RELEASE installs receive the
        # checksum-verified symdex wheel from the bootstrap, so there is nothing to do
        # here when running from the installed venv (kit is None).
        if kit is not None:
            step = "pip install symdex (local checkout dir)"
            symdex_dir = kit.parent / "symdex"
            if not (symdex_dir / "pyproject.toml").is_file():
                raise RuntimeError(
                    f"symdex source not found at {symdex_dir} — incomplete checkout"
                )
            _pip_install(python, str(symdex_dir))

        step = "lock down config permissions"
        state._private(_config_path())  # lock down the key-bearing config

        step = "select this version (current link)"
        if kit is not None:
            _point_current(home, venv)

        step = "render runtime adapters"
        # Rendered surfaces route through the `current` alias, never the
        # versioned dir: that is what makes future updates render-free (the
        # embedded paths stay literally identical across flips) and keeps
        # runtime configs from pinning a venv that GC will remove.
        venv_bin = _venv_bin(_venv_root(home))
        for name in _selected_runtimes(args.runtime, include_generic=_generic_is_configured()):
            step = f"render {name} adapter"
            get_adapter(name).render(venv_bin=venv_bin)

        # Put a `firekeep` launcher on PATH (best-effort — a PATH failure must NEVER
        # fail the install; it is a convenience, not a dependency). --no-modify-path
        # or FIREKEEP_NO_MODIFY_PATH opts out (sysadmins who manage PATH centrally).
        step = "add firekeep to PATH"
        path_msgs: list[str] = []
        if getattr(args, "no_modify_path", False) or _truthy_env("FIREKEEP_NO_MODIFY_PATH"):
            path_msgs = ["skipped PATH setup (--no-modify-path); the `firekeep` command "
                         f"is at {venv_bin / 'firekeep'}"]
        else:
            try:
                path_msgs = pathenv.ensure_on_path(home, venv_bin)
            except Exception as exc:  # noqa: BLE001 — best-effort; never fail the install
                path_msgs = [f"could not modify PATH ({exc}); add "
                             f"{home / pathenv.SHIM_DIR_NAME} to PATH manually"]

        if join_code:
            step = "join Firekeep server"
            from firekeep_client.join import join
            join_result = join(join_code, agent_id=getattr(args, "agent_id", None))
            needs_edit = False
    except resolver.ConfigMigrationConflict as exc:
        print(f"firekeep: install stopped: {exc}", file=sys.stderr)
        return 3
    except subprocess.TimeoutExpired:
        print(f"firekeep: install failed at '{step}': timed out after "
              f"{_INSTALL_TIMEOUT:.0f}s (override with FIREKEEP_INSTALL_TIMEOUT)",
              file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - installer surface, fail loud not raw
        print(f"firekeep: install failed at '{step}': {exc}", file=sys.stderr)
        return 1

    print(f"firekeep: installed into {home}")
    registered = "firekeep (gateway: memory, sessions, relay, monitoring, code, decisions)"
    print(f"firekeep: registered MCP servers: {registered}")
    print("firekeep: tip — `/personal` (Claude) or `firekeep personal` toggles a private "
          "session where Firekeep is fully bypassed (nothing logged/recalled); it "
          "auto-clears at session end.")
    for msg in path_msgs:
        print(f"firekeep: {msg}")
    if join_result:
        return join_result

    # The routing answer becomes an ACTION here, not advice. "Set one up on this
    # machine" used to be something the user had to discover was spelled
    # `firekeep init` -- a command named by no output anywhere in the product.
    if plan is not None and plan.action == wizard.PROVISION_HERE:
        print("\nfirekeep: setting up the server on this machine "
              "(`firekeep init`) — this takes a few minutes.\n")
        return cmd_init(_init_args_for_self_provision(args))

    if needs_edit:
        print("firekeep: NEXT STEPS — edit ~/.firekeep/config: set agent_id (currently "
              "CHANGEME) and complete the [server] connection values. "
              "Open a new terminal (or `source` your shell rc), then run `firekeep doctor`. "
              "Config changes apply on next agent start.")
    elif plan is not None and plan.action == wizard.DECIDE_LATER:
        # Deliberately NOT "run firekeep doctor" alone. Doctor is a diagnosis, and
        # this user already knows the diagnosis -- they chose it. Give them the
        # three commands so the answer is on screen when they come back.
        print("firekeep: NEXT STEPS — the client kit is installed, but it is not "
              "connected to a server yet. When you are ready:\n"
              "  • Run one here:      firekeep init\n"
              "  • Join your team's:  firekeep join <code>\n"
              "  • Over SSH:          firekeep connect <user@host>\n"
              "`firekeep doctor` will repeat this until one of them is done.")
    else:
        print("firekeep: NEXT STEPS — open a new terminal (or `source` your shell rc), "
              "then run `firekeep doctor`. Config changes apply on next agent start.")
    # Discovery: nothing else in the product tells a Cursor/Windsurf/Zed user
    # that a runtime for them exists.
    hint = _generic_hint()
    if hint is not None:
        print(hint)
    return 0


def _init_args_for_self_provision(args):
    """Build the argparse-shaped object cmd_init expects.

    A namespace rather than re-entering the parser: this is an internal hand-off
    with no command line to parse, and constructing it explicitly keeps every
    field cmd_init reads visible in one place instead of depending on parser
    defaults that a future flag could quietly change.
    """
    return types.SimpleNamespace(
        server_dir=None,
        version=None,
        dist_base=getattr(args, "dist_base", None),
        pull=False,
        office=False,
        # Carried through so the identity answered at the prompt survives
        # enrolment; see _finish_server_provision for what happens without it.
        agent_id=getattr(args, "agent_id", None),
        # Chosen at an interactive prompt, so the box is one the human is sitting
        # at: enrol it and hand them the line for their next machine.
        self_enroll=True,
    )


# --- uninstall: reverse the render, strip PATH, delete ~/.firekeep ------------

def _confirm(prompt: str, *, assume_yes: bool) -> bool:
    """A y/N gate. `assume_yes` (--yes) approves without asking; a session with no
    human on the other end declines rather than block a script on input()."""
    if assume_yes:
        return True
    if not wizard.is_interactive():
        return False
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _remove_current_link(home: Path) -> str | None:
    """Remove the `current` alias NODE alone, never recursing through it.

    Windows: `current` is a junction; a recursive delete that follows the reparse
    point would delete the TARGET venv's files (the hazard _point_current guards).
    os.rmdir removes only the link node. POSIX: unlink the symlink. Returns an
    error string on failure, else None."""
    current = home / CURRENT_LINK_NAME
    if not (os.path.lexists(current) or current.is_symlink() or current.exists()):
        return None
    try:
        if os.name == "nt":
            os.rmdir(current)  # junction/dir-link node only, not the target
        else:
            current.unlink()
        return None
    except OSError as exc:
        return f"{current}: {exc}"


def _remove_home(home: Path) -> tuple[list[str], list[str]]:
    """Delete ~/.firekeep, removing the `current` junction node FIRST so the tree
    delete never follows it into the target venv. Returns (removed, failed)."""
    removed: list[str] = []
    failed: list[str] = []
    if not (home.exists() or os.path.lexists(home)):
        return removed, failed
    err = _remove_current_link(home)
    if err is not None:
        # Refuse the recursive delete rather than risk following a live junction.
        failed.append(
            f"could not remove the `current` alias ({err}); left {home} in place "
            f"so a recursive delete never follows the junction into a venv"
        )
        return removed, failed
    try:
        shutil.rmtree(home)
        removed.append(str(home))
    except OSError as exc:
        failed.append(f"{home}: {exc}")
    return removed, failed


def _teardown_server(server_dir: Path) -> tuple[list[str], list[str]]:
    """`docker compose down -v` on the managed bundle. DELETES ALL DATA. Never
    raises: reports what happened. If docker is absent, names the manual command
    and continues. Returns (removed, failed)."""
    removed: list[str] = []
    failed: list[str] = []
    compose = server_dir / "docker-compose.yml"
    docker = shutil.which("docker")
    if docker is None:
        failed.append(
            "docker not found — the server stack was NOT torn down. Remove it by "
            f"hand:\n      cd {server_dir} && docker compose down -v   # deletes all data"
        )
        return removed, failed
    try:
        subprocess.run(
            [docker, "compose", "-f", str(compose), "down", "-v"],
            cwd=str(server_dir), check=True, timeout=_INSTALL_TIMEOUT,
        )
        removed.append(f"server stack + data volumes (docker compose down -v in {server_dir})")
    except (OSError, subprocess.SubprocessError) as exc:
        failed.append(
            f"docker compose down -v failed ({exc}); the stack may still be running. "
            f"Finish by hand: cd {server_dir} && docker compose down -v"
        )
    return removed, failed


def _generic_orphan_warning(home: Path) -> str | None:
    """A line to print when the config MENTIONS `[generic]` but no usable path
    could be read from it — a corrupt or hand-mangled config.

    Without this, uninstall skips generic silently, deletes ~/.firekeep, and the
    block sits in the user's rules file with the record of its location gone."""
    try:
        text = (home / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if "[generic]" not in text:
        return None
    return ("a [generic] section is present but unreadable, so the generic runtime "
            "was skipped — if a Firekeep instruction block remains in your MCP "
            "client's rules file, remove it by hand")


def cmd_uninstall(args) -> int:
    """Reverse `firekeep install`: unrender every adapter, strip the PATH entry,
    then delete ~/.firekeep. Server teardown is OPT-IN and DESTRUCTIVE (--server or
    an explicit second confirm). Never raises on a partial failure — reports what
    was and was not removed."""
    home = _firekeep_home()
    assume_yes = getattr(args, "yes", False)
    server_dir = home / "server"
    server_present = (server_dir / "docker-compose.yml").is_file()
    shim_dir = home / pathenv.SHIM_DIR_NAME

    # Resolve the generic target BEFORE anything is removed. Its block lives in a
    # file outside ~/.firekeep, and the only record of WHICH file is inside
    # ~/.firekeep — which this command deletes at the end. Read it late and a
    # config we could not parse leaves the block stranded with no way to find it.
    generic_target = resolver.generic_agents_md()
    generic_orphan = _generic_orphan_warning(home) if generic_target is None else None
    runtimes = _selected_runtimes("all", include_generic=generic_target is not None)

    # Say exactly what will be removed BEFORE touching anything.
    print("firekeep uninstall will remove:")
    print(f"  - the Firekeep MCP + hook blocks from every runtime config "
          f"({', '.join(runtimes)}); your own settings are left intact")
    print(f"  - the `firekeep` launcher and its PATH entry ({shim_dir})")
    print(f"  - {home} (venvs, config, shims, bin, logs, server bundle, snapshots)")
    if server_present:
        print(f"  - a managed server bundle exists at {server_dir}; its running stack "
              "and DATA are left untouched unless you opt in below")

    # Client-removal confirm.
    if not _confirm("Proceed? [y/N] ", assume_yes=assume_yes):
        print("firekeep uninstall: aborted — nothing was removed.")
        return 0

    # Server teardown gate: OPT-IN (--server, or an explicit second confirm) and
    # guarded by its OWN loud data-loss confirm, distinct from the client confirm
    # above. Runs BEFORE home is deleted — the compose file lives inside it. A bare
    # `--yes` removes the client but never opts into data loss.
    teardown = False
    if server_present:
        opted_in = getattr(args, "server", False) or _confirm(
            f"Also tear down the server stack at {server_dir}? [y/N] ", assume_yes=False,
        )
        if opted_in:
            print("\n  WARNING: `docker compose down -v` DELETES ALL SERVER DATA — the "
                  "Neo4j graph, Qdrant vectors and Redis state are gone for good, no undo.")
            teardown = _confirm("  Type y to permanently delete all server data: ",
                                assume_yes=assume_yes)

    removed: list[str] = []
    failed: list[str] = []
    kept: list[str] = []

    if teardown:
        r, f = _teardown_server(server_dir)
        removed += r
        failed += f
    elif server_present:
        kept.append(f"the server stack and its data at {server_dir} "
                    "(re-run with --server to remove it)")

    # Adapters + PATH FIRST: they edit files OUTSIDE ~/.firekeep, so they must run
    # before the home (and the venv they reference) is gone. One runtime's failure
    # must never abort the rest.
    for name in runtimes:
        try:
            get_adapter(name).unrender()
            removed.append(f"{name} runtime config (Firekeep block removed)")
        except Exception as exc:  # noqa: BLE001 — best-effort per runtime
            failed.append(f"{name} adapter unrender: {exc}")
            if name == "generic" and generic_target is not None:
                # Name the file: after ~/.firekeep is gone this line is the only
                # remaining record of where the block is.
                failed.append(
                    f"the Firekeep instruction block in {generic_target} — remove it "
                    "by hand (the config recording this path is being deleted)")
    if generic_orphan is not None:
        kept.append(generic_orphan)

    try:
        for msg in pathenv.remove_from_path(home):
            removed.append(msg)
    except Exception as exc:  # noqa: BLE001 — PATH cleanup is best-effort, never fatal
        failed.append(f"PATH cleanup: {exc}")

    # Delete ~/.firekeep last (junction-safe — see _remove_home).
    r, f = _remove_home(home)
    removed += r
    failed += f

    print("\nfirekeep uninstall:")
    for msg in removed:
        print(f"  removed: {msg}")
    for msg in kept:
        print(f"  kept: {msg}")
    for msg in failed:
        print(f"  NOT removed: {msg}", file=sys.stderr)
    if failed:
        print("\nfirekeep: some items could not be removed (above); nothing else was "
              "touched. Re-run after resolving them.", file=sys.stderr)
        return 1
    print("\nfirekeep: uninstalled. Open a new terminal to drop the stale PATH entry.")
    return 0


# --- doctor: fail-loud preflight (health, skew, venv, perms, CA) -----------

def _check_health(cfg) -> list[tuple[str, str, str]]:
    # name == bare service id (cli._check_health's own test asserts
    # {svc for svc, _, _ in results} == set(resolver.SERVICES) -- no prefix).
    #
    # Also catches OSError: transport._build_ssl_context() runs OUTSIDE
    # transport._request()'s own try/except, so a malformed/expired ca_path
    # (very plausible -- this is exactly the file _check_ca_expiry validates
    # separately) raises a raw ssl.SSLError (an OSError subclass), not
    # TransportError. A doctor check must never let that escape uncaught and
    # crash the whole preflight before later checks get to run.
    out = []
    for svc in resolver.SERVICES:
        try:
            ep = resolver.resolve(svc, cfg=cfg)
            get_json(f"{ep.rest_base}/health", headers=ep.headers, verify=ep.verify)
            out.append((svc, "ok", ep.rest_base))
        except (TransportError, resolver.ConfigError, OSError) as exc:
            out.append((svc, "fail", f"{_ep_url(svc, cfg)}: {exc}"))
    return out


def _no_server_at_all(health: list[tuple[str, str, str]]) -> bool:
    """True when EVERY service failed at the connection layer.

    One service down is an outage. All of them refusing at the socket is not an
    outage — it is "there is no server here", and that is a completely different
    sentence to say to a user.
    """
    if not health or any(status != "fail" for _, status, _ in health):
        return False
    # Connection-layer signatures across platforms and transports. An HTTP
    # status (4xx/5xx) means something ANSWERED, which is not this case.
    markers = (
        "connection refused", "actively refused", "no route to host",
        "name or service not known", "nodename nor servname",
        "temporary failure in name resolution", "getaddrinfo",
        "network is unreachable", "timed out", "connection reset",
        "no address associated",
    )
    return all(
        any(marker in detail.lower() for marker in markers)
        for _, _, detail in health
    )


def _check_server_connection(cfg, health: list[tuple[str, str, str]]):
    """The row that says WHERE YOU ARE, not merely what failed.

    This exists because the only text in the entire diagnostic that named
    `firekeep join` / `firekeep connect` lived inside `_check_api_key`, whose
    guidance branch is reached only after a SUCCESSFUL HTTP round-trip that
    returns 401/403. With no server, the request dies at the socket, the branch
    is skipped, and the advice was structurally suppressed in precisely the
    situation it was written for — a first install. What the user saw instead
    was four identical [FAIL] rows whose entire content was an OS socket error.

    Runs FIRST and reports once, so the four rows below it read as detail rather
    than as four separate problems.
    """
    if not _no_server_at_all(health):
        return None
    try:
        host = cfg.get("server", "host", fallback="").strip()
    except Exception:  # noqa: BLE001 — a malformed config is other checks' row
        host = ""
    local = host in ("", "127.0.0.1", "localhost", "::1", "[::1]")
    where = (
        "This machine has a Firekeep client but no server to talk to."
        if local
        else f"No Firekeep server is answering at {host}."
    )
    return (
        "server", "fail",
        f"{where} Pick the one that describes you:\n"
        "      • Run the server on THIS machine:      firekeep init\n"
        "      • Join a server your team already has: firekeep join <code>\n"
        "        (get a code from Dashboard -> Devices -> Add device)\n"
        "      • Set one up over SSH on another box:  firekeep connect <user@host>\n"
        "      The four rows below are the same fact, once per service.",
    )


def _ep_url(svc, cfg) -> str:
    try:
        return resolver.resolve(svc, cfg=cfg).rest_base
    except resolver.ConfigError:
        return svc


def _check_versions(cfg) -> tuple[str, str, str]:
    """Report the client and cortex versions. Deliberately does NOT judge them.

    This used to be `version-skew`, and it returned "ok" only when
    `server == __version__`. That is structurally unsatisfiable: the client ships
    on `client-v*` tags (currently 0.1.23) and the server on `v[0-9]+.[0-9]+.[0-9]+`
    (see .github/workflows/release.yml vs server-release.yml) — two independently
    released artifacts on different tag series. Equality is not merely unlikely,
    it is meaningless, so EVERY correct install emitted `version-skew: warn`.
    A row that is always wrong is worse than no row: it teaches the reader to
    skip the whole report.

    Judging real incompatibility would need a declared protocol/compat version,
    which exists nowhere in this codebase; inventing one spans four MCP services,
    shim.py, five hook cores and doctor — the "new subsystem" docs/STRATEGY.md
    freezes. Until such a declaration exists, this reports and the reader judges.

    The ACTIONABLE half is already covered, against an authority that actually
    exists: `_check_client_version` compares the kit to the release manifest and
    tells you to run `firekeep update`. This row is for support ("what are you
    running?"), not for verdicts.

    Unreachable stays a warn: that is a real, checkable condition, unlike skew.
    """
    try:
        ep = resolver.resolve("cortex", cfg=cfg)
        data = get_json(f"{ep.rest_base}/version", headers=ep.headers, verify=ep.verify)
        server = data.get("version") if isinstance(data, dict) else None
        if not server:
            return ("versions", "warn", f"client {__version__}, cortex reported no version")
        return ("versions", "ok", f"client {__version__}, cortex {server}")
    except (TransportError, resolver.ConfigError, OSError) as exc:
        # OSError: see _check_health's comment -- an unverifiable ca_path
        # raises ssl.SSLError, not TransportError. Unreachable, not fatal.
        return ("versions", "warn", f"cortex /version unreachable: {exc}")


def _check_embeddings(cfg) -> tuple[str, str, str] | None:
    """Report the gap between "the stack is up" and "your memories are findable".

    Since install.sh stopped blocking on the ~3.3GB model pull, a freshly
    installed server is genuinely usable while embeddings are still warming —
    and in that window `memory_learn` returns HTTP 200 with status="partial",
    stores the memory, queues it for backfill, and does NOT make it recallable.
    That is a successful-looking write with a surprising consequence, which is
    precisely the kind of state that has to be named somewhere the user already
    looks rather than discovered later by wondering why recall is empty.

    WARN, never FAIL: nothing is broken and nothing needs doing. Silent when the
    server does not report the field at all, so an older server produces no row
    rather than a scary unknown.
    """
    try:
        ep = resolver.resolve("cortex", cfg=cfg)
        payload = get_json(f"{ep.rest_base}/health", headers=ep.headers, verify=ep.verify)
    except (TransportError, resolver.ConfigError, OSError):
        return None  # unreachable is _check_health's row, not ours
    if not isinstance(payload, dict):
        return None
    row = (payload.get("services") or {}).get("embeddings")
    if not isinstance(row, dict):
        return None
    status = str(row.get("status", ""))
    detail = str(row.get("detail", "") or "")
    if status == "connected":
        return ("embeddings", "ok", detail or "ready")
    return (
        "embeddings", "warn",
        f"still warming ({detail or 'model not loaded yet'}) — memories you write "
        "now are STORED and queued for backfill, but not searchable until this "
        "finishes. Nothing to do; watch it with: docker compose logs -f ollama-pull",
    )


def _check_agent_id(cfg) -> tuple[str, str, str] | None:
    # The installer skeleton (_CONFIG_SKELETON above) writes agent_id =
    # CHANGEME. If a teammate never edits it, every memory
    # write / session / replay event silently attributes to "CHANGEME" —
    # cheap to catch here, expensive to untangle after the fact. Report the
    # EFFECTIVE identity (resolver.agent_id() applies the FIREKEEP_AGENT_ID env
    # override), not the raw config text, so an env-based override that
    # already fixes a stale CHANGEME doesn't get flagged.
    try:
        value = resolver.agent_id(cfg)
    except resolver.ConfigError as exc:
        return ("agent-id", "warn", str(exc))
    if value == "CHANGEME":
        return (
            "agent-id", "warn",
            "[identity] agent_id is still the installer default 'CHANGEME' -- "
            "set a real identity in ~/.firekeep/config (or export "
            "FIREKEEP_AGENT_ID) or your work will be attributed to 'CHANGEME'",
        )
    return ("agent-id", "ok", f"agent_id={value}")


def _check_api_key(cfg, label: str = "api-key") -> tuple[str, str, str] | None:
    """Warn when the configured server requires a missing API key.

    THE FALSE-GREEN TRAP this closes: /health and Cortex /version are on the
    auth middleware's skip list, so under AUTH_ENABLED=true a keyless connection
    passes every health/skew check above while EVERY real MCP/REST call 401s.
    Doctor must not report a broken config as healthy.
    """
    try:
        scheme = cfg.get("server", "scheme", fallback="http").strip().lower()
    except resolver.ConfigError as exc:
        return (label, "warn", str(exc))
    if scheme != "https":
        # `http` does not imply "no auth expected". A common self-hosted shape binds
        # the server to loopback and lets the client
        # reaches it over an SSH tunnel as http://127.0.0.1, and AUTH_ENABLED is
        # true. A keyless connection then passes every check above (health/version are
        # auth-exempt) while every real call 401s -- the same FALSE-GREEN TRAP this
        # function was written to close, just reached over http instead of https.
        #
        # So don't infer auth-expectation from the scheme. If there is no key, ASK
        # the server whether it enforces auth, using a gated read-only route.
        if cfg.get("server", "api_key", fallback="").strip():
            return None
        try:
            ep = resolver.resolve("cortex", cfg=cfg)
        except resolver.ConfigError:
            return None
        try:
            get_json(f"{ep.rest_base}/vault/secrets", headers=ep.headers, verify=ep.verify)
        except TransportError as exc:
            if getattr(exc, "status", None) in (401, 403):
                return (
                    label, "fail",
                    f"[server] has no api_key but {ep.rest_base} enforces auth "
                    "-- health/version are auth-exempt, so the checks above pass while "
                    "every real MCP call 401s. Enroll with `firekeep join <code>` "
                    "from Dashboard -> Devices, or run `firekeep connect "
                    "<user@host>` to issue one over SSH",
                )
        except OSError:
            pass          # unreachable is _check_health's row to report, not ours
        return None
    key = cfg.get("server", "api_key", fallback="").strip()
    if not key:
        return (
            label, "warn",
            "[server] uses https (auth expected) but api_key is "
            "empty -- health/version are auth-exempt so the checks above can "
            "pass while every real MCP call 401s; run `firekeep join <code>`",
        )
    return (label, "ok", "api_key configured (redacted)")


def _check_venv_scripts(venv: Path, is_windows: bool | None = None) -> tuple[str, str, str]:
    if is_windows is None:
        is_windows = os.name == "nt"
    bindir = venv / ("Scripts" if is_windows else "bin")
    ext = ".exe" if is_windows else ""
    wanted = (
        "firekeep", "firekeep-shim", "firekeep-sidecar",
        "firekeep-decision", "firekeep-symdex",
    )
    missing = [n for n in wanted if not (bindir / f"{n}{ext}").exists()]
    if missing:
        # Partial-venv detection: bin dir present, python interpreter never
        # landed -- an interrupted provisioning. Under the side-by-side layout
        # the release bootstrap repairs this itself (its fast path re-provisions
        # an unhealthy venvs/<V> with --clear), so the advice is a re-run; only
        # a legacy or checkout install needs the manual delete, because
        # `firekeep install` skips venv creation once the bin dir exists.
        if venv.exists() and not (bindir / f"python{ext}").exists():
            if venv.name == CURRENT_LINK_NAME:
                repair = ("re-run the release installer or `firekeep update` "
                          "-- it reprovisions the broken versioned venv")
            else:
                repair = (f"a rerun of `firekeep install` will NOT repair this -- "
                          f"it skips venv creation once the bin dir exists; delete "
                          f"{venv} and rerun `firekeep install`")
            return (
                "venv-scripts", "fail",
                f"partial venv at {venv}: python interpreter never landed in "
                f"{bindir} ({repair}); also missing: {', '.join(missing)}",
            )
        return ("venv-scripts", "fail", f"missing in {bindir}: {', '.join(missing)}")
    return ("venv-scripts", "ok", str(bindir))


def _check_dexes() -> tuple[str, str, str]:
    """Which domain indexes this machine has registered.

    "ok" whether or not any are: a dex is a suggestion, never a default
    (ROADMAP §5), so the empty state is an OFFER, not a finding. The one fault
    this row can report is a REGISTERED dex whose wheel is gone — the gateway
    mounts a backend that cannot start, and the only symptom the user sees is
    tools that quietly stopped existing.

    Deliberately says nothing about _check_venv_scripts' wanted list, which is
    unchanged: the wheels are always installed and checksum-verified, and
    registration gates mounting only.
    """
    registered = dexes.registered()
    if not registered:
        return ("dexes", "ok",
                "none registered — add code intelligence with `firekeep dex add symdex`")
    names = ", ".join(m.name for m in registered)
    missing = [m.name for m in registered if not dexes.is_installed(m)]
    if missing:
        return ("dexes", "warn",
                f"{names} (registered) — but no wheel for {', '.join(missing)} in this "
                f"venv; re-run the installer, or `firekeep dex remove {missing[0]}`")
    return ("dexes", "ok", f"{names} (registered)")


def _check_current_link(home: Path | None = None) -> tuple[str, str, str] | None:
    """Health of the `current` alias under the side-by-side layout.

    None on a legacy install (no venvs/ dir and no link) — a pre-0.1.35 layout
    is not a fault, it just hasn't updated yet. Once either exists, a dangling
    or mispointed link is exactly the failure doctor must name: every rendered
    surface routes through it, so a bad link is a dead client that looks
    installed.
    """
    home = home if home is not None else _firekeep_home()
    current = home / CURRENT_LINK_NAME
    venvs = home / VENVS_DIR_NAME
    # os.path.lexists sees the LINK NODE itself. This matters on Windows: a
    # DANGLING junction reports False for both is_symlink() and exists()
    # (probe-verified), so without lexists a dangling alias whose venvs/ dir is
    # also gone would silently read as "pure legacy, nothing to check" — the
    # one state where every rendered surface is dead and doctor says nothing.
    link_node_exists = os.path.lexists(current) or current.is_symlink() or current.exists()
    if not link_node_exists:
        if not venvs.is_dir():
            return None  # legacy layout — nothing to check
        return ("current-link", "fail",
                f"{current} missing while {venvs} exists; re-run the installer "
                f"(irm/curl per docs) or `firekeep update` to repoint it")
    try:
        target = current.resolve(strict=True)
    except OSError:
        return ("current-link", "fail",
                f"{current} is dangling (its target venv was removed); re-run "
                f"the installer or `firekeep update`")
    installed = __version__
    if target.name != installed:
        return ("current-link", "warn",
                f"{current} -> {target} but this client is {installed}; a new "
                f"session will run {target.name} (fine mid-update, stale otherwise)")
    return ("current-link", "ok", f"{current} -> {target}")


def _check_codex_adapter(venv: Path) -> list[tuple[str, str, str]]:
    """codex-mcp only. The codex-instructions row this used to emit (an exact
    containment check on the rendered block) is subsumed by _check_instructions,
    which reports hash-based staleness for EVERY runtime — codex included."""
    config = Path.home() / ".codex" / "config.toml"
    instructions = Path.home() / ".codex" / "AGENTS.md"
    repair = "run `firekeep install --runtime codex`"

    instruction_text = ""
    try:
        if instructions.exists():
            instruction_text = instructions.read_text(encoding="utf-8")
    except OSError:
        pass  # an unreadable AGENTS.md is _check_instructions' row to report
    # Prefix match, line-anchored: the 0.1.41 stamped begin line and the legacy
    # unstamped one must both count as "codex is managed here" — but prose that
    # merely mentions the marker mid-line must not.
    has_instruction_block = has_marked_begin(instruction_text)

    if not config.exists():
        if not has_instruction_block:
            return []
        return [("codex-mcp", "fail",
                 f"{config} missing while Firekeep instructions exist; {repair}")]

    try:
        config_text = config.read_text(encoding="utf-8")
    except OSError as exc:
        if not has_instruction_block:
            return []
        return [("codex-mcp", "fail", f"cannot read {config}: {exc}; {repair}")]

    is_managed = (
        "[mcp_servers.firekeep]" in config_text
        or "firekeep-client (managed" in config_text
        or has_instruction_block
    )
    if not is_managed:
        return []

    if not mcp_block_is_current(config_text, _venv_bin(venv)):
        return [("codex-mcp", "fail", f"stale or missing Firekeep gateway in {config}; {repair}")]
    return [("codex-mcp", "ok", str(config))]


# The shipped runtimes, in adapter order (`_selected_runtimes("all")`), plus
# generic — which contributes a row ONLY when the user configured it, so an
# unconfigured user's doctor output is unchanged.
_INSTRUCTION_RUNTIMES = ("claude", "codex", "kiro", "opencode", "generic")


def _expected_instructions_hash(runtime: str) -> str:
    """The hash THIS runtime's block should carry. Generic renders the hook-free
    text, so checking it against the four's hash would report a correct file as
    'edited' forever."""
    if runtime == "generic":
        return RENDERED_GENERIC_INSTRUCTIONS_HASH
    return RENDERED_INSTRUCTIONS_HASH


def _generic_hint() -> str | None:
    """One line pointing a user on an unsupported MCP client at the generic
    runtime — the only discovery path for someone who never runs the wizard.
    None once `[generic]` exists: they already know."""
    if _generic_is_configured():
        return None
    return ("Using another MCP client (Cursor, Windsurf, Gemini CLI, …)? "
            "`firekeep install --runtime generic --agents-md <path>`")


def _check_runtime_instructions(runtime: str) -> tuple[str, str, str] | None:
    """One instruction-staleness row: on-disk block hash vs this wheel's hash.

    States (named in the detail): ok (current), stale (an intact older render —
    the stamp matches the on-disk content, or the block predates stamping),
    edited (on-disk content contradicts its own stamp), absent (runtime present,
    no block). None when the runtime itself shows no trace on this machine —
    doctor must not warn a claude-only user about three runtimes they never
    installed."""
    path = rendered_instructions_path(runtime)
    if path is None:
        return None  # unknown runtime, or generic never opted into
    name = f"{runtime}-instructions"
    repair = f"run `firekeep install --runtime {runtime}`"
    if runtime == "generic":
        # The presence gate splits here. For the four, "no trace on disk" means
        # a runtime this user never installed — silence is right. For generic,
        # `[generic] agents_md` IS the user telling us they installed it, so a
        # vanished target is a BROKEN state to report, not an absence to hide.
        repair = f"run `firekeep install --runtime generic --agents-md {path}`"
        try:
            missing = not path.exists()
        except OSError:
            missing = True
        if missing:
            return (name, "warn",
                    f"target {path} is missing — {repair}")
    else:
        # Presence evidence: the runtime's config root. For kiro the rendered
        # file lives one level down (~/.kiro/steering/), so the root is ~/.kiro.
        root = path.parent.parent if runtime == "kiro" else path.parent
        try:
            if not root.exists():
                return None
        except OSError:
            return None
    expected = _expected_instructions_hash(runtime)
    on_disk = read_rendered_instructions_hash(runtime)
    if on_disk is None:
        return (name, "warn",
                f"absent — no Firekeep instruction block in {path}; {repair}")
    if on_disk == expected:
        return (name, "ok", f"current (h={on_disk}) in {path}")
    stamp = None
    if runtime != "kiro":  # kiro's steering doc is whole-file, no stamped marker
        try:
            stamp = rendered_block_stamp(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            stamp = None
    if stamp is not None and stamp != on_disk:
        return (name, "warn",
                f"edited — on-disk block h={on_disk} contradicts its own stamp "
                f"h={stamp} in {path}; {repair}")
    return (name, "warn",
            f"stale — on-disk block h={on_disk}, this wheel renders "
            f"h={expected} in {path}; {repair}")


def _check_instructions() -> list[tuple[str, str, str]]:
    """Per-runtime instruction-staleness rows (round-2 measurement contract).

    Generalizes the codex-only containment check that used to live in
    _check_codex_adapter: same question ("does the on-disk block match this
    wheel?"), asked of every runtime, answered by content hash rather than
    string containment — the same basis the X-Firekeep-Instr-* headers report."""
    rows: list[tuple[str, str, str]] = []
    for runtime in _INSTRUCTION_RUNTIMES:
        try:
            row = _check_runtime_instructions(runtime)
        except Exception:  # noqa: BLE001 — one runtime's failure must not mask the rest
            row = (f"{runtime}-instructions", "warn",
                   f"could not inspect the rendered instruction file; "
                   f"run `firekeep install --runtime {runtime}`")
        if row is not None:
            rows.append(row)
    return rows


_WIN_BROAD_PRINCIPALS = ("Everyone", "BUILTIN\\Users", "Authenticated Users", "Users:")


def _check_config_perms(config: Path, is_windows: bool | None = None) -> tuple[str, str, str]:
    if is_windows is None:
        is_windows = os.name == "nt"
    if not config.exists():
        return ("config-perms", "fail", f"{config} missing")
    if is_windows:
        try:
            out = subprocess.run(
                ["icacls", str(config)],
                capture_output=True, text=True, check=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            return ("config-perms", "warn", f"icacls failed: {exc}")
        broad = [p for p in _WIN_BROAD_PRINCIPALS if p in out]
        if broad:
            return ("config-perms", "warn", f"{config} grants access to {broad}")
        return ("config-perms", "ok", str(config))
    mode = stat.S_IMODE(os.stat(config).st_mode)
    if mode & 0o077:
        return ("config-perms", "warn", f"{config} mode {oct(mode)} is group/world-accessible")
    return ("config-perms", "ok", f"mode {oct(mode)}")


def _cert_not_after(path: Path) -> datetime:
    info = ssl._ssl._test_decode_cert(str(path))  # stdlib X.509 decode
    seconds = ssl.cert_time_to_seconds(info["notAfter"])
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _check_ca_expiry(cfg, label: str = "ca-expiry") -> tuple[str, str, str] | None:
    if not cfg.has_section("server"):
        return None
    section = cfg["server"]
    ca_path = section.get("ca_path")
    verify_tls = section.get("verify_tls", "false").strip().lower() == "true"
    if not ca_path or not verify_tls:
        return None  # plaintext: no CA to check
    if ca_path.strip().lower() == resolver.OS_TRUST:
        # OS trust store: the operating system owns rotation/expiry; no file to parse.
        return (label, "ok", "OS trust store (ca_path = os; no CA file to expire)")
    path = Path(os.path.expanduser(ca_path))
    if not path.exists():
        return (label, "fail", f"ca_path {path} missing")
    try:
        not_after = _cert_not_after(path)
    except Exception as exc:  # malformed cert is a hard, loud failure
        return (label, "fail", f"cannot parse {path}: {exc}")
    remaining = (not_after - datetime.now(timezone.utc)).days
    if remaining < 0:
        return (label, "fail", f"CA expired {abs(remaining)}d ago ({path})")
    if remaining < 30:
        return (label, "warn", f"CA expires in {remaining}d ({path})")
    return (label, "ok", f"CA valid {remaining}d ({path})")


def _check_credential_expiry(cfg) -> tuple[str, str, str] | None:
    if not cfg.has_section("server"):
        return None
    value = cfg.get("server", "credential_expires_at", fallback="").strip()
    if not value:
        return None
    try:
        expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
    except ValueError:
        return ("credential-expiry", "fail", f"invalid credential_expires_at: {value}")
    seconds = (expires - datetime.now(timezone.utc)).total_seconds()
    if seconds < 0:
        days = max(0, int(abs(seconds) // 86400))
        return ("credential-expiry", "fail", f"credential expired {days}d ago — rejoin this device")
    days = int(seconds // 86400)
    if seconds < 14 * 86400:
        return (
            "credential-expiry",
            "warn",
            f"credential expires in {days}d — ask an admin for a regenerated join code",
        )
    return ("credential-expiry", "ok", f"credential valid for {days}d")


def _check_retired_profile_env() -> tuple[str, str, str] | None:
    value = os.environ.get("FIREKEEP_PROFILE", "").strip()
    if not value:
        return None
    return (
        "retired-profile-env",
        "warn",
        f"FIREKEEP_PROFILE={value} is ignored; remove it from this shell and your "
        f"shell startup files. The connection comes from {_config_path().expanduser().resolve()}.",
    )


def _check_client_version(cfg) -> tuple[str, str, str] | None:
    """Compare the installed kit against the release manifest.

    Returns None for a checkout install (no [dist] section) — that developer never used the
    bootstrap and has nothing to update from. Never 'fail': a stale-but-working client is a
    nudge, not an outage.

    This is the ONLY version check that renders a verdict, and it can because it compares
    the kit against an authority that exists (the release manifest). Its former sibling
    compared client to CORTEX — two independently released artifacts — which conflated 'my
    client is old' with 'the server moved' and could never return ok; that row is now the
    verdict-free `_check_versions` report.
    """
    try:
        base = updater.dist_base(cfg)
    except updater.UpdateError:
        return None
    try:
        manifest = updater.fetch_manifest(base)
        # is_newer -> parse_version raises on a malformed version, and fetch_manifest only
        # checks that `version` is a str. Leaving this call outside the block would let a bad
        # release deploy crash the WHOLE doctor run — killing the agent-id, api-key, venv and
        # CA checks below it, which is exactly the isolation run_doctor promises.
        stale = updater.is_newer(manifest.version, __version__)
    except updater.UpdateError as exc:
        return ("client-version", "warn", f"cannot check for updates: {exc}")
    if stale:
        return ("client-version", "warn",
                f"client {__version__}, latest {manifest.version} — run `firekeep update`")
    return ("client-version", "ok", f"client {__version__} is current")


def run_doctor(cfg=None) -> list[tuple[str, str, str]]:
    from firekeep_client.join import sweep_pending
    sweep_pending(_config_path())
    if cfg is None:
        cfg = resolver.load_config(_config_path())
    results: list[tuple[str, str, str]] = []
    # Every check function below is self-contained: it catches its own
    # ConfigError/TransportError/OSError and returns a tuple rather than
    # raising, so one check's failure can never mask or short-circuit the
    # rest -- doctor always runs the full suite and reports everything.
    # Health runs first but is REPORTED second: _check_server_connection reads
    # its results to distinguish "no server exists" from "a service is down",
    # and that distinction belongs at the top of the output, not buried under
    # four socket errors.
    health = _check_health(cfg)
    no_server = _check_server_connection(cfg, health)
    if no_server is not None:
        results.append(no_server)
    results.extend(health)
    results.append(_check_versions(cfg))
    # Skipped entirely when nothing is reachable: the routing row above already
    # said the one useful thing, and adding "embeddings unknown" under it would
    # be noise on top of a diagnosis.
    if no_server is None:
        embeddings = _check_embeddings(cfg)
        if embeddings is not None:
            results.append(embeddings)
    client_version = _check_client_version(cfg)
    if client_version is not None:
        results.append(client_version)
    agent_id_result = _check_agent_id(cfg)
    if agent_id_result is not None:
        results.append(agent_id_result)
    api_key_result = _check_api_key(cfg)
    if api_key_result is not None:
        results.append(api_key_result)
    current_link = _check_current_link()
    if current_link is not None:
        results.append(current_link)
    results.append(_check_venv_scripts(_venv_root()))
    results.append(_check_dexes())
    results.extend(_check_codex_adapter(_venv_root()))
    results.extend(_check_instructions())
    results.append(_check_config_perms(_config_path()))
    ca = _check_ca_expiry(cfg)
    if ca is not None:
        results.append(ca)
    credential_expiry = _check_credential_expiry(cfg)
    if credential_expiry is not None:
        results.append(credential_expiry)
    results.append(_check_personal_mode())
    retired_env = _check_retired_profile_env()
    if retired_env is not None:
        results.append(retired_env)
    return results


def cmd_restore(args) -> int:
    """Browse or restore local snapshots of uncommitted work.

    The READ side of worktree_snapshot, and not optional polish: a snapshot store nobody
    can read from is write-only machinery, which this repo has deleted features for
    before (the corpus entity graph after "0 entities ever extracted"; ~161K BACKLINK
    edges never traversed). Snapshots are local-only and never leave the machine.
    """
    from pathlib import Path as _Path

    from firekeep_client import worktree_snapshot as ws

    root = ws.repo_root()
    if root is None:
        print("firekeep restore: not inside a git repository", file=sys.stderr)
        return 1

    snaps = ws.list_snapshots(root)
    if args.apply:
        if not any(s.get("id") == args.apply for s in snaps):
            print(f"firekeep restore: no snapshot '{args.apply}' for {root.name}",
                  file=sys.stderr)
            return 1
        res = ws.apply_snapshot(ws.snapshot_path(root, args.apply), root)
        for err in res["errors"]:
            print(f"  ! {err}", file=sys.stderr)
        print(f"firekeep restore: {res['restored']} file(s) restored from {args.apply}")
        if res.get("backup"):
            print(f"  current state snapshotted first: {_Path(res['backup']).name}")
        if res.get("deleted_not_restored"):
            print("  reported, NOT re-deleted: "
                  f"{', '.join(res['deleted_not_restored'])}")
        return 1 if res["errors"] else 0

    if not snaps:
        print(f"firekeep restore: no snapshots for {root.name}")
        return 0
    print(f"snapshots for {root.name} (newest last):")
    for s in snaps:
        print(f"  {s.get('id')}  {s.get('created_at', '')}  "
              f"{s.get('files_copied', 0)} file(s)  {s.get('reason', '')}")
        if s.get("truncated"):
            print(f"      TRUNCATED: {s['truncated']}")
    return 0


def cmd_night_shift(args) -> int:
    """Drain distill_session Relay tasks with a LOCAL model (LM Studio or Ollama).

    The stop hook enqueues one per session end; this worker turns each into a
    consolidated memory + (when warranted) a DRAFT skill for human review —
    attributed to the original session, on zero-marginal-cost local compute.
    Run it manually, or schedule it (launchd/cron) for actual night shifts."""
    from firekeep_client import nightshift

    out = nightshift.run(max_tasks=args.max, dry_run=args.dry_run)
    if out.get("error"):
        print(f"firekeep night-shift: {out['error']}", file=sys.stderr)
        return 1
    mode = " (dry-run — nothing written)" if args.dry_run else ""
    print(f"firekeep night-shift{mode}: {out['distilled']} distilled, "
          f"{out['legacy']} legacy cleared, {out['skipped']} skipped, "
          f"{out['failed']} failed")
    if out["distilled"] and not args.dry_run:
        print("firekeep night-shift: draft skills await review in the dashboard "
              "Skills tab; memories are live in recall.")
    return 0


def _dex_state(manifest, registry) -> str:
    """The one word `dex list` prints for a dex. Ordered by what a user needs to
    be told first: a registered dex whose wheel is gone is a BROKEN state (its
    backend will fail to start), not merely an unregistered one."""
    registered = manifest.name in registry
    if not dexes.is_installed(manifest):
        return "registered (wheel missing!)" if registered else "not installed"
    return "registered" if registered else "available"


def cmd_dex(args) -> int:
    """Manage dexes — the domain indexes the Keep understands.

    Registration gates ACTIVITY, not installation: the wheels arrive bundled and
    checksum-verified with every release either way, and this only decides
    whether the gateway mounts them and whether their background work runs.
    That is why `remove` never uninstalls anything and `add` never downloads
    anything — the only thing that changes is a line in ~/.firekeep/dexes.json.
    """
    action = getattr(args, "action", None) or "list"
    registry = dexes.read_registry()

    if action == "list":
        print("firekeep dexes — the domain indexes this Keep understands\n")
        for manifest in dexes.KNOWN_DEXES.values():
            print(f"  {manifest.name}  [{_dex_state(manifest, registry)}]  "
                  f"indexes {manifest.indexes}")
            print(f"      {manifest.description}")
        # Names in the file with no manifest here: a hand-edited entry, or a dex
        # from a newer client after a rollback. The gateway ignores them; a
        # `list` that hid them would be lying about the file it reports on.
        for name in sorted(set(registry) - set(dexes.KNOWN_DEXES)):
            print(f"  {name}  [unknown to this client — ignored]")
        if not registry:
            # The suggestion-not-default funnel (ROADMAP §5): absence is a
            # choice, so this is an offer, never a warning.
            print("\nfirekeep: none registered — add code intelligence with "
                  "`firekeep dex add symdex`")
        else:
            print("\nfirekeep: `firekeep dex add|remove <name>` changes this; it takes "
                  "effect on the next agent session.")
        return 0

    name = (getattr(args, "name", None) or "").strip()
    if not name:
        print(f"firekeep: `firekeep dex {action}` needs a dex name "
              f"({', '.join(dexes.KNOWN_DEXES)})", file=sys.stderr)
        return 2
    manifest = dexes.KNOWN_DEXES.get(name)
    if manifest is None:
        print(f"firekeep: unknown dex '{name}' — this client knows "
              f"{', '.join(dexes.KNOWN_DEXES)}", file=sys.stderr)
        return 1

    if action == "add":
        if name in registry:
            print(f"firekeep: {name} is already registered — nothing to do.")
            return 0
        # Prove the code is there BEFORE writing the entry. Registering a dex
        # whose wheel is absent trades a clear error now for a silent missing
        # tool next session, which is the harder failure to diagnose by far.
        if not dexes.is_installed(manifest):
            print(f"firekeep: cannot register {name} — its wheel is not in this venv "
                  f"(no module '{manifest.import_probe}').\n"
                  f"  Release install: re-run the installer "
                  f"(https://firekeep.ai/docs.html) to fetch the bundled, "
                  f"checksum-verified wheel.\n"
                  f"  From a checkout: `cd client && ./install`.",
                  file=sys.stderr)
            return 1
        dexes.add(name)
        print(f"firekeep: registered {name} — {manifest.title} will index "
              f"{manifest.indexes} for this Keep.\n"
              f"firekeep: takes effect on the next agent session.")
        return 0

    # remove — deliberately does NOT probe the wheel: the machine most likely to
    # need this is one whose dex is broken.
    if name not in registry:
        print(f"firekeep: {name} is not registered — nothing to do.")
        return 0
    dexes.remove(name)
    print(f"firekeep: removed {name} — {manifest.title} no longer runs, and nothing "
          f"here will index {manifest.indexes} any more.\n"
          f"firekeep: the wheel stays installed — `firekeep dex add {name}` brings it "
          f"back. Takes effect on the next agent session.")
    return 0


def cmd_personal(args) -> int:
    """Toggle Firekeep personal (bypass) mode for THIS session.

    While ON, Firekeep is dormant: the hooks no-op (no briefing / presence /
    pre-edit gate / session capture), and you should not use firekeep_* memory tools
    — nothing is logged or recalled. It auto-clears when the session ends (the
    `stop` hook removes the marker); a crash is covered by FIREKEEP_PERSONAL_TTL_HOURS.
    This flips a MARKER file (~/.firekeep/personal), never the config.
    """
    action = getattr(args, "action", None) or "toggle"
    if action == "on":
        resolver.set_personal(True)
    elif action == "off":
        resolver.set_personal(False)
    elif action == "toggle":  # flips the MARKER — the tier this CLI owns
        resolver.set_personal(not resolver.is_personal())
    # status: report only, change nothing.

    # Report the EFFECTIVE state, not just the marker: FIREKEEP_BYPASS is a separate tier
    # this CLI can't clear (a child can't unset the parent shell's env), so an honest
    # `off`/`status` must own up to it rather than falsely claim team mode.
    marker_on = resolver.is_personal()
    env_on = os.environ.get("FIREKEEP_BYPASS", "").strip().lower() not in ("", "0", "false", "no", "off")
    if marker_on:
        print("firekeep: personal mode ON — Firekeep bypassed for this session "
              "(auto-clears at session end). `firekeep personal off` rejoins team mode.")
    elif env_on:
        print("firekeep: personal mode ON via FIREKEEP_BYPASS (env) — unset FIREKEEP_BYPASS in "
              "your shell to rejoin team mode; `firekeep personal off` only clears the marker.")
    else:
        print("firekeep: personal mode OFF — team mode (Firekeep active).")
    return 0


def _check_personal_mode() -> tuple[str, str, str]:
    """Doctor row: surface an active bypass loudly so it's never silently left on."""
    if not resolver.is_bypassed():
        return ("personal-mode", "ok", "off (team mode)")
    env = os.environ.get("FIREKEEP_BYPASS", "").strip().lower()
    if env not in ("", "0", "false", "no", "off"):
        why = "FIREKEEP_BYPASS env set"
    else:
        why = "personal marker present (auto-clears at session end)"
    return ("personal-mode", "warn",
            f"ON — {why}; Firekeep bypassed, nothing logged/recalled")



def cmd_connect(args) -> int:
    """Thin entry point; the work lives in firekeep_client.connect (see its module
    docstring for why this command exists at all)."""
    from firekeep_client.connect import ConnectError, connect
    try:
        return connect(args.target, agent_id=args.agent_id,
                       remote_dir=args.remote_dir, use_tunnel=not args.no_tunnel)
    except ConnectError as exc:
        print(f"connect failed: {exc}", file=sys.stderr)
        return 1


def cmd_join(args) -> int:
    from firekeep_client.join import JoinError, join
    try:
        return join(
            args.code,
            agent_id=args.agent_id,
            force=args.force,
            print_key=args.print_key,
            resume=args.resume,
        )
    except JoinError as exc:
        print(f"join failed: {exc}", file=sys.stderr)
        return exc.exit_code


def _server_source_dir(explicit: str | None) -> Path | None:
    """Find a source checkout or an already-downloaded server bundle."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    else:
        candidates.append(Path.cwd())
        candidates.append(_firekeep_home() / "server")
        kit = _kit_dir()
        if kit is not None:
            candidates.append(kit.parent)

    for candidate in candidates:
        root = candidate.resolve()
        if all((root / name).is_file() for name in (
            "install.sh", "docker-compose.yml", ".env.example",
        )):
            return root
    return None


def _server_dist_base(explicit: str | None) -> str:
    if explicit:
        return explicit.rstrip("/")
    configured = os.environ.get("FIREKEEP_SERVER_DIST_BASE", "").strip()
    if configured:
        return configured.rstrip("/")
    path = _config_path()
    if path.is_file():
        try:
            cfg = resolver.load_config(path)
            if cfg.has_section("dist"):
                configured = cfg.get("dist", "base_url", fallback="").strip()
                if configured:
                    return configured.rstrip("/")
        except resolver.ConfigError:
            pass
    return serverinit.DEFAULT_DIST_BASE


def _refresh_stale_bundle(root: Path, args) -> Path | None:
    """When the managed ~/.firekeep/server cache is OLDER than server/latest,
    re-download latest and return the new root; else None (reuse the cache).

    Only ever called for the MANAGED cache (never a source checkout). A published
    bundle moving vX -> vY between a failed init and its retry silently reused the
    stale vX — a real dead-end (v0.4.4 kept after latest advanced to v0.4.5). A
    transient network failure NEVER blocks init: it warns and returns None so the
    cached bundle is reused."""
    cached = serverinit._bundle_version(root)
    if cached is None:
        return None  # unversioned/malformed marker — reuse rather than guess
    base = _server_dist_base(args.dist_base)
    try:
        latest = serverinit.fetch_manifest(base)
    except serverinit.ServerInitError as exc:
        print(f"firekeep init: could not check for a newer server bundle ({exc}); "
              f"reusing the cached bundle {cached}.", file=sys.stderr)
        return None
    try:
        # Both are vX.Y.Z tags; is_newer -> parse_version wants bare X.Y.Z.
        stale = updater.is_newer(latest.version.lstrip("v"), cached.lstrip("v"))
    except updater.UpdateError:
        return None  # unparseable version (prerelease tag) — reuse, never crash init
    if not stale:
        return None
    try:
        new_root = serverinit.download_bundle(
            base, root, version=latest.version, timeout=_INSTALL_TIMEOUT,
        )
    except (serverinit.ServerInitError, updater.UpdateError) as exc:
        print(f"firekeep init: refresh to {latest.version} failed ({exc}); "
              f"reusing the cached bundle {cached}.", file=sys.stderr)
        return None
    print(f"firekeep: refreshed server bundle {cached} -> {latest.version}")
    return new_root


def cmd_init(args) -> int:
    """Provision a server from a local checkout or the verified public bundle."""
    root = _server_source_dir(args.server_dir)
    downloaded = False

    # Stale managed-cache guard. When REUSING the ~/.firekeep/server cache (no
    # --server-dir source, no explicit --version) and the published server has
    # moved on, refresh to latest instead of reusing a superseded bundle. A source
    # CHECKOUT (has .git, or was passed via --server-dir) is NEVER auto-refreshed —
    # only the managed cache is.
    if root is not None and not args.server_dir and not args.version:
        managed = (_firekeep_home() / "server").resolve()
        is_managed_cache = (
            root == managed
            and (root / "SERVER_BUNDLE.json").is_file()
            and not (root / ".git").exists()
        )
        if is_managed_cache:
            refreshed = _refresh_stale_bundle(root, args)
            if refreshed is not None:
                root, downloaded = refreshed, True

    if root is not None and args.version and (root / "SERVER_BUNDLE.json").is_file():
        previous = serverinit.previous_bundle_path(root)
        try:
            root = serverinit.download_bundle(
                _server_dist_base(args.dist_base),
                root,
                version=args.version,
                timeout=_INSTALL_TIMEOUT,
            )
        except (serverinit.ServerInitError, updater.UpdateError) as exc:
            print(f"firekeep init: {exc}. Existing server bundle was preserved.", file=sys.stderr)
            return 2
        downloaded = True
        print(f"firekeep: verified server bundle {args.version} prepared at {root}")
        if previous.is_dir():
            print(f"firekeep: previous deployment files retained at {previous}")
    elif root is not None and args.version:
        print(
            "firekeep init: --version updates a published server bundle, not a "
            "source checkout. Omit --server-dir to use ~/.firekeep/server.",
            file=sys.stderr,
        )
        return 2
    if root is None:
        destination = (
            Path(args.server_dir).expanduser()
            if args.server_dir
            else _firekeep_home() / "server"
        )
        try:
            root = serverinit.download_bundle(
                _server_dist_base(args.dist_base),
                destination,
                version=args.version,
                timeout=_INSTALL_TIMEOUT,
            )
        except (serverinit.ServerInitError, updater.UpdateError) as exc:
            print(f"firekeep init: {exc}. Nothing was changed.", file=sys.stderr)
            return 2
        downloaded = True
        print(f"firekeep: verified server bundle downloaded to {root}")
    bash = shutil.which("bash")
    if not bash:
        print(
            "firekeep init needs bash to run the server installer. Install bash "
            "(or use WSL on Windows), then retry the same command.",
            file=sys.stderr,
        )
        return 2

    command = [bash, str(root / "install.sh")]
    if args.pull or downloaded or (root / "SERVER_BUNDLE.json").is_file():
        command.append("--pull")
    if args.office:
        command.append("--office")
    try:
        subprocess.run(command, cwd=root, check=True, timeout=_SERVER_INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(
            f"firekeep init timed out after {_SERVER_INSTALL_TIMEOUT / 60:.0f} minutes "
            "(override with FIREKEEP_SERVER_INSTALL_TIMEOUT, in seconds).\n"
            "The stack may still be starting. Check it before re-running:\n"
            f"  cd {root} && docker compose ps",
            file=sys.stderr,
        )
        return 1
    except subprocess.CalledProcessError as exc:
        print(
            f"firekeep init: server installer exited with status {exc.returncode}; "
            "its output above names the failed step.",
            file=sys.stderr,
        )
        return exc.returncode or 1
    except OSError as exc:
        print(f"firekeep init could not start the server installer: {exc}", file=sys.stderr)
        return 1
    return _finish_server_provision(root, bash, args)


def _mint_code(bash: str, root: Path, *flags: str) -> str:
    """Ask the server for a join code. Returns "" on any failure.

    Runs `deploy/firekeep-admin invite`, which mints through
    `docker compose exec cortex-api python -m app.enroll.mint` and therefore
    needs NO credential on the server box -- it is already inside the trust
    boundary. Failure is never fatal: the stack is up and every manual path
    still works, so this can only ever add convenience.
    """
    admin = root / "deploy" / "firekeep-admin"
    if not admin.is_file():
        return ""
    try:
        done = subprocess.run(
            [bash, str(admin), "invite", "--json", *flags],
            cwd=root, capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    try:
        import json as _json
        payload = _json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return ""
    code = payload.get("code", "") if isinstance(payload, dict) else ""
    return code if isinstance(code, str) and code.startswith("fk_join_") else ""


def _finish_server_provision(root: Path, bash: str, args) -> int:
    """Close the loop the install used to leave open.

    What this replaced:

        firekeep: server provisioned. Use Dashboard -> Devices -> Add device,
        then run `firekeep join <code>` on each client machine.

    Every word of that was true and it was unreachable. The dashboard binds to
    127.0.0.1 by default and its password lives in `dashboard/.htpasswd.cred`,
    so the prescribed next action required an SSH tunnel, a file read and a
    browser -- from the machine the user was already standing on, three minutes
    after installing a client kit on it.
    """
    if not getattr(args, "self_enroll", False):
        print("firekeep: server provisioned. Add devices from Dashboard -> Devices, "
              "or run: deploy/firekeep-admin invite --agent <name>")
        return 0

    print("\nfirekeep: enrolling this machine against the server it just built…")
    code = _mint_code(bash, root, "--local", "--agent", _default_device_label())
    if not code:
        # Explicitly NOT a failure of the install. Say what worked, what did not,
        # and the exact command to finish by hand.
        print(
            "firekeep: the server is running, but this machine could not enrol "
            "itself automatically.\n"
            "  Finish by hand:  cd " + str(root) + " && "
            "deploy/firekeep-admin invite --local --agent this-machine\n"
            "  then:            firekeep join <the code it prints>",
            file=sys.stderr,
        )
        return 0

    from firekeep_client.join import join
    # Pass the identity EXPLICITLY. join._agent_id ranks the server's
    # `suggested_agent_id` above the local config, which is right when a
    # teammate redeems an invite an admin named the device in -- and wrong here,
    # where the person answered "Agent identity" at a prompt thirty seconds ago.
    # Without this the lab produced `agent_id=4dcd94c5792e-4dcd94c5792e`, the
    # container's hostname doubled, silently replacing what the user typed.
    rc = join(code, agent_id=getattr(args, "agent_id", None) or _configured_agent_id())
    if rc != 0:
        print("firekeep: the server is running, but enrolment failed (above). "
              "Retry with: deploy/firekeep-admin invite --local --agent this-machine",
              file=sys.stderr)
        return 0

    print("\nfirekeep: this machine is connected. Check it with: firekeep doctor")

    # A second code, for the laptop. This one is a TUNNEL code (the server is
    # loopback-bound), which is the shape `firekeep join` already knows how to
    # redeem over SSH -- so the next machine needs no browser either.
    second = _mint_code(bash, root, "--agent", "workstation-1")
    if second:
        base = _server_dist_base(getattr(args, "dist_base", None))
        print(
            "\n  To add your laptop or another machine, run THIS on it "
            "(the code is single-use and expires):\n\n"
            f"    curl -fsSL {base}/latest/install | FIREKEEP_JOIN={second} sh\n\n"
            "  For any machine after that:  deploy/firekeep-admin invite --agent <name>"
        )
    return 0


def _configured_agent_id() -> str | None:
    """The identity already in ~/.firekeep/config, or None if it is unset/placeholder."""
    try:
        cfg = resolver.load_config(_config_path())
        value = cfg.get("identity", "agent_id", fallback="").strip()
    except (resolver.ConfigError, OSError):
        return None
    return value or None if value != wizard.PLACEHOLDER_AGENT_ID else None


def _default_device_label() -> str:
    """A device name that means something in a device list six months from now."""
    import socket
    try:
        name = socket.gethostname().split(".")[0].strip()
    except OSError:
        name = ""
    # The minter validates against ^[A-Za-z0-9_.-]{1,64}$; a hostname with any
    # other character would fail the invite rather than the install, which is a
    # confusing place to discover it.
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", name)[:64].strip("-")
    return safe or "server"


def _oauth_metadata_url(server_url: str) -> str:
    parsed = urlparse(server_url.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("server URL must be an http(s) origin or path without credentials, query, or fragment")
    return f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-protected-resource"


def cmd_login(args) -> int:
    """Discover hosted OAuth, or explain the self-hosted join-code path."""
    try:
        metadata_url = _oauth_metadata_url(args.server_url)
    except ValueError as exc:
        print(f"firekeep login: {exc}", file=sys.stderr)
        return 2

    try:
        metadata = get_json(metadata_url, headers={}, timeout=10, verify=True)
    except TransportError as exc:
        if exc.status == 404:
            print(
                "this server issues join codes — ask an admin for one, then run: "
                "`firekeep join <code>`"
            )
            return 2
        print(f"firekeep login: could not discover sign-in at {metadata_url}: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"firekeep login: TLS setup failed for {metadata_url}: {exc}", file=sys.stderr)
        return 1

    authorization_servers = (
        metadata.get("authorization_servers", []) if isinstance(metadata, dict) else []
    )
    if authorization_servers:
        print(
            "firekeep login: this server advertises hosted OAuth, but the hosted "
            "control-plane sign-in flow is not included in this self-hosted client "
            "build yet.",
            file=sys.stderr,
        )
        return 2
    print(
        "this server issues join codes — ask an admin for one, then run: "
        "`firekeep join <code>`"
    )
    return 2


def cmd_gateway(args) -> int:
    from firekeep_client.gateway import run

    return run(runtime=getattr(args, "runtime", None))


def cmd_doctor(args) -> int:
    try:
        results = run_doctor()
    except resolver.ConfigMigrationConflict as exc:
        print(f"firekeep: {exc}", file=sys.stderr)
        return 3
    except resolver.ConfigError as exc:
        # No config at all — the one state where doctor has nothing to check and
        # every remedy is the same. `firekeep version` already names the fix
        # here; doctor, the command the installer TELLS you to run, did not.
        print(f"firekeep: {exc}", file=sys.stderr)
        print(
            "firekeep: no usable config at ~/.firekeep/config. Re-run the "
            "installer to write one:\n"
            "  firekeep install",
            file=sys.stderr,
        )
        return 1
    rc = 0
    marks = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}
    for name, status, detail in results:
        print(f"[{marks[status]}] {name}: {detail}")
        if status == "fail":
            rc = 1
    hint = _generic_hint()
    if hint is not None:
        print(hint)
    return rc


# --- update: re-exec the bootstrap; never pip over a running process --------

def _write_verified_sums(text: str) -> Path:
    """Persist the signature-VERIFIED SHA256SUMS for the bootstrap hand-off.

    Why this exists (security review, HIGH): the client verifies
    `<version>/SHA256SUMS.minisig` against its pinned key — and then used to throw
    the verified bytes away. The bootstrap fetched its OWN copy over the network
    and verified uv + the wheels against THAT, so a host serving different bytes to
    the two fetches (they are trivially distinguishable: urllib vs curl/IWR user
    agents) installed attacker artifacts with exit 0 even under require_signed.
    Handing the verified bytes through by PATH is what makes the signature cover
    what actually gets installed.

    Created 0600 with no permissive window: unlink first, then O_CREAT|O_EXCL with
    the final mode — the file never exists readable to anyone else.
    """
    path = _firekeep_home() / "bootstrap" / "SHA256SUMS.verified"
    # mode=0o700 on the DIRECTORY too, not just the file. `mkdir` applies the
    # process umask, so under a permissive umask (000 measured) the parent lands
    # 0777 — and a world-writable parent lets a co-resident unprivileged user
    # unlink-and-replace the sums file between our write and the bootstrap's
    # read, which is the same substitution the file's own 0600 exists to stop.
    # exist_ok leaves an already-correct dir alone; the chmod re-asserts the mode
    # on a directory created by an earlier, more permissive version.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    path.unlink(missing_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    state._private(path)  # Windows: POSIX mode bits do nothing; tighten the ACL too
    return path


def _exec_bootstrap(script: Path, version: str | None, base: str,
                    *, sums_file: "Path | None" = None) -> None:
    """Hand this process over to the bootstrap script.

    POSIX: a true execve — the bootstrap replaces us. Windows: a FOREGROUND child we
    wait on, streaming its output in order to the same console, exiting with its code.
    Waiting is safe precisely because of the side-by-side layout: the bootstrap
    provisions venvs/<version> beside this process's venv and flips the `current`
    junction — nothing this process holds (its own exe included) is ever overwritten.
    The previous design rebuilt ~/.firekeep/venv in place, which forced a detached
    spawn + immediate exit to release the exe lock, and the detached installer's
    output then tore across the caller's returned prompt — the exact console mess
    this replaces.

    `base` is REQUIRED: the bootstrap dies on an unset FIREKEEP_DIST_BASE (that is its own
    fail-loud guard), and an exec'd script inherits none of our config.

    `sums_file` (when the signature verified) is handed through as FIREKEEP_SUMS_FILE so
    the bootstrap verifies artifacts against the SAME bytes the client verified, never a
    second network fetch. When absent, any inherited FIREKEEP_SUMS_FILE is dropped — a
    stale or caller-set file must not masquerade as this update's verified sums.
    """
    env = dict(os.environ)
    env["FIREKEEP_DIST_BASE"] = base
    if version:
        env["FIREKEEP_VERSION"] = version  # the bootstrap pins this exact release
    if sums_file is not None:
        env["FIREKEEP_SUMS_FILE"] = str(sums_file)
    else:
        env.pop("FIREKEEP_SUMS_FILE", None)
    # Hand the CLIENT'S pinned signing key to the bootstrap's own best-effort
    # minisign check (it otherwise trusts whatever key the HOST baked into the
    # script — circular on the update path). The bootstrap already accepts
    # FIREKEEP_SIGNING_PUB as an out-of-band override; minisign -P wants the bare
    # base64 line, which by our own convention (make_release.sign_release) is the
    # last line of the pinned text.
    from firekeep_client import signing
    pinned = signing.PINNED_PUBLIC_KEY.strip()
    if pinned:
        env["FIREKEEP_SIGNING_PUB"] = pinned.splitlines()[-1].strip()
    if os.name == "nt":
        # `powershell` is Windows PowerShell 5.1, and it must not inherit a PSModulePath
        # built by PowerShell 7. When the parent shell is pwsh (increasingly the default,
        # and what every agent runtime spawns us under), that variable leads with pwsh's
        # own module directories -- so 5.1 autoloads Microsoft.PowerShell.Utility 7.0.0.0
        # ahead of its own 3.1.0.0. That module binds SOME of its cmdlets under 5.1 and
        # not others: `Select-String` resolves, `Get-FileHash` does not. The bootstrap
        # then dies inside Verify-AgainstSums -- the checksum gate on a binary it is
        # about to execute -- with "Get-FileHash is not recognized", which reads like a
        # broken Windows install rather than an inherited variable.
        #
        # Dropping it is the fix, not overriding it: 5.1 rebuilds the correct default
        # from the registry (HKLM Session Manager\Environment) when it is unset, so we
        # never have to hard-code a path list that Microsoft owns.
        #
        # Case matters. `dict(os.environ)` on Windows returns a PLAIN dict with keys
        # UPPERCASED, losing the case-insensitive lookup os.environ itself has -- a
        # literal env.pop("PSModulePath") silently pops nothing and the bug survives.
        for key in [k for k in env if k.upper() == "PSMODULEPATH"]:
            env.pop(key)
        # Foreground on purpose (see docstring): the side-by-side bootstrap never
        # touches this process's venv, so there is no lock to get out of the way of.
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            env=env,
        )
        sys.exit(proc.wait())
    os.execve("/bin/sh", ["/bin/sh", str(script)], env)


def _set_auto_update(enabled: bool) -> int:
    """Persist the [dist] auto_update flag. session_start reads it to decide whether
    to background-update. (A checkout install has no [dist] base_url, so auto-update
    stays inert there regardless — this just records the preference.)"""
    path = _config_path()
    try:
        cfg = resolver.load_config(path)
    except resolver.ConfigMigrationConflict as exc:
        print(f"firekeep: {exc}", file=sys.stderr)
        return 3
    except resolver.ConfigError as exc:
        print(f"firekeep: {exc}", file=sys.stderr)
        return 1
    if not cfg.has_section("dist"):
        cfg.add_section("dist")
    cfg.set("dist", "auto_update", "true" if enabled else "false")
    with open(path, "w", encoding="utf-8") as handle:
        cfg.write(handle)
    state._private(path)
    print(f"firekeep: background auto-update {'enabled' if enabled else 'disabled'} "
          f"(applies from the next session start)")
    return 0


def cmd_update(args) -> int:
    # `--auto on|off` only toggles the preference; it never also runs an update.
    if getattr(args, "auto", None):
        return _set_auto_update(args.auto == "on")
    # ONE try block around every UpdateError-raising call. is_newer() -> parse_version()
    # raises on a malformed version string, and fetch_manifest only checks that `version` is
    # a str, not that it parses — so a bad manifest deploy (plain-HTTP fetch, no signing)
    # would otherwise dump a raw traceback at a teammate.
    try:
        cfg = resolver.load_config(_config_path())
        base = updater.dist_base(cfg)
        manifest = updater.fetch_manifest(base)

        target = getattr(args, "to", None) or manifest.version
        newer = updater.is_newer(manifest.version, __version__)

        if getattr(args, "check", False):
            if newer:
                print(f"firekeep: {__version__} installed, {manifest.version} available "
                      f"— run `firekeep update`")
            else:
                print(f"firekeep: {__version__} is already up to date")
            return 0

        if not args.to and not newer:
            print(f"firekeep: {__version__} is already up to date")
            return 0

        windows = os.name == "nt"
        # Release-signing check (docs/RELEASE-SIGNING.md): verify the TARGET release's
        # SHA256SUMS signature against the key pinned in THIS build — the target is what
        # the bootstrap will install, so `--to <older>` must verify THAT version's sums,
        # not latest's (security review, MEDIUM: verifying manifest.version while the
        # bootstrap pinned FIREKEEP_VERSION=target left every rollback unsigned, even
        # under require_signed=true). verify-if-present — absence warns (until
        # [dist] require_signed flips), an invalid signature is always fatal inside
        # fetch_signed_sums itself.
        req_signed = updater.require_signed(cfg)
        signed = updater.fetch_signed_sums(base, target, require_signed=req_signed)
        # The bootstrap SCRIPT we execute is always latest/'s, whose bytes are listed in
        # the LATEST version's signed sums. On a pinned --to that differs from latest,
        # anchor the script hash against latest's sums separately — target's sums
        # describe the target's own (older) scripts, not the one we are about to run.
        if target == manifest.version:
            signed_script = signed
        else:
            signed_script = updater.fetch_signed_sums(
                base, manifest.version, require_signed=req_signed
            )
        warnings = []
        for s in (signed, signed_script):
            if s.warning and s.warning not in warnings:
                warnings.append(s.warning)
        for warning in warnings:
            print(f"firekeep: WARNING: {warning}", file=sys.stderr)
        if warnings:
            # The detached background auto-update sends this stderr to DEVNULL, so
            # persist a one-shot marker the NEXT session_start briefing prints —
            # otherwise nobody ever learns an unsigned release was installed
            # (security review, MEDIUM: absence is attacker-choosable while
            # require_signed=false, and an invisible warning is no warning).
            state.note_unsigned_update(
                f"client update to {target} ran WITHOUT a verified release signature "
                f"({warnings[0]}); [dist] require_signed=false tolerates this — "
                f"see docs/RELEASE-SIGNING.md"
            )
        # The checksum is REQUIRED here: we are about to EXECUTE this script. Verifying uv
        # inside install.sh while exec'ing an unverified install.sh would be theatre.
        # When the signature verified, bootstrap_sha256 anchors this hash to the SIGNED
        # SHA256SUMS (and refuses a manifest that disagrees with it).
        # download() creates dest's parent itself — do not duplicate that here.
        script = updater.download(
            updater.bootstrap_url(base, windows=windows),
            _firekeep_home() / "bootstrap" / ("install.ps1" if windows else "install.sh"),
            sha256=updater.bootstrap_sha256(manifest, signed_script, windows=windows),
        )
        # Thread the VERIFIED sums through to the bootstrap (see _write_verified_sums):
        # only ever the TARGET's sums — they are what the bootstrap verifies uv and the
        # wheels for FIREKEEP_VERSION=target against. A write failure is FATAL, not a
        # silent degrade: handing nothing would put the bootstrap back on its own
        # network fetch — the exact hole the hand-off closes.
        sums_file = None
        if signed.verified and signed.text is not None:
            try:
                sums_file = _write_verified_sums(signed.text)
            except OSError as exc:
                raise updater.UpdateError(
                    f"cannot write the verified SHA256SUMS for the bootstrap "
                    f"hand-off: {exc}"
                ) from exc
    except resolver.ConfigMigrationConflict as exc:
        print(f"firekeep: {exc}", file=sys.stderr)
        return 3
    except (resolver.ConfigError, updater.UpdateError) as exc:
        print(f"firekeep: {exc}", file=sys.stderr)
        return 1

    print(f"firekeep: updating {__version__} -> {target}")
    _exec_bootstrap(script, target, base, sums_file=sums_file)
    return 0  # POSIX never reaches this (execve replaced the image)


# --- parser / dispatch -------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="firekeep", description="Firekeep client")
    sub = parser.add_subparsers(dest="command")

    prof = sub.add_parser("profile", help=argparse.SUPPRESS)
    prof.add_argument("retired_args", nargs=argparse.REMAINDER)
    prof.set_defaults(func=cmd_profile_removed)

    ver = sub.add_parser("version", help="print client version + skew status")
    ver.set_defaults(func=cmd_version)

    inst = sub.add_parser("install", help="install/refresh the client kit")
    # An unset runtime prepares every shipped adapter. Explicit --runtime (or the
    # bootstrap's FIREKEEP_RUNTIME) remains available for a targeted re-render.
    inst.add_argument(
        "--runtime",
        choices=["claude", "codex", "kiro", "opencode", "generic", "all"],
        default=None,
    )
    # `generic` is any MCP client the kit ships no bespoke adapter for: it prints
    # a paste-in gateway snippet, and --agents-md points it at that client's
    # rules file so the protocol is installed as text too. Persisting the path is
    # also what makes generic join later installs/uninstalls.
    inst.add_argument("--agents-md", metavar="PATH",
                      help="rules/AGENTS.md file the generic runtime manages "
                           "(only with --runtime generic)")
    # Config answers. Interactively each SEEDS its prompt's default; with
    # --non-interactive (or no TTY) each is written straight to the config.
    inst.add_argument("--agent-id", help="identity attributed to memories/sessions")
    inst.add_argument("--host", help="service host for a ports-style connection")
    inst.add_argument("--dist-base", metavar="URL",
                      help="release base URL this kit came from (set by the bootstrap; "
                           "enables `firekeep update`)")
    inst.add_argument("--non-interactive", action="store_true",
                      help="never prompt (implied when stdin is not a TTY)")
    inst.add_argument("--join", metavar="CODE",
                      help="enroll from a single-use join code (also via FIREKEEP_JOIN)")
    inst.add_argument("--no-modify-path", action="store_true",
                      help="do not put a `firekeep` launcher on PATH (also via "
                           "FIREKEEP_NO_MODIFY_PATH)")
    inst.set_defaults(func=cmd_install)

    # Positional-choices rather than nested subparsers — the `personal` shape
    # below, and the only sub-command pattern this package uses.
    dex = sub.add_parser("dex", help="manage dexes — domain indexes the Keep understands")
    dex.add_argument("action", nargs="?", choices=["list", "add", "remove"], default="list")
    dex.add_argument("name", nargs="?", help="dex name (symdex, docdex)")
    dex.set_defaults(func=cmd_dex)

    personal = sub.add_parser(
        "personal",
        help="toggle personal (bypass) mode — Firekeep dormant for this session",
    )
    personal.add_argument(
        "action", nargs="?", choices=["on", "off", "status", "toggle"], default="toggle",
    )
    personal.set_defaults(func=cmd_personal)

    conn = sub.add_parser(
        "connect",
        help="point this machine at a Firekeep server over ssh (mint key, tunnel, verify)")
    conn.add_argument("target", help="ssh target for the server, e.g. root@203.0.113.10")
    conn.add_argument("--agent-id", default=None, help="identity to mint the key for")
    conn.add_argument("--remote-dir", default=None, help="server install dir if not auto-detected")
    conn.add_argument("--no-tunnel", action="store_true",
                      help="do not manage an SSH tunnel (needs a non-loopback server)")
    conn.set_defaults(func=cmd_connect)

    join_parser = sub.add_parser(
        "join", help="enroll this machine with a single-use Firekeep join code"
    )
    join_parser.add_argument("code")
    join_parser.add_argument("--agent-id", default=None)
    join_parser.add_argument("--force", action="store_true")
    join_parser.add_argument("--print-key", action="store_true")
    join_parser.add_argument("--resume", action="store_true")
    join_parser.set_defaults(func=cmd_join)

    init_parser = sub.add_parser(
        "init", help="provision a new local or self-hosted Firekeep server"
    )
    init_parser.add_argument(
        "--server-dir", metavar="PATH", help="server bundle destination or source directory"
    )
    init_parser.add_argument(
        "--pull", action="store_true", help="pull published images even from a source checkout"
    )
    init_parser.add_argument(
        "--version", metavar="vX.Y.Z", help="install a specific published server version"
    )
    init_parser.add_argument(
        "--dist-base", metavar="URL", help="override the public release distribution URL"
    )
    init_parser.add_argument(
        "--office", action="store_true", help="enable the TLS reverse-proxy deployment"
    )
    # Default ON: the overwhelmingly common case is a person at a shell on the box
    # they are provisioning, and leaving that machine unable to talk to the server
    # it just built is the defect this whole flow exists to fix. --no-self-enroll
    # covers headless provisioning (CI, Ansible, a golden image) where minting a
    # device credential into the image would be wrong.
    init_parser.add_argument(
        "--self-enroll", action="store_true", default=True,
        help="enrol this machine against the new server and print a join code for the next one (default)",
    )
    init_parser.add_argument(
        "--no-self-enroll", action="store_false", dest="self_enroll",
        help="provision only; mint no credentials for this machine",
    )
    init_parser.add_argument(
        "--agent-id", metavar="NAME",
        help="identity to enrol as (defaults to the identity already in ~/.firekeep/config)",
    )
    init_parser.set_defaults(func=cmd_init)

    uninstall_parser = sub.add_parser(
        "uninstall",
        help="remove the client kit (runtime configs, PATH entry, ~/.firekeep)",
    )
    uninstall_parser.add_argument(
        "--yes", "-y", action="store_true",
        help="skip the confirmation prompt (removes the client only, never server data)",
    )
    uninstall_parser.add_argument(
        "--server", action="store_true",
        help="also tear down the managed server stack and DELETE ALL DATA "
             "(docker compose down -v; Neo4j/Qdrant/Redis volumes)",
    )
    uninstall_parser.set_defaults(func=cmd_uninstall)

    login_parser = sub.add_parser(
        "login", help="attach through hosted sign-in, when the server supports it"
    )
    login_parser.add_argument("server_url")
    login_parser.set_defaults(func=cmd_login)

    gateway = sub.add_parser("gateway", help="run the local Firekeep MCP gateway")
    # Rendered by each adapter as `firekeep gateway --runtime <its name>` so
    # proxied server calls carry X-Firekeep-* attribution. Default None: an old
    # rendered config (no flag) keeps working, with no attribution headers.
    gateway.add_argument("--runtime", default=None,
                         help="runtime that launched this gateway "
                              "(claude|codex|kiro|opencode|generic)")
    gateway.set_defaults(func=cmd_gateway)

    # `status` alias: what operators type first on an unfamiliar CLI (observed
    # live on the maintainer's own Mac before it existed). Dispatch is via
    # set_defaults(func=...), so the alias needs no handler changes.
    doc = sub.add_parser(
        "doctor", aliases=["status"],
        help="preflight health / skew / perm checks",
    )
    doc.set_defaults(func=cmd_doctor)

    shift = sub.add_parser(
        "night-shift",
        help="distill queued sessions into memory/draft skills via the local LLM",
    )
    shift.add_argument("--max", type=int, default=5, metavar="N",
                       help="max tasks to drain this run (default 5)")
    shift.add_argument("--dry-run", action="store_true",
                       help="synthesize but write nothing (no leases either)")
    shift.set_defaults(func=cmd_night_shift)

    rest = sub.add_parser(
        "restore",
        help="browse or restore local snapshots of uncommitted work",
    )
    rest.add_argument("--list", action="store_true",
                      help="list snapshots for this repo (the default)")
    rest.add_argument("--apply", metavar="ID",
                      help="restore this snapshot into the working tree")
    rest.set_defaults(func=cmd_restore)

    upd = sub.add_parser("update", help="update the client kit to the latest release")
    upd.add_argument("--check", action="store_true", help="report only; change nothing")
    upd.add_argument("--to", metavar="VERSION",
                     help="install this exact version (also how you roll back)")
    upd.add_argument("--auto", choices=["on", "off"],
                     help="enable/disable background auto-update on session start "
                          "(default on; also via FIREKEEP_NO_AUTO_UPDATE)")
    upd.set_defaults(func=cmd_update)

    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 1
    try:
        return func(args)
    except resolver.ConfigMigrationConflict as exc:
        print(f"firekeep: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
