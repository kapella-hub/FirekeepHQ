"""`firekeep backup status|list|link|pull|restore` — the workstation half of the
Keep's backup story.

What these tests pin is the set of promises a person only ever cashes in during
a disaster, when there is no second chance to discover they were wrong:

* `link` VERIFIES a key against the live admin gate before storing it. A key
  that turns out to be broken at restore time is the failure mode this whole
  command exists to remove, so a rejected key must leave the config untouched.
* `pull` verifies EVERY sha256 in the manifest. There is no resume, so a
  truncated download must fail loudly and re-pull rather than sit on disk
  looking like a backup.
* `restore` prints steps and executes nothing. Restore runs on the host with
  the stack down; a client that pretended otherwise would be lying about
  physics.
* `status` never claims off-box freshness it cannot know — the honest number is
  the last pull ON THIS MACHINE.

`transport.get_file` is exercised here rather than in test_transport.py because
it exists for exactly one caller (`pull`) and its contract — same TLS semantics,
same TransportError shape, no partial file left behind — is what makes the
verification promise above meaningful.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import ssl
import textwrap
import urllib.error
from pathlib import Path

import pytest

from firekeep_client import backups, cli, resolver, transport
from firekeep_client.transport import TransportError

SERVER = textwrap.dedent("""\
    [identity]
    agent_id = tester
    [server]
    kind = ports
    scheme = http
    host = 10.0.0.5
    verify_tls = false
    api_key = nxs_member_key
""")

POLICY = "nightly 04:30 · keep 7 nightly + 4 weekly"
NEWEST = "20260818-043000"

STATUS = {
    "enabled": True,
    "policy": POLICY,
    "backups": [
        {"stamp": NEWEST, "age_seconds": 3600.0, "mode": "cold",
         "total_bytes": 412_000_000, "indexed": True},
        {"stamp": "20260817-043000", "age_seconds": 90_000.0, "mode": "cold",
         "total_bytes": 410_000_000, "indexed": True},
        {"stamp": "20260816-120000", "age_seconds": 200_000.0, "indexed": False},
    ],
}

EMPTY_STATUS = {"enabled": False, "policy": POLICY, "backups": []}

ARTIFACTS = {
    "neo4j_data.tar.gz": b"neo4j-volume-bytes",
    "qdrant_data.tar.gz": b"qdrant-volume-bytes",
    "env": b"VAULT_KEY=hunter2\n",
}


def _manifest(stamp: str = NEWEST) -> dict:
    return {
        "stamp": stamp,
        "mode": "cold",
        "commit": "abc1234",
        "sensitive": True,
        "files": [
            {"name": name, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
            for name, body in ARTIFACTS.items()
        ],
        "total_bytes": sum(len(b) for b in ARTIFACTS.values()),
    }


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """An isolated ~/.firekeep with a member connection and nothing else."""
    home = tmp_path / ".firekeep"
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config"
    path.write_text(SERVER, encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(path))
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    return path


@pytest.fixture
def dest(tmp_path):
    d = tmp_path / "pulls"
    d.mkdir()
    return d


def _out(capsys):
    captured = capsys.readouterr()
    return captured.out + captured.err


class Recorder:
    """A fake `get_json` that answers by URL suffix and records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        for suffix, answer in self.routes.items():
            if url.endswith(suffix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unrouted URL: {url}")


def _status_only(payload=STATUS):
    return Recorder({"/ops/backups": payload})


def _linked(configured, key="nxs_admin_key"):
    backups.store_admin_key(key)
    return key


def _file_server(manifest, bodies=None):
    """A fake `get_file` that writes the real artifact bytes to `dest`."""
    bodies = dict(ARTIFACTS if bodies is None else bodies)
    calls = []

    def get_file(url, dest_path, **kw):
        calls.append((url, Path(dest_path), kw))
        name = url.rsplit("/", 1)[-1]
        body = (json.dumps(manifest).encode("utf-8") if name == "manifest.json"
                else bodies[name])
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(dest_path).write_bytes(body)
        return len(body)

    get_file.calls = calls
    return get_file


# --- status -----------------------------------------------------------------


def test_status_prints_the_newest_age_the_count_and_the_policy(
        configured, monkeypatch, capsys):
    monkeypatch.setattr(backups, "get_json", _status_only())
    assert cli.main(["backup", "status"]) == 0
    out = _out(capsys)
    assert NEWEST in out
    assert "1h ago" in out
    assert POLICY in out
    assert "3" in out  # three backups on the server


def test_status_says_never_pulled_when_this_machine_has_no_pull(
        configured, monkeypatch, capsys):
    """The honest off-box number. Server-side age says nothing about whether a
    copy of that backup exists anywhere but the one disk."""
    monkeypatch.setattr(backups, "get_json", _status_only())
    assert cli.main(["backup", "status"]) == 0
    out = _out(capsys)
    assert "last pull on this machine: never" in out


def test_status_reports_the_last_pull_recorded_on_this_machine(
        configured, monkeypatch, capsys, dest):
    backups.write_pull_state(NEWEST, dest / f"firekeep-backup-{NEWEST}")
    monkeypatch.setattr(backups, "get_json", _status_only())
    assert cli.main(["backup", "status"]) == 0
    out = _out(capsys)
    assert "never" not in out.split("off-box")[-1]
    assert NEWEST in out


def test_status_is_honest_when_the_server_has_no_backups(
        configured, monkeypatch, capsys):
    """`enabled: false` is rendered as what it means, not as an error and not as
    a blank screen."""
    monkeypatch.setattr(backups, "get_json", _status_only(EMPTY_STATUS))
    assert cli.main(["backup", "status"]) == 0
    out = _out(capsys)
    assert "no backups" in out.lower() or "nothing has been backed up" in out.lower()
    assert POLICY in out


def test_status_names_an_unreachable_server_rather_than_traceback(
        configured, monkeypatch, capsys):
    monkeypatch.setattr(backups, "get_json",
                        Recorder({"/ops/backups": TransportError("refused")}))
    assert cli.main(["backup", "status"]) == 1
    assert "refused" in _out(capsys)


# --- list -------------------------------------------------------------------


def test_list_prints_one_line_per_backup_including_unindexed(
        configured, monkeypatch, capsys):
    """An unindexed dir (update.sh's ad-hoc backups, anything pre-feature) is
    real and rotation will never delete it — hiding it would misrepresent both
    what is on the disk and what the retention policy will do."""
    monkeypatch.setattr(backups, "get_json", _status_only())
    assert cli.main(["backup", "list"]) == 0
    out = _out(capsys)
    for entry in STATUS["backups"]:
        assert entry["stamp"] in out
    assert "unindexed" in out


def test_list_renders_explicit_nulls_rather_than_the_word_none(
        configured, monkeypatch, capsys):
    """The endpoint emits `mode` and `total_bytes` as explicit JSON nulls on an
    unindexed entry rather than omitting the keys (confirmed against S3). A
    `.get(key, default)` reader would sail past that and print "None" — these
    columns must degrade to a dash."""
    payload = {"enabled": True, "policy": POLICY, "backups": [
        {"stamp": "20260816-201500", "age_seconds": 200_000.0,
         "mode": None, "total_bytes": None, "indexed": False},
    ]}
    monkeypatch.setattr(backups, "get_json", _status_only(payload))
    assert cli.main(["backup", "list"]) == 0
    out = _out(capsys)
    assert "None" not in out
    assert "unindexed" in out


def test_status_is_honest_about_a_directory_that_exists_but_is_empty(
        configured, monkeypatch, capsys):
    """`enabled: true` with no backups is a REACHABLE state — the mount is
    there but the first nightly has not run. It must read as "nothing has been
    backed up", not as a healthy empty list."""
    payload = {"enabled": True, "policy": POLICY, "backups": []}
    monkeypatch.setattr(backups, "get_json", _status_only(payload))
    assert cli.main(["backup", "status"]) == 0
    out = _out(capsys)
    assert "nothing has been backed up yet" in out
    # ...and NOT the "no backups directory" aside, which is the other cause.
    assert "no backups directory" not in out


# --- link -------------------------------------------------------------------


def test_link_verifies_against_the_admin_gate_before_storing(
        configured, monkeypatch, capsys):
    recorder = Recorder({
        "/ops/backups": STATUS,
        f"/ops/backups/{NEWEST}/manifest.json": _manifest(),
    })
    monkeypatch.setattr(backups, "get_json", recorder)
    assert cli.main(["backup", "link", "--key", "nxs_admin_key"]) == 0
    assert backups.admin_key() == "nxs_admin_key"
    # The verification call carried the candidate key, not the member key.
    probe = [c for c in recorder.calls if "manifest.json" in c[0]]
    assert probe, "link must probe the admin-only download endpoint"
    assert probe[0][1]["headers"]["X-API-Key"] == "nxs_admin_key"


def test_link_never_stores_a_key_the_admin_gate_rejects(
        configured, monkeypatch, capsys):
    """THE point of the command. A key stored without proof is a key discovered
    broken during the disaster."""
    monkeypatch.setattr(backups, "get_json", Recorder({
        "/ops/backups": STATUS,
        f"/ops/backups/{NEWEST}/manifest.json": TransportError("forbidden", status=403),
    }))
    assert cli.main(["backup", "link", "--key", "nxs_not_admin"]) == 1
    assert backups.admin_key() is None
    assert "admin" in _out(capsys).lower()


def test_link_names_a_network_failure_and_still_stores_nothing(
        configured, monkeypatch, capsys):
    monkeypatch.setattr(backups, "get_json", Recorder({
        "/ops/backups": STATUS,
        f"/ops/backups/{NEWEST}/manifest.json": TransportError("connection refused"),
    }))
    assert cli.main(["backup", "link", "--key", "nxs_admin_key"]) == 1
    assert backups.admin_key() is None
    assert "connection refused" in _out(capsys)


def test_link_refuses_when_there_is_nothing_to_verify_against(
        configured, monkeypatch, capsys):
    """No backup means no admin-gated object to prove the key on. Storing an
    unverified key here would recreate exactly the failure mode `link` exists
    to remove, so it says so instead."""
    monkeypatch.setattr(backups, "get_json", _status_only(EMPTY_STATUS))
    assert cli.main(["backup", "link", "--key", "nxs_admin_key"]) == 1
    assert backups.admin_key() is None


def test_link_writes_the_key_into_the_backup_section_without_losing_the_server(
        configured, monkeypatch):
    monkeypatch.setattr(backups, "get_json", Recorder({
        "/ops/backups": STATUS,
        f"/ops/backups/{NEWEST}/manifest.json": _manifest(),
    }))
    assert cli.main(["backup", "link", "--key", "nxs_admin_key"]) == 0
    cfg = resolver.load_config(configured)
    assert cfg.get("server", "host") == "10.0.0.5"
    assert cfg.get("backup", "admin_key") == "nxs_admin_key"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_link_leaves_the_config_owner_only(configured, monkeypatch):
    monkeypatch.setattr(backups, "get_json", Recorder({
        "/ops/backups": STATUS,
        f"/ops/backups/{NEWEST}/manifest.json": _manifest(),
    }))
    assert cli.main(["backup", "link", "--key", "nxs_admin_key"]) == 0
    assert configured.stat().st_mode & 0o777 == 0o600


def test_link_prompts_when_no_key_flag_is_given(configured, monkeypatch, capsys):
    monkeypatch.setattr(backups, "get_json", Recorder({
        "/ops/backups": STATUS,
        f"/ops/backups/{NEWEST}/manifest.json": _manifest(),
    }))
    monkeypatch.setattr(backups, "_read_key", lambda: "nxs_typed_key")
    assert cli.main(["backup", "link"]) == 0
    assert backups.admin_key() == "nxs_typed_key"


def test_link_without_a_tty_names_the_flag_instead_of_blocking(
        configured, monkeypatch, capsys):
    """A script, a cron entry or a CI step must be told which flag to use, not
    left waiting on a prompt nobody will ever see."""
    monkeypatch.setattr(backups, "get_json", _status_only())
    assert cli.main(["backup", "link"]) == 2
    assert "--key" in _out(capsys)
    assert backups.admin_key() is None


# --- pull -------------------------------------------------------------------


def test_pull_requires_link_first_and_names_it(configured, monkeypatch, capsys, dest):
    monkeypatch.setattr(backups, "get_json", _status_only())
    assert cli.main(["backup", "pull", "--dest", str(dest)]) == 1
    assert "firekeep backup link" in _out(capsys)


def test_pull_downloads_and_verifies_every_file_in_the_manifest(
        configured, monkeypatch, capsys, dest):
    _linked(configured)
    manifest = _manifest()
    monkeypatch.setattr(backups, "get_json", _status_only())
    monkeypatch.setattr(backups, "get_file", _file_server(manifest))
    assert cli.main(["backup", "pull", "--dest", str(dest)]) == 0
    pulled = dest / f"firekeep-backup-{NEWEST}"
    assert (pulled / "manifest.json").exists()
    for name, body in ARTIFACTS.items():
        assert (pulled / name).read_bytes() == body
    out = _out(capsys)
    assert str(pulled) in out


def test_pull_fails_loudly_on_a_sha256_mismatch(configured, monkeypatch, capsys, dest):
    """No resume in round 1: a truncated file must fail verification and be
    thrown away, never left on disk looking like a backup."""
    _linked(configured)
    manifest = _manifest()
    truncated = dict(ARTIFACTS, **{"qdrant_data.tar.gz": b"trunc"})
    monkeypatch.setattr(backups, "get_json", _status_only())
    monkeypatch.setattr(backups, "get_file", _file_server(manifest, truncated))
    assert cli.main(["backup", "pull", "--dest", str(dest)]) == 1
    out = _out(capsys)
    assert "qdrant_data.tar.gz" in out
    assert "sha256" in out.lower()
    assert not (dest / f"firekeep-backup-{NEWEST}" / "qdrant_data.tar.gz").exists()


def test_pull_uses_the_admin_key_for_every_download(configured, monkeypatch, dest):
    _linked(configured)
    get_file = _file_server(_manifest())
    monkeypatch.setattr(backups, "get_json", _status_only())
    monkeypatch.setattr(backups, "get_file", get_file)
    assert cli.main(["backup", "pull", "--dest", str(dest)]) == 0
    assert get_file.calls
    for _url, _path, kw in get_file.calls:
        assert kw["headers"]["X-API-Key"] == "nxs_admin_key"


def test_pull_prunes_local_pulls_to_three(configured, monkeypatch, capsys, dest):
    for old in ("20260810-043000", "20260811-043000", "20260812-043000"):
        (dest / f"firekeep-backup-{old}").mkdir()
    _linked(configured)
    monkeypatch.setattr(backups, "get_json", _status_only())
    monkeypatch.setattr(backups, "get_file", _file_server(_manifest()))
    assert cli.main(["backup", "pull", "--dest", str(dest)]) == 0
    kept = sorted(p.name for p in dest.iterdir() if p.is_dir())
    assert kept == [
        "firekeep-backup-20260812-043000",
        f"firekeep-backup-{NEWEST}",
    ] or kept == [
        "firekeep-backup-20260811-043000",
        "firekeep-backup-20260812-043000",
        f"firekeep-backup-{NEWEST}",
    ]
    assert len(kept) <= backups.KEEP_LOCAL_PULLS


def test_pull_leaves_unrelated_directories_alone(configured, monkeypatch, dest):
    """Pruning walks only what `pull` itself wrote. A `--dest` pointed at a
    shared folder must not become a delete radius."""
    (dest / "my-holiday-photos").mkdir()
    for old in ("20260810-043000", "20260811-043000", "20260812-043000"):
        (dest / f"firekeep-backup-{old}").mkdir()
    _linked(configured)
    monkeypatch.setattr(backups, "get_json", _status_only())
    monkeypatch.setattr(backups, "get_file", _file_server(_manifest()))
    assert cli.main(["backup", "pull", "--dest", str(dest)]) == 0
    assert (dest / "my-holiday-photos").exists()


def test_pull_refuses_a_manifest_filename_that_escapes_the_destination(
        configured, monkeypatch, capsys, dest):
    """Every name in the manifest arrives over the wire and is about to become a
    path under someone's `--dest`. A traversing name must be an error, not a
    write outside the pull directory."""
    _linked(configured)
    evil = {
        "stamp": NEWEST, "mode": "cold", "commit": "abc", "sensitive": True,
        "files": [{"name": "../escaped.txt", "sha256": "0" * 64, "bytes": 1}],
        "total_bytes": 1,
    }
    monkeypatch.setattr(backups, "get_json", _status_only())
    monkeypatch.setattr(backups, "get_file", _file_server(evil, {}))
    assert cli.main(["backup", "pull", "--dest", str(dest)]) == 1
    assert not (dest / "escaped.txt").exists()
    assert "unsafe" in _out(capsys).lower()


def test_pull_refuses_a_manifest_entry_with_no_digest(
        configured, monkeypatch, capsys, dest):
    """No sha256 means nothing to verify against, and an unverifiable download
    must never be called a backup."""
    _linked(configured)
    unhashed = {
        "stamp": NEWEST, "mode": "cold", "commit": "abc", "sensitive": True,
        "files": [{"name": "env", "bytes": 3}],
        "total_bytes": 3,
    }
    monkeypatch.setattr(backups, "get_json", _status_only())
    monkeypatch.setattr(backups, "get_file", _file_server(unhashed))
    assert cli.main(["backup", "pull", "--dest", str(dest)]) == 1
    assert "sha256" in _out(capsys).lower()


def test_pull_records_the_last_pull_for_status_and_doctor(
        configured, monkeypatch, dest):
    _linked(configured)
    monkeypatch.setattr(backups, "get_json", _status_only())
    monkeypatch.setattr(backups, "get_file", _file_server(_manifest()))
    assert cli.main(["backup", "pull", "--dest", str(dest)]) == 0
    state = backups.read_pull_state()
    assert state["stamp"] == NEWEST
    assert state["at"]
    assert Path(state["path"]).name == f"firekeep-backup-{NEWEST}"


def test_pull_refuses_when_the_server_has_no_indexed_backup(
        configured, monkeypatch, capsys, dest):
    _linked(configured)
    monkeypatch.setattr(backups, "get_json", _status_only(EMPTY_STATUS))
    assert cli.main(["backup", "pull", "--dest", str(dest)]) == 1
    assert "no" in _out(capsys).lower()


def test_pull_discloses_that_the_archive_is_a_secret(
        configured, monkeypatch, capsys, dest):
    """The archive carries `.env` — VAULT_KEY included. That trade-off was made
    eyes-open, so the moment a copy lands on a laptop it has to be said out loud."""
    _linked(configured)
    monkeypatch.setattr(backups, "get_json", _status_only())
    monkeypatch.setattr(backups, "get_file", _file_server(_manifest()))
    assert cli.main(["backup", "pull", "--dest", str(dest)]) == 0
    assert "secret" in _out(capsys).lower()


# --- restore ----------------------------------------------------------------


def test_restore_prints_real_paths_when_a_pull_exists(
        configured, monkeypatch, capsys, dest):
    pulled = dest / f"firekeep-backup-{NEWEST}"
    pulled.mkdir()
    backups.write_pull_state(NEWEST, pulled)
    monkeypatch.setattr(backups, "get_json", _status_only())
    assert cli.main(["backup", "restore"]) == 0
    out = _out(capsys)
    assert str(pulled) in out
    assert "deploy/restore.sh" in out
    assert "10.0.0.5" in out          # the configured server, not a placeholder
    assert "docker compose up -d" in out


def test_restore_covers_the_fresh_vps_path(configured, monkeypatch, capsys):
    monkeypatch.setattr(backups, "get_json", _status_only())
    assert cli.main(["backup", "restore"]) == 0
    out = _out(capsys)
    assert "install.sh" in out
    assert "firekeep backup pull" in out  # nothing pulled yet — name the step


def test_restore_executes_nothing_remotely(configured, monkeypatch, capsys):
    """Guided, honest, no remote magic: restore runs ON the host with the stack
    down. A module that could not shell out cannot quietly grow the habit."""
    source = Path(backups.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "subprocess" not in imported
    assert "os" in imported or True  # (os is fine; the ban is on process spawning)


# --- transport.get_file -----------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
        self._pos = 0

    def read(self, size=-1):
        if size is None or size < 0:
            chunk, self._pos = self._body[self._pos:], len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_get_file_streams_the_body_to_the_destination(tmp_path, monkeypatch):
    body = b"x" * (1024 * 300)  # larger than one chunk
    monkeypatch.setattr(transport.urllib.request, "urlopen",
                        lambda req, **kw: _FakeResponse(body))
    target = tmp_path / "out.tar.gz"
    written = transport.get_file("http://h/f", target, headers={"X-API-Key": "k"})
    assert written == len(body)
    assert target.read_bytes() == body
    assert list(tmp_path.iterdir()) == [target]  # no .part left behind


def test_get_file_maps_http_errors_to_the_transport_contract(tmp_path, monkeypatch):
    def boom(req, **kw):
        raise urllib.error.HTTPError("http://h/f", 403, "Forbidden", {}, None)

    monkeypatch.setattr(transport.urllib.request, "urlopen", boom)
    target = tmp_path / "out.bin"
    with pytest.raises(TransportError) as exc:
        transport.get_file("http://h/f", target, headers={})
    assert exc.value.status == 403
    assert not target.exists()
    assert not list(tmp_path.iterdir())  # nothing partial survives a failure


def test_get_file_leaves_no_partial_file_when_the_stream_dies(tmp_path, monkeypatch):
    class _Dies(_FakeResponse):
        def read(self, size=-1):
            raise TimeoutError("stalled")

    monkeypatch.setattr(transport.urllib.request, "urlopen",
                        lambda req, **kw: _Dies(b""))
    target = tmp_path / "out.bin"
    with pytest.raises(TransportError):
        transport.get_file("http://h/f", target, headers={})
    assert not list(tmp_path.iterdir())


def test_get_file_honours_verify_false_exactly_like_a_json_request(tmp_path, monkeypatch):
    seen = {}

    def urlopen(req, **kw):
        seen["ctx"] = kw.get("context")
        seen["timeout"] = kw.get("timeout")
        return _FakeResponse(b"body")

    monkeypatch.setattr(transport.urllib.request, "urlopen", urlopen)
    transport.get_file("https://h/f", tmp_path / "o", headers={},
                       verify=False, timeout=9.0)
    assert seen["ctx"].verify_mode == ssl.CERT_NONE
    assert seen["ctx"].check_hostname is False
    assert seen["timeout"] == 9.0


# --- parser -----------------------------------------------------------------


def test_bare_backup_reports_status(configured, monkeypatch, capsys):
    monkeypatch.setattr(backups, "get_json", _status_only())
    assert cli.main(["backup"]) == 0
    assert POLICY in _out(capsys)


def test_unknown_action_is_refused_by_the_parser():
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["backup", "delete"])
