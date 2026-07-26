"""Tests for the Confluence Celery task wiring (SP3 Task 9)."""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch
from app.collectors.confluence import run_confluence_collector


def test_task_delegates_to_engine_with_confluence_wiring():
    with patch("app.collectors.confluence.CollectorEngine") as MockEngine:
        MockEngine.return_value.run = AsyncMock(return_value={"status": "disabled"})
        with patch("app.collectors.confluence.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                COLLECTORS_ENABLED=True,
                CONFLUENCE_COLLECTOR_ENABLED=True,
                CONFLUENCE_SPACE_KEYS="OPS",
                CONFLUENCE_PAT_VAULT_KEY="confluence_pat",
            )
            # .run() on the bound Celery task executes the sync body
            result = run_confluence_collector.run()
    assert result == {"status": "disabled"}
    kwargs = MockEngine.return_value.run.await_args.kwargs
    assert kwargs["name"] == "confluence"
    assert kwargs["pat_vault_key"]  # from settings.CONFLUENCE_PAT_VAULT_KEY


def test_task_registered_and_beat_scheduled_under_matching_name():
    from app.workers.sleep_cycle import celery_app
    assert "app.collectors.confluence.run_confluence_collector" in celery_app.tasks
    assert celery_app.conf.beat_schedule["confluence-collector"]["task"] == \
        "app.collectors.confluence.run_confluence_collector"


def test_task_passes_pat_env_value_when_confluence_pat_set():
    with patch("app.collectors.confluence.CollectorEngine") as MockEngine:
        MockEngine.return_value.run = AsyncMock(return_value={"status": "disabled"})
        with patch("app.collectors.confluence.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                COLLECTORS_ENABLED=True,
                CONFLUENCE_COLLECTOR_ENABLED=True,
                CONFLUENCE_SPACE_KEYS="OPS",
                CONFLUENCE_PAT_VAULT_KEY="confluence_pat",
                CONFLUENCE_PAT="envtoken123",
            )
            run_confluence_collector.run()
    kwargs = MockEngine.return_value.run.await_args.kwargs
    assert kwargs["pat_env_value"] == "envtoken123"


def test_task_pat_env_value_none_when_confluence_pat_empty():
    with patch("app.collectors.confluence.CollectorEngine") as MockEngine:
        MockEngine.return_value.run = AsyncMock(return_value={"status": "disabled"})
        with patch("app.collectors.confluence.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                COLLECTORS_ENABLED=True,
                CONFLUENCE_COLLECTOR_ENABLED=True,
                CONFLUENCE_SPACE_KEYS="OPS",
                CONFLUENCE_PAT_VAULT_KEY="confluence_pat",
                CONFLUENCE_PAT="",
            )
            run_confluence_collector.run()
    kwargs = MockEngine.return_value.run.await_args.kwargs
    assert kwargs["pat_env_value"] is None


def test_task_pat_env_value_stripped_and_whitespace_only_is_none():
    """K8s secret mounts often carry a trailing newline; strip it so it doesn't
    land in the Bearer header, and treat a whitespace-only value as unset (→ Vault)."""
    with patch("app.collectors.confluence.CollectorEngine") as MockEngine:
        MockEngine.return_value.run = AsyncMock(return_value={"status": "disabled"})
        with patch("app.collectors.confluence.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                COLLECTORS_ENABLED=True, CONFLUENCE_COLLECTOR_ENABLED=True,
                CONFLUENCE_SPACE_KEYS="OPS", CONFLUENCE_PAT_VAULT_KEY="confluence_pat",
                CONFLUENCE_PAT="tok-with-newline\n",
            )
            run_confluence_collector.run()
    assert MockEngine.return_value.run.await_args.kwargs["pat_env_value"] == "tok-with-newline"

    with patch("app.collectors.confluence.CollectorEngine") as MockEngine:
        MockEngine.return_value.run = AsyncMock(return_value={"status": "disabled"})
        with patch("app.collectors.confluence.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                COLLECTORS_ENABLED=True, CONFLUENCE_COLLECTOR_ENABLED=True,
                CONFLUENCE_SPACE_KEYS="OPS", CONFLUENCE_PAT_VAULT_KEY="confluence_pat",
                CONFLUENCE_PAT="   \n",
            )
            run_confluence_collector.run()
    assert MockEngine.return_value.run.await_args.kwargs["pat_env_value"] is None


def test_task_no_space_keys_is_noop():
    with patch("app.collectors.confluence.CollectorEngine") as MockEngine:
        MockEngine.return_value.run = AsyncMock(return_value={"status": "disabled"})
        with patch("app.collectors.confluence.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                COLLECTORS_ENABLED=True,
                CONFLUENCE_COLLECTOR_ENABLED=True,
                CONFLUENCE_SPACE_KEYS="   ",
                CONFLUENCE_PAT_VAULT_KEY="confluence_pat",
            )
            result = run_confluence_collector.run()
    assert result == {"status": "disabled", "reason": "no space keys"}
    MockEngine.return_value.run.assert_not_awaited()
