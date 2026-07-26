"""Support bundle. The property that matters is that it never ships a secret.

Driven through bash like tests/test_deploy_lib.py, reusing its interpreter
resolution -- on Windows a bare `bash` resolves to the WSL shim, which cannot
read E:\\... paths.
"""
import subprocess
from pathlib import Path

# BASH: the resolved POSIX bash. LIB: path to deploy/lib.sh. _p(): POSIX-ifies a
# path for embedding in a `bash -c` string — on Windows str(Path) yields
# backslashes MSYS cannot resolve. All three verified to exist and to import
# cleanly across test modules under pytest's default import mode.
# If BASH is None, test_deploy_lib skips at module level and that skip
# correctly propagates to this module on import.
from test_deploy_lib import BASH, LIB, _p

REPO = Path(__file__).resolve().parents[1]

SECRETS_ENV = """\
# comment line
VAULT_KEY=gAAAAABmSuperSecretFernetKeyValue=
NEO4J_PASSWORD=hunter2-do-not-leak
FIREKEEP_INTERNAL_KEY=nxs_deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbe
DASHBOARD_API_KEY=nxs_cafebabecafebabecafebabecafebabecafebabecafebab
EMBEDDING_MODEL=mxbai-embed-large
VPS_IP=203.0.113.9
"""


def _redact(tmp_path, contents: str) -> str:
    envfile = tmp_path / ".env"
    envfile.write_text(contents, encoding="utf-8")
    result = subprocess.run(
        [BASH, "-c", f'source "{_p(LIB)}"; redact_env_file "{_p(envfile)}"'],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def test_no_secret_value_survives_redaction(tmp_path):
    out = _redact(tmp_path, SECRETS_ENV)
    for secret in (
        "gAAAAABmSuperSecretFernetKeyValue=",
        "hunter2-do-not-leak",
        "nxs_deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbe",
        "nxs_cafebabecafebabecafebabecafebabecafebabecafebab",
    ):
        assert secret not in out, f"leaked: {secret}"


def test_keys_are_preserved_so_the_vendor_can_see_what_is_configured(tmp_path):
    out = _redact(tmp_path, SECRETS_ENV)
    for key in ("VAULT_KEY", "NEO4J_PASSWORD", "FIREKEEP_INTERNAL_KEY", "EMBEDDING_MODEL"):
        assert key in out, f"key {key} should survive redaction"


def test_every_value_is_redacted_including_non_secret_looking_ones(tmp_path):
    """Allow-listing 'safe' keys is how the next secret leaks. Redact all values."""
    out = _redact(tmp_path, SECRETS_ENV)
    assert "mxbai-embed-large" not in out
    assert "203.0.113.9" not in out


def test_exported_value_is_redacted(tmp_path):
    """A customer .env may legally use `export KEY=value` lines -- the
    original regex required the key to start the line and missed these,
    letting the value survive redaction verbatim."""
    out = _redact(tmp_path, "export EXPORTED_SECRET=super-secret-value\n")
    assert "super-secret-value" not in out, "leaked: super-secret-value"
    assert "EXPORTED_SECRET" in out, "key should survive redaction"


def test_comments_and_blank_lines_survive(tmp_path):
    out = _redact(tmp_path, "# comment line\n\nFOO=bar\n")
    assert "# comment line" in out


def test_missing_env_file_is_not_fatal(tmp_path):
    missing = tmp_path / "nope.env"
    result = subprocess.run(
        [BASH, "-c", f'source "{_p(LIB)}"; redact_env_file "{_p(missing)}"'],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, "a missing .env must not abort the bundle"


def test_script_exists_and_is_syntactically_valid():
    script = REPO / "deploy" / "support-bundle.sh"
    assert script.is_file(), "deploy/support-bundle.sh missing"
    subprocess.run([BASH, "-n", str(script)], check=True)


def test_script_never_cats_the_raw_env_file():
    """A regression guard with teeth: the bundle must route .env through
    redact_env_file, never copy or cat it directly."""
    source = (REPO / "deploy" / "support-bundle.sh").read_text(encoding="utf-8")
    assert "redact_env_file" in source
    for forbidden in ("cp .env", "cat .env", 'cp "$ENV_FILE"', 'cat "$ENV_FILE"'):
        assert forbidden not in source, f"bundle copies .env verbatim: {forbidden}"


def test_bundle_never_renders_interpolated_compose_config():
    """`docker compose config` substitutes .env values -- VAULT_KEY,
    NEO4J_PASSWORD and the wildcard dashboard key would land in the bundle in
    cleartext even though env.redacted.txt carefully redacted them."""
    source = (REPO / "deploy" / "support-bundle.sh").read_text(encoding="utf-8")
    for line in source.splitlines():
        if "docker compose config" in line:
            assert "--no-interpolate" in line, line
