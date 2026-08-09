"""`firekeep` CLI — install, connect, doctor, update, and dormancy controls.

Stdlib-only (plus firekeep_client submodules). Never imports mcp/httpx.
Native runtime config is written once at install and refreshed idempotently.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import ssl
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from firekeep_client import __version__, pathenv, resolver, serverinit, state, updater, wizard
from firekeep_client.adapters import get_adapter
from firekeep_client.adapters.base import FIREKEEP_INSTRUCTIONS, INSTRUCTIONS_BEGIN, INSTRUCTIONS_END
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

_CONFIG_SKELETON = """\
[identity]
agent_id = CHANGEME

[server]
kind = ports
scheme = http
host = 127.0.0.1
verify_tls = false
api_key =
"""


def _firekeep_home() -> Path:
    return _config_path().parent


def _venv_bin(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def _venv_python(venv: Path) -> Path:
    return _venv_bin(venv) / ("python.exe" if os.name == "nt" else "python")


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no", "off")


# Bounded, generous, env-overridable (Global Constraints: never a silent hang;
# but pip installs legitimately take minutes on slow links).
_INSTALL_TIMEOUT = float(os.environ.get("FIREKEEP_INSTALL_TIMEOUT", "600"))


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


def _selected_runtimes(runtime: str) -> list[str]:
    if runtime == "all":
        return ["claude", "codex", "kiro", "opencode"]
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


def _configure(args) -> bool:
    """Build ~/.firekeep/config, interactively when there's a human to ask.

    Returns True if the config still holds the CHANGEME placeholder afterwards — the caller
    prints the hand-edit NEXT STEPS only in that case. A teammate who answered the prompts
    should never be told to go edit the file they just filled in."""
    path = _config_path()
    cfg = resolver.load_config(path)
    interactive = wizard.is_interactive() and not getattr(args, "non_interactive", False)

    # A normal install prepares every shipped runtime. The user should not have to predict
    # which client they will use later, and selecting one here used to leave the others
    # looking broken. Explicit --runtime remains the targeted re-render/repair path.
    if getattr(args, "runtime", None) is None:
        args.runtime = "all"

    if interactive:
        print("firekeep: configuring ~/.firekeep/config (Enter accepts the [default])")
        wizard.prompt_config(
            cfg,
            agent_id=getattr(args, "agent_id", None),
            host=getattr(args, "host", None),
        )
        if getattr(args, "dist_base", None):
            wizard.set_dist_base(cfg, args.dist_base)
        changed = True
    else:
        changed = _apply_flags(cfg, args)

    if changed:
        with open(path, "w", encoding="utf-8") as handle:
            cfg.write(handle)

    return cfg.get("identity", "agent_id", fallback="").strip() == wizard.PLACEHOLDER_AGENT_ID


def cmd_install(args) -> int:
    # Fail-loud per step: a teammate's FIRST command must never dump a raw
    # traceback or hang unbounded (the <5 min onboarding promise).
    step = "bootstrap ~/.firekeep"
    join_code = getattr(args, "join", None) or os.environ.get("FIREKEEP_JOIN", "").strip()
    join_result = 0
    try:
        home = _firekeep_home()
        _bootstrap_home(home)

        # Ask BEFORE the venv/pip work: a teammate should not sit through a multi-minute
        # pip install only to then be asked who they are.
        step = "configure ~/.firekeep/config"
        if join_code:
            # The code carries every answer. A TTY must not re-enable prompts.
            args.non_interactive = True
        needs_edit = _configure(args)

        step = "create venv"
        venv = home / "venv"
        # kit resolved BEFORE the venv step: when kit is None the process is EXECUTING
        # from ~/.firekeep/venv (the bootstrap's wizard hand-off, or a documented
        # re-render), and with no kit dir there is nothing to reinstall afterwards —
        # so never create or rebuild here. The pip-less-venv rebuild in _create_venv
        # exists for half-built CHECKOUT installs; run against the bootstrap's uv venv
        # (which ships no pip BY DESIGN) it wiped the very install it belonged to,
        # leaving a bare pip-only venv (release-breaking bug found live in the 0.1.2
        # bootstrap acceptance, 2026-07-13). The venv's existence is self-evident when
        # we are running from it.
        kit = _kit_dir()
        if kit is not None:
            _create_venv(venv)
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

        step = "render runtime adapters"
        venv_bin = _venv_bin(venv)
        for name in _selected_runtimes(args.runtime):
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
    if needs_edit:
        print("firekeep: NEXT STEPS — edit ~/.firekeep/config: set agent_id (currently "
              "CHANGEME) and complete the [server] connection values. "
              "Open a new terminal (or `source` your shell rc), then run `firekeep doctor`. "
              "Config changes apply on next agent start.")
    else:
        print("firekeep: NEXT STEPS — open a new terminal (or `source` your shell rc), "
              "then run `firekeep doctor`. Config changes apply on next agent start.")
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


def _check_entitlement(cfg) -> tuple[str, str, str] | None:
    """Report the server-authoritative plan and any licence expiry warning."""
    try:
        ep = resolver.resolve("cortex", cfg=cfg)
        data = get_json(f"{ep.rest_base}/workspace", headers=ep.headers, verify=ep.verify)
    except (TransportError, resolver.ConfigError, OSError):
        return None  # reachability/auth already have dedicated doctor rows
    entitlement = data.get("entitlement") if isinstance(data, dict) else None
    if not isinstance(entitlement, dict):
        return None  # pre-workspace server; version/update rows provide the action
    plan = str(entitlement.get("plan", "solo")).title()
    maximum = entitlement.get("max_members", 1)
    warning = entitlement.get("warning")
    if warning:
        return ("licence", "warn", f"{plan}, {maximum} member(s) — {warning}")
    if not entitlement.get("verified") and entitlement.get("source") != "built-in":
        reason = entitlement.get("reason", "licence is not valid")
        return ("licence", "warn", f"{plan}, {maximum} member(s) — {reason}")
    return ("licence", "ok", f"{plan}, up to {maximum} member(s)")


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
        # Partial-venv detection: `firekeep install` only creates the venv when
        # the bin dir doesn't exist yet (_create_venv short-circuits
        # otherwise), so a venv whose creation was interrupted -- bin dir
        # present, python interpreter never landed -- will NOT be repaired by
        # a plain rerun. Name that distinctly so the fix (delete + reinstall)
        # is obvious rather than a confusing repeat failure.
        if venv.exists() and not (bindir / f"python{ext}").exists():
            return (
                "venv-scripts", "fail",
                f"partial venv at {venv}: python interpreter never landed in "
                f"{bindir} (a rerun of `firekeep install` will NOT repair this -- "
                f"it skips venv creation once the bin dir exists; delete "
                f"{venv} and rerun `firekeep install`); also missing: "
                f"{', '.join(missing)}",
            )
        return ("venv-scripts", "fail", f"missing in {bindir}: {', '.join(missing)}")
    return ("venv-scripts", "ok", str(bindir))


def _check_codex_adapter(venv: Path) -> list[tuple[str, str, str]]:
    config = Path.home() / ".codex" / "config.toml"
    instructions = Path.home() / ".codex" / "AGENTS.md"
    repair = "run `firekeep install --runtime codex`"

    instruction_text = ""
    instruction_error: OSError | None = None
    try:
        if instructions.exists():
            instruction_text = instructions.read_text(encoding="utf-8")
    except OSError as exc:
        instruction_error = exc
    has_instruction_block = INSTRUCTIONS_BEGIN in instruction_text
    expected_instructions = f"{INSTRUCTIONS_BEGIN}\n{FIREKEEP_INSTRUCTIONS}{INSTRUCTIONS_END}"
    instructions_current = expected_instructions in instruction_text

    if not config.exists():
        if not has_instruction_block:
            return []
        rows = [
            ("codex-mcp", "fail", f"{config} missing while Firekeep instructions exist; {repair}"),
        ]
        if instructions_current:
            rows.append(("codex-instructions", "ok", str(instructions)))
        else:
            rows.append((
                "codex-instructions", "warn",
                f"Firekeep instruction block missing or stale in {instructions}; {repair}",
            ))
        return rows

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

    rows: list[tuple[str, str, str]] = []
    if not mcp_block_is_current(config_text, _venv_bin(venv)):
        rows.append(("codex-mcp", "fail", f"stale or missing Firekeep gateway in {config}; {repair}"))
    else:
        rows.append(("codex-mcp", "ok", str(config)))

    if instruction_error is not None:
        rows.append((
            "codex-instructions", "warn",
            f"cannot read {instructions}: {instruction_error}; {repair}",
        ))
    elif not instructions_current:
        rows.append((
            "codex-instructions", "warn",
            f"Firekeep instruction block missing or stale in {instructions}; {repair}",
        ))
    else:
        rows.append(("codex-instructions", "ok", str(instructions)))
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
    results.extend(_check_health(cfg))
    results.append(_check_versions(cfg))
    entitlement = _check_entitlement(cfg)
    if entitlement is not None:
        results.append(entitlement)
    client_version = _check_client_version(cfg)
    if client_version is not None:
        results.append(client_version)
    agent_id_result = _check_agent_id(cfg)
    if agent_id_result is not None:
        results.append(agent_id_result)
    api_key_result = _check_api_key(cfg)
    if api_key_result is not None:
        results.append(api_key_result)
    results.append(_check_venv_scripts(_firekeep_home() / "venv"))
    results.extend(_check_codex_adapter(_firekeep_home() / "venv"))
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


def cmd_init(args) -> int:
    """Provision a server from a local checkout or the verified public bundle."""
    root = _server_source_dir(args.server_dir)
    downloaded = False
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
        subprocess.run(command, cwd=root, check=True, timeout=_INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(
            f"firekeep init timed out after {_INSTALL_TIMEOUT:.0f}s "
            "(override with FIREKEEP_INSTALL_TIMEOUT).",
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
    print(
        "firekeep: server provisioned. Use Dashboard -> Devices -> Add device, "
        "then run `firekeep join <code>` on each client machine."
    )
    return 0


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

    return run()


def cmd_doctor(args) -> int:
    try:
        results = run_doctor()
    except resolver.ConfigMigrationConflict as exc:
        print(f"firekeep: {exc}", file=sys.stderr)
        return 3
    except resolver.ConfigError as exc:
        print(f"firekeep: {exc}", file=sys.stderr)
        return 1
    rc = 0
    marks = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}
    for name, status, detail in results:
        print(f"[{marks[status]}] {name}: {detail}")
        if status == "fail":
            rc = 1
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
    """Replace THIS process with the bootstrap script.

    The whole point: by the time uv rewrites ~/.firekeep/venv, `firekeep` is no longer running.
    On Windows, `Scripts\\firekeep.exe` is locked while it executes and simply cannot be
    overwritten in place — every self-upgrading tool that ignores this grows a rename-dance.
    Handing off means the replacing process is uv, under sh/powershell, and nothing is held.

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
        # os.execv on Windows can leave the parent's image briefly held; spawn detached and
        # exit immediately so the launcher is released before uv touches the venv.
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            env=env, close_fds=True,
        )
        sys.exit(0)
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
        "--runtime", choices=["claude", "codex", "kiro", "opencode", "all"], default=None
    )
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
    init_parser.set_defaults(func=cmd_init)

    login_parser = sub.add_parser(
        "login", help="attach through hosted sign-in, when the server supports it"
    )
    login_parser.add_argument("server_url")
    login_parser.set_defaults(func=cmd_login)

    gateway = sub.add_parser("gateway", help="run the local Firekeep MCP gateway")
    gateway.set_defaults(func=cmd_gateway)

    doc = sub.add_parser("doctor", help="preflight health / skew / perm checks")
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
