"""`firekeep` CLI — profile switching, install, doctor, version.

Stdlib-only (plus firekeep_client submodules). Never imports mcp/httpx.
Native config is written once at install; `profile use` is a pointer flip
(D-switch) that the shim reads at next agent spawn.
"""
from __future__ import annotations

import argparse
import os
import ssl
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from firekeep_client import __version__, pathenv, resolver, state, updater, wizard
from firekeep_client.adapters import get_adapter
from firekeep_client.transport import get_json, TransportError


def _config_path() -> Path:
    """Path to ~/.firekeep/config, overridable via env FIREKEEP_CONFIG (tests)."""
    env = os.environ.get("FIREKEEP_CONFIG")
    return Path(env) if env else resolver.CONFIG_PATH


# --- profile -----------------------------------------------------------------

def cmd_profile_use(args) -> int:
    path = _config_path()
    try:
        cfg = resolver.load_config(path)
    except resolver.ConfigError as exc:
        print(f"firekeep: {exc}", file=sys.stderr)
        return 1
    if not cfg.has_section(args.name):
        print(
            f"firekeep: unknown profile '{args.name}' "
            f"(define [{args.name}] in {path})",
            file=sys.stderr,
        )
        return 1
    if not cfg.has_section("active"):
        cfg.add_section("active")
    cfg.set("active", "profile", args.name)
    with open(path, "w", encoding="utf-8") as handle:
        cfg.write(handle)
    state._private(path)  # config carries the office key — keep it locked down
    print(f"firekeep: active profile -> {args.name}")
    _env_profile_notice()
    return 0


def cmd_profile_show(args) -> int:
    _env_profile_notice()
    path = _config_path()
    try:
        cfg = resolver.load_config(path)
        active = resolver.active_profile(cfg)
    except resolver.ConfigError as exc:
        print(f"firekeep: {exc}", file=sys.stderr)
        return 1
    print(f"active profile: {active}")
    for svc in resolver.SERVICES:
        try:
            ep = resolver.resolve(svc, cfg=cfg)
        except resolver.ConfigError as exc:
            print(f"  {svc}: config error: {exc}")
            continue
        headers = dict(ep.headers)
        if "X-API-Key" in headers:
            headers["X-API-Key"] = "REDACTED"
        print(
            f"  {svc}: mcp={ep.mcp_url} rest={ep.rest_base} "
            f"verify={ep.verify} headers={headers}"
        )
    if cfg.has_section("pins"):
        for runtime, prof in cfg.items("pins"):
            print(f"  pin: {runtime} -> {prof}")
    return 0


_PIN_RUNTIMES = ("claude", "codex", "kiro", "opencode")

# Structural config sections that are NOT profiles. Pinning a runtime to one of these
# passes the charset check and (for [active]/[pins]/[dist] on a real config) even the
# has_section check, but resolves to nonsense at render time — reject at write time,
# warn in doctor.
_RESERVED_SECTIONS = ("active", "pins", "dist")


def _env_profile_notice() -> None:
    env = os.environ.get("FIREKEEP_PROFILE", "").strip()
    if not env:
        return
    # Best-effort enrichment: naming WHICH [active] profile is being overridden helps a
    # user who forgot the export. Any config trouble (missing file, malformed INI, no
    # [active] section) falls back to the generic text — this is a notice, it must
    # never fail or noisy-up the command that triggered it.
    try:
        cfg = resolver.load_config(_config_path())
        name = cfg.get("active", "profile", fallback="").strip()
    except Exception:  # noqa: BLE001 — informational only, degrade to the generic notice
        name = ""
    if name:
        print(f"note: FIREKEEP_PROFILE={env} overrides [active] profile '{name}' "
              "for this shell")
    else:
        print(f"note: FIREKEEP_PROFILE={env} overrides the [active] profile for this shell")


def _rerender_runtime(runtime: str) -> None:
    """A recorded pin that isn't rendered is a silent lie — re-render immediately.
    Best-effort: a render failure must not strand the config write (the pin IS saved;
    `firekeep install --runtime <rt>` re-renders later)."""
    try:
        get_adapter(runtime).render(venv_bin=_venv_bin(_firekeep_home() / "venv"))
        print(f"firekeep: {runtime} adapter re-rendered (applies on next agent start)")
    except Exception as exc:  # noqa: BLE001 — installer-adjacent surface, fail loud not raw
        print(f"firekeep: WARNING — pin saved but re-render failed: {exc}; "
              f"run `firekeep install --runtime {runtime}`", file=sys.stderr)


def cmd_profile_pin(args) -> int:
    path = _config_path()
    try:
        cfg = resolver.load_config(path)
    except resolver.ConfigError as exc:
        print(f"firekeep: {exc}", file=sys.stderr)
        return 1
    if not resolver.PIN_NAME_RE.match(args.profile):
        print("firekeep: pin profile names must match ^[A-Za-z0-9_-]+$ "
              "(they are rendered into hook command strings)", file=sys.stderr)
        return 1
    if args.profile in _RESERVED_SECTIONS:
        # Must run BEFORE has_section: [active]/[pins]/[dist] usually EXIST as sections,
        # so the unknown-profile check below would wave them through.
        print(f"firekeep: '{args.profile}' is a reserved config section, not a profile "
              f"(reserved: {', '.join(_RESERVED_SECTIONS)})", file=sys.stderr)
        return 1
    if not cfg.has_section(args.profile):
        print(f"firekeep: unknown profile '{args.profile}' "
              f"(define [{args.profile}] in {path})", file=sys.stderr)
        return 1
    if not cfg.has_section("pins"):
        cfg.add_section("pins")
    cfg.set("pins", args.runtime, args.profile)
    with open(path, "w", encoding="utf-8") as handle:
        cfg.write(handle)
    state._private(path)
    print(f"firekeep: {args.runtime} pinned to profile '{args.profile}'")
    _rerender_runtime(args.runtime)
    return 0


def cmd_profile_unpin(args) -> int:
    path = _config_path()
    try:
        cfg = resolver.load_config(path)
    except resolver.ConfigError as exc:
        print(f"firekeep: {exc}", file=sys.stderr)
        return 1
    if not (cfg.has_section("pins") and cfg.has_option("pins", args.runtime)):
        print(f"firekeep: {args.runtime} is not pinned")
        return 0
    cfg.remove_option("pins", args.runtime)
    if not cfg.options("pins"):
        cfg.remove_section("pins")
    with open(path, "w", encoding="utf-8") as handle:
        cfg.write(handle)
    state._private(path)
    print(f"firekeep: {args.runtime} unpinned (follows the active profile again)")
    _rerender_runtime(args.runtime)
    return 0


def cmd_profile_help(args) -> int:
    print("usage: firekeep profile {use <name>|show|pin <runtime> <profile>|unpin <runtime>}",
          file=sys.stderr)
    return 1


# --- version (skew status added in Task 28) ----------------------------------

def cmd_version(args) -> int:
    print(f"firekeep-client {__version__}")
    try:
        cfg = resolver.load_config(_config_path())
    except resolver.ConfigError:
        print("skew: no config (run firekeep install)")
        return 0
    _, _, detail = _check_skew(cfg)
    print(f"skew: {detail}")
    return 0


# --- install: venv, pip, ~/.firekeep bootstrap, adapter render -----------------

_CONFIG_SKELETON = """\
[active]
profile = personal

[personal]
kind = ports
scheme = http
host = 127.0.0.1
verify_tls = false
agent_id = CHANGEME

[office]
kind = paths
scheme = https
base_url = https://firekeep.office.example
verify_tls = true
# ca_path may be a PEM file, or the literal `os` to verify against the
# operating-system trust store (corporate CA in the OS keychain — no file).
ca_path = ~/.firekeep/firekeep-root-ca.crt
api_key =
agent_id = CHANGEME
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
    """Non-interactive path: land --agent-id / --host / --profile in the config directly.
    Returns True if anything was set (so the caller knows to write)."""
    profile = getattr(args, "profile", None) or cfg.get(
        "active", "profile", fallback="personal")
    if not cfg.has_section(profile):
        cfg.add_section(profile)
    touched = False
    if getattr(args, "profile", None):
        if not cfg.has_section("active"):
            cfg.add_section("active")
        cfg.set("active", "profile", profile)
        touched = True
    if getattr(args, "agent_id", None):
        cfg.set(profile, "agent_id", args.agent_id)
        touched = True
    if getattr(args, "host", None):
        cfg.set(profile, "host", args.host)
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

    # Resolve which agent(s) to render. Explicit --runtime (or FIREKEEP_RUNTIME, which the
    # bootstrap forwards as --runtime) wins; otherwise ask the human, or default to all when
    # headless. Asked first so it frames the rest of the prompts.
    if getattr(args, "runtime", None) is None:
        args.runtime = wizard.ask_runtime() if interactive else "all"

    if interactive:
        print("firekeep: configuring ~/.firekeep/config (Enter accepts the [default])")
        wizard.prompt_config(
            cfg,
            agent_id=getattr(args, "agent_id", None),
            host=getattr(args, "host", None),
            profile=getattr(args, "profile", None),
        )
        if getattr(args, "dist_base", None):
            wizard.set_dist_base(cfg, args.dist_base)
        changed = True
    else:
        changed = _apply_flags(cfg, args)

    if changed:
        with open(path, "w", encoding="utf-8") as handle:
            cfg.write(handle)

    active = cfg.get("active", "profile", fallback="personal")
    return cfg.get(active, "agent_id", fallback="").strip() == wizard.PLACEHOLDER_AGENT_ID


def cmd_install(args) -> int:
    # Fail-loud per step: a teammate's FIRST command must never dump a raw
    # traceback or hang unbounded (the <5 min onboarding promise).
    step = "bootstrap ~/.firekeep"
    try:
        home = _firekeep_home()
        _bootstrap_home(home)

        # Ask BEFORE the venv/pip work: a teammate should not sit through a multi-minute
        # pip install only to then be asked who they are.
        step = "configure ~/.firekeep/config"
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
    except subprocess.TimeoutExpired:
        print(f"firekeep: install failed at '{step}': timed out after "
              f"{_INSTALL_TIMEOUT:.0f}s (override with FIREKEEP_INSTALL_TIMEOUT)",
              file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - installer surface, fail loud not raw
        print(f"firekeep: install failed at '{step}': {exc}", file=sys.stderr)
        return 1

    print(f"firekeep: installed into {home}")
    registered = ("firekeep-cortex, firekeep-bridge, firekeep-sentinel, firekeep-relay, "
                  "firekeep-decision, firekeep-symdex")
    print(f"firekeep: registered MCP servers: {registered}")
    print("firekeep: tip — `/personal` (Claude) or `firekeep personal` toggles a private "
          "session where Firekeep is fully bypassed (nothing logged/recalled); it "
          "auto-clears at session end.")
    for msg in path_msgs:
        print(f"firekeep: {msg}")
    if needs_edit:
        print("firekeep: NEXT STEPS — edit ~/.firekeep/config: set agent_id (currently "
              "CHANGEME) and, for the office profile, api_key + base_url + ca_path. "
              "Open a new terminal (or `source` your shell rc), then run `firekeep doctor`. "
              "Profile changes apply on next agent start.")
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


def _check_skew(cfg) -> tuple[str, str, str]:
    try:
        ep = resolver.resolve("cortex", cfg=cfg)
        data = get_json(f"{ep.rest_base}/version", headers=ep.headers, verify=ep.verify)
        server = data.get("version") if isinstance(data, dict) else None
        if server == __version__:
            return ("version-skew", "ok", f"client and cortex both {__version__}")
        return ("version-skew", "warn", f"client {__version__} != cortex {server}")
    except (TransportError, resolver.ConfigError, OSError) as exc:
        # OSError: see _check_health's comment -- an unverifiable ca_path
        # raises ssl.SSLError, not TransportError. Skew unknown, not fatal.
        return ("version-skew", "warn", f"cortex /version unreachable: {exc}")


def _check_agent_id(cfg) -> tuple[str, str, str] | None:
    # The installer skeleton (_CONFIG_SKELETON above) writes agent_id =
    # CHANGEME for both profiles. If a teammate never edits it, every memory
    # write / session / replay event silently attributes to "CHANGEME" —
    # cheap to catch here, expensive to untangle after the fact. Report the
    # EFFECTIVE identity (resolver.agent_id() applies the FIREKEEP_AGENT_ID env
    # override), not the raw config text, so an env-based override that
    # already fixes a stale CHANGEME doesn't get flagged.
    try:
        profile = resolver.active_profile(cfg)
        value = resolver.agent_id(cfg, profile)
    except resolver.ConfigError as exc:
        return ("agent-id", "warn", str(exc))
    if value == "CHANGEME":
        return (
            "agent-id", "warn",
            f"profile '{profile}' agent_id is still the installer default "
            "'CHANGEME' -- set a real identity in ~/.firekeep/config (or export "
            "FIREKEEP_AGENT_ID) or your work will be attributed to 'CHANGEME'",
        )
    return ("agent-id", "ok", f"agent_id={value}")


def _check_api_key(cfg, profile: str | None = None,
                   label: str = "api-key") -> tuple[str, str, str] | None:
    """Warn on an https (office-style) profile with a missing/empty api_key.

    THE FALSE-GREEN TRAP this closes: /health and Cortex /version are on the
    auth middleware's skip list, so under AUTH_ENABLED=true a keyless profile
    passes every health/skew check above while EVERY real MCP/REST call 401s.
    Doctor must not report a broken office config as healthy. Returns None for
    plain-http profiles (personal — no auth expected).

    `profile`/`label` let run_doctor reuse this check for pinned profiles
    other than the active one (see _check_pins) — default behavior (active
    profile, "api-key" label) is unchanged.
    """
    try:
        if profile is None:
            profile = resolver.active_profile(cfg)
        scheme = cfg.get(profile, "scheme", fallback="http").strip().lower()
    except resolver.ConfigError as exc:
        return (label, "warn", str(exc))
    if scheme != "https":
        return None
    key = cfg.get(profile, "api_key", fallback="").strip()
    if not key:
        return (
            label, "warn",
            f"profile '{profile}' is https (auth expected) but api_key is "
            "empty -- health/version are auth-exempt so the checks above can "
            "pass while every real MCP call 401s; set api_key",
        )
    return (label, "ok", "api_key configured (redacted)")


def _check_venv_scripts(venv: Path, is_windows: bool | None = None) -> tuple[str, str, str]:
    if is_windows is None:
        is_windows = os.name == "nt"
    bindir = venv / ("Scripts" if is_windows else "bin")
    ext = ".exe" if is_windows else ""
    wanted = ("firekeep", "firekeep-shim", "firekeep-sidecar")
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


def _check_ca_expiry(cfg, profile: str | None = None,
                     label: str = "ca-expiry") -> tuple[str, str, str] | None:
    if profile is None:
        try:
            profile = resolver.active_profile(cfg)
        except resolver.ConfigError:
            return None
    if not cfg.has_section(profile):
        return None
    section = cfg[profile]
    ca_path = section.get("ca_path")
    verify_tls = section.get("verify_tls", "false").strip().lower() == "true"
    if not ca_path or not verify_tls:
        return None  # personal / plaintext: no CA to check
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


def _check_pins(cfg) -> list[tuple[str, str, str]]:
    """[pins] hygiene: unknown runtimes, charset-unsafe names (ignored at render),
    and pins referencing deleted profiles. Existence-level only — the per-profile
    api-key/CA checks for pinned profiles are appended by run_doctor."""
    if not cfg.has_section("pins"):
        return []
    out: list[tuple[str, str, str]] = []
    for runtime, profile in cfg.items("pins"):
        if runtime not in _PIN_RUNTIMES:
            out.append(("pins", "warn", f"[pins] names unknown runtime '{runtime}'"))
        elif not resolver.PIN_NAME_RE.match((profile or "").strip()):
            out.append(("pins", "warn",
                        f"pin {runtime} -> '{profile}' has an unsafe name "
                        "(must match ^[A-Za-z0-9_-]+$) and is IGNORED at render"))
        elif (profile or "").strip() in _RESERVED_SECTIONS:
            # Before has_section: reserved sections usually EXIST, so the fall-through
            # below would report a hand-edited `kiro = active` pin as ok.
            out.append(("pins", "warn",
                        f"pin {runtime} -> '{profile}' references the reserved section "
                        f"[{profile}], not a profile "
                        f"(reserved: {', '.join(_RESERVED_SECTIONS)})"))
        elif not cfg.has_section(profile):
            out.append(("pins", "warn",
                        f"pin {runtime} -> '{profile}' references a profile with no "
                        f"[{profile}] section"))
        else:
            out.append(("pins", "ok", f"{runtime} -> {profile}"))
    return out


def _check_client_version(cfg) -> tuple[str, str, str] | None:
    """Compare the installed kit against the release manifest.

    Returns None for a checkout install (no [dist] section) — that developer never used the
    bootstrap and has nothing to update from. Never 'fail': a stale-but-working client is a
    nudge, not an outage. This is the actionable half of what _check_skew gestures at:
    _check_skew compares client to CORTEX, which conflates 'my client is old' with 'the
    server moved'.
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
    if cfg is None:
        cfg = resolver.load_config(_config_path())
    results: list[tuple[str, str, str]] = []
    # Every check function below is self-contained: it catches its own
    # ConfigError/TransportError/OSError and returns a tuple rather than
    # raising, so one check's failure can never mask or short-circuit the
    # rest -- doctor always runs the full suite and reports everything.
    results.extend(_check_health(cfg))
    results.append(_check_skew(cfg))
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
    results.append(_check_config_perms(_config_path()))
    ca = _check_ca_expiry(cfg)
    if ca is not None:
        results.append(ca)
    results.append(_check_personal_mode())
    results.extend(_check_pins(cfg))
    if cfg.has_section("pins"):
        seen: set[str] = set()
        for runtime, profile in cfg.items("pins"):
            profile = (profile or "").strip()
            if profile in seen or not cfg.has_section(profile):
                continue
            seen.add(profile)
            for check in (_check_api_key, _check_ca_expiry):
                row = check(cfg, profile=profile,
                            label=f"{'api-key' if check is _check_api_key else 'ca-expiry'}"
                                  f"[pin:{runtime}->{profile}]")
                if row is not None:
                    results.append(row)
    return results


def cmd_night_shift(args) -> int:
    """Drain distill_session Relay tasks with the LOCAL model (LM Studio).

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


def cmd_doctor(args) -> int:
    try:
        _env_profile_notice()
        results = run_doctor()
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

def _exec_bootstrap(script: Path, version: str | None, base: str) -> None:
    """Replace THIS process with the bootstrap script.

    The whole point: by the time uv rewrites ~/.firekeep/venv, `firekeep` is no longer running.
    On Windows, `Scripts\\firekeep.exe` is locked while it executes and simply cannot be
    overwritten in place — every self-upgrading tool that ignores this grows a rename-dance.
    Handing off means the replacing process is uv, under sh/powershell, and nothing is held.

    `base` is REQUIRED: the bootstrap dies on an unset FIREKEEP_DIST_BASE (that is its own
    fail-loud guard), and an exec'd script inherits none of our config.
    """
    env = dict(os.environ)
    env["FIREKEEP_DIST_BASE"] = base
    if version:
        env["FIREKEEP_VERSION"] = version  # the bootstrap pins this exact release
    if os.name == "nt":
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
        # The checksum is REQUIRED here: we are about to EXECUTE this script. Verifying uv
        # inside install.sh while exec'ing an unverified install.sh would be theatre.
        # download() creates dest's parent itself — do not duplicate that here.
        script = updater.download(
            updater.bootstrap_url(base, windows=windows),
            _firekeep_home() / "bootstrap" / ("install.ps1" if windows else "install.sh"),
            sha256=manifest.bootstrap_hash_for(windows=windows),
        )
    except (resolver.ConfigError, updater.UpdateError) as exc:
        print(f"firekeep: {exc}", file=sys.stderr)
        return 1

    print(f"firekeep: updating {__version__} -> {target}")
    _exec_bootstrap(script, target, base)
    return 0  # POSIX never reaches this (execve replaced the image)


# --- parser / dispatch -------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="firekeep", description="Firekeep client")
    sub = parser.add_subparsers(dest="command")

    prof = sub.add_parser("profile", help="manage the active profile")
    prof.set_defaults(func=cmd_profile_help)
    prof_sub = prof.add_subparsers(dest="profile_command")
    use = prof_sub.add_parser("use", help="switch the active profile")
    use.add_argument("name")
    use.set_defaults(func=cmd_profile_use)
    show = prof_sub.add_parser("show", help="print active profile + resolved endpoints")
    show.set_defaults(func=cmd_profile_show)
    pin = prof_sub.add_parser("pin", help="pin a runtime to a profile (survives re-renders)")
    pin.add_argument("runtime", choices=list(_PIN_RUNTIMES))
    pin.add_argument("profile")
    pin.set_defaults(func=cmd_profile_pin)
    unpin = prof_sub.add_parser("unpin", help="remove a runtime's profile pin")
    unpin.add_argument("runtime", choices=list(_PIN_RUNTIMES))
    unpin.set_defaults(func=cmd_profile_unpin)

    ver = sub.add_parser("version", help="print client version + skew status")
    ver.set_defaults(func=cmd_version)

    inst = sub.add_parser("install", help="install/refresh the client kit")
    # default None (not "all"): an unset runtime means "ask interactively / default all",
    # which is what lets the wizard prompt for the agent. Explicit --runtime (or the
    # bootstrap's FIREKEEP_RUNTIME) still wins. Resolved in _configure.
    inst.add_argument(
        "--runtime", choices=["claude", "codex", "kiro", "opencode", "all"], default=None
    )
    # Config answers. Interactively each SEEDS its prompt's default; with
    # --non-interactive (or no TTY) each is written straight to the config.
    inst.add_argument("--agent-id", help="identity attributed to memories/sessions")
    inst.add_argument("--host", help="service host for a ports-style profile")
    inst.add_argument("--profile", choices=["personal", "office"],
                      help="profile to configure and make active")
    inst.add_argument("--dist-base", metavar="URL",
                      help="release base URL this kit came from (set by the bootstrap; "
                           "enables `firekeep update`)")
    inst.add_argument("--non-interactive", action="store_true",
                      help="never prompt (implied when stdin is not a TTY)")
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
    return func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
