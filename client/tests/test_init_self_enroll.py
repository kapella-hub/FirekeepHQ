"""`firekeep init` must leave the box it just provisioned able to talk to it.

Before this, a successful server install ended with:

    firekeep: server provisioned. Use Dashboard -> Devices -> Add device,
    then run `firekeep join <code>` on each client machine.

Every word true, and unreachable. The dashboard binds to 127.0.0.1 by default
and its password lives in dashboard/.htpasswd.cred, so the prescribed next action
needed an SSH tunnel, a file read and a browser -- from the machine the user was
already sitting at, minutes after installing a client kit on it.

The mint runs through `deploy/firekeep-admin invite`, which shells into
`docker compose exec cortex-api python -m app.enroll.mint` and needs no
credential on the server box. So the tests below care about two things: that the
loop closes, and that when it cannot, the user is not left worse off than the
message it replaced.
"""
from __future__ import annotations

import json

import pytest
from firekeep_client import cli


def _server_bundle(path):
    path.mkdir(parents=True, exist_ok=True)
    for name in ("install.sh", "docker-compose.yml", ".env.example"):
        (path / name).write_text("# test\n", encoding="utf-8")
    admin = path / "deploy"
    admin.mkdir(exist_ok=True)
    (admin / "firekeep-admin").write_text("# test\n", encoding="utf-8")
    return path


@pytest.fixture
def provisioned(tmp_path, monkeypatch):
    """A server bundle whose install.sh 'succeeds', with invites stubbed."""
    root = _server_bundle(tmp_path)
    minted: list[list[str]] = []
    joined: list[str] = []

    def fake_run(command, **kwargs):
        if any(str(c).endswith("firekeep-admin") for c in command):
            minted.append([str(c) for c in command])
            label = "local" if "--local" in command else "remote"
            return type("R", (), {
                "returncode": 0,
                "stdout": json.dumps({"code": f"fk_join_{label}.sig"}),
                "stderr": "",
            })()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/bash")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(
        "firekeep_client.join.join",
        lambda code, agent_id=None: joined.append(code) or 0,
    )
    return root, minted, joined


def test_the_box_enrols_itself_over_loopback(provisioned, capsys):
    root, minted, joined = provisioned
    assert cli.main(["init", "--server-dir", str(root)]) == 0

    # --local is what makes this a loopback code rather than a tunnel code. The
    # tunnel shape is right for a laptop reaching a loopback-bound server and
    # absurd for the server enrolling itself -- it would be told to SSH to its
    # own address to reach a port already on its own loopback interface.
    assert any("--local" in call for call in minted), minted
    assert joined == ["fk_join_local.sig"], joined
    assert "this machine is connected" in capsys.readouterr().out.lower()


def test_it_hands_over_a_ready_to_paste_line_for_the_next_machine(provisioned, capsys):
    root, minted, _ = provisioned
    cli.main(["init", "--server-dir", str(root)])
    out = capsys.readouterr().out

    # The SECOND code is deliberately NOT --local: it is redeemed on a different
    # machine, over the SSH tunnel that `firekeep join` already knows how to open.
    assert sum(1 for call in minted if "--local" not in call) == 1, minted
    assert "FIREKEEP_JOIN=fk_join_remote.sig" in out
    assert "curl -fsSL" in out


def test_no_self_enroll_mints_nothing(provisioned, capsys):
    """Headless provisioning -- CI, Ansible, a golden image -- must not bake a
    device credential into the machine being built."""
    root, minted, joined = provisioned
    assert cli.main(["init", "--server-dir", str(root), "--no-self-enroll"]) == 0
    assert minted == []
    assert joined == []
    assert "server provisioned" in capsys.readouterr().out


def test_a_failed_mint_is_not_a_failed_install(tmp_path, monkeypatch, capsys):
    """The stack is up. Reporting failure here would tell the user to undo a
    server that is working, and is the single most damaging thing this could do."""
    root = _server_bundle(tmp_path)

    def fake_run(command, **kwargs):
        if any(str(c).endswith("firekeep-admin") for c in command):
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "boom"})()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/bash")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli.main(["init", "--server-dir", str(root)]) == 0
    err = capsys.readouterr().err
    assert "the server is running" in err
    assert "invite --local" in err, "the manual fallback command must be named"


def test_a_mint_that_returns_junk_is_ignored(tmp_path, monkeypatch):
    """Never hand `join` something that is not a join code: it would fail deeper
    in, with an error about the code rather than about the mint."""
    root = _server_bundle(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/bash")
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda command, **kw: type("R", (), {
            "returncode": 0, "stdout": "not json at all", "stderr": "",
        })(),
    )
    called = []
    monkeypatch.setattr(
        "firekeep_client.join.join", lambda code, agent_id=None: called.append(code) or 0
    )
    assert cli.main(["init", "--server-dir", str(root)]) == 0
    assert called == []


def test_a_wrong_shaped_code_is_rejected_before_join(tmp_path, monkeypatch):
    root = _server_bundle(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/bash")
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda command, **kw: type("R", (), {
            "returncode": 0,
            "stdout": json.dumps({"code": "totally-not-a-join-code"}),
            "stderr": "",
        })(),
    )
    called = []
    monkeypatch.setattr(
        "firekeep_client.join.join", lambda code, agent_id=None: called.append(code) or 0
    )
    assert cli.main(["init", "--server-dir", str(root)]) == 0
    assert called == []


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        ("vps-1.example.com", "vps-1"),
        ("MyBox", "MyBox"),
        ("weird name!", "weird-name"),
        ("", "server"),
        ("---", "server"),
    ],
)
def test_device_label_is_always_valid(monkeypatch, hostname, expected):
    """The minter validates ^[A-Za-z0-9_.-]{1,64}$. A hostname with anything else
    would fail the INVITE rather than the install -- a confusing place to find out."""
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: hostname)
    assert cli._default_device_label() == expected


def test_the_identity_answered_at_the_prompt_survives_enrolment(
    provisioned, monkeypatch, tmp_path
):
    """join._agent_id ranks the server's `suggested_agent_id` above the local
    config. Correct when a teammate redeems an invite an admin named the device
    in; wrong on self-enrol, where the user answered "Agent identity" moments
    earlier. Unfixed, the lab produced `agent_id=4dcd94c5792e-4dcd94c5792e` --
    the container hostname, doubled, silently replacing what was typed."""
    root, _, _ = provisioned
    config = tmp_path / "fkconfig"
    config.write_text(
        "[identity]\nagent_id = alex\n\n[server]\nkind = ports\nhost = 127.0.0.1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_config_path", lambda: config)

    passed = {}
    monkeypatch.setattr(
        "firekeep_client.join.join",
        lambda code, agent_id=None: passed.setdefault("agent_id", agent_id) or 0,
    )
    assert cli.main(["init", "--server-dir", str(root)]) == 0
    assert passed["agent_id"] == "alex"


def test_the_placeholder_identity_is_not_forced_on_the_server(
    provisioned, monkeypatch, tmp_path
):
    """CHANGEME is the absence of an answer, not an answer. Passing it as an
    override would pin every memory to the placeholder and defeat the server's
    perfectly good suggestion."""
    root, _, _ = provisioned
    config = tmp_path / "fkconfig"
    config.write_text(
        "[identity]\nagent_id = CHANGEME\n\n[server]\nkind = ports\nhost = 127.0.0.1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_config_path", lambda: config)

    passed = {}
    monkeypatch.setattr(
        "firekeep_client.join.join",
        lambda code, agent_id=None: passed.setdefault("agent_id", agent_id) or 0,
    )
    assert cli.main(["init", "--server-dir", str(root)]) == 0
    assert passed["agent_id"] is None
