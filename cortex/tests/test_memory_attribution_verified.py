"""Memory provenance must come from the verified principal, never X-Agent-Id."""


def test_memory_write_uses_verified_member_not_runtime_header(
    test_client, mock_graph, mock_vector, monkeypatch
):
    monkeypatch.setattr(
        "auth.principal.request_principal",
        lambda _request: {
            "workspace_id": "workspace-secure",
            "member_id": "member-alice",
            "credential_id": "cred-1",
            "scopes": ["memory:write"],
            "authenticated": True,
        },
    )

    response = test_client.post(
        "/memory/learn",
        json={"action": "changed auth", "outcome": "tests pass"},
        headers={"X-Agent-Id": "someone-else"},
    )

    assert response.status_code == 200
    metadata = mock_vector.upsert.call_args.kwargs["metadata"]
    assert metadata["workspace_id"] == "workspace-secure"
    assert metadata["member_id"] == "member-alice"
    assert metadata["agent_id"] == "someone-else"  # telemetry only
    graph_args = mock_graph.merge_action_log.call_args.kwargs
    assert graph_args["workspace_id"] == "workspace-secure"
    assert graph_args["member_id"] == "member-alice"
