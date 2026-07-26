"""Tests for webhook payload formatters."""

import json

import pytest

from app.webhook_formatters import format_payload, format_slack, format_discord, format_generic


@pytest.fixture
def sample_body():
    return {
        "event": "memory.learned",
        "payload": {"action": "Fixed auth bug", "domain": "debugging"},
        "timestamp": "2026-03-19T15:00:00Z",
        "namespace": "default",
    }


class TestFormatGeneric:
    def test_returns_json(self, sample_body):
        body_bytes, headers = format_generic(sample_body)
        assert headers["Content-Type"] == "application/json"
        parsed = json.loads(body_bytes)
        assert parsed["event"] == "memory.learned"

    def test_format_payload_default(self, sample_body):
        body_bytes, headers = format_payload(sample_body)
        assert headers["Content-Type"] == "application/json"

    def test_format_payload_explicit(self, sample_body):
        body_bytes, headers = format_payload(sample_body, "generic")
        parsed = json.loads(body_bytes)
        assert parsed["event"] == "memory.learned"


class TestFormatSlack:
    def test_returns_attachments(self, sample_body):
        body_bytes, headers = format_slack(sample_body)
        parsed = json.loads(body_bytes)
        assert "attachments" in parsed
        assert len(parsed["attachments"]) == 1
        blocks = parsed["attachments"][0]["blocks"]
        assert len(blocks) >= 1

    def test_contains_event_name(self, sample_body):
        body_bytes, _ = format_slack(sample_body)
        parsed = json.loads(body_bytes)
        text = parsed["attachments"][0]["blocks"][0]["text"]["text"]
        assert "memory.learned" in text

    def test_color_set(self, sample_body):
        body_bytes, _ = format_slack(sample_body)
        parsed = json.loads(body_bytes)
        assert "color" in parsed["attachments"][0]

    def test_format_payload_slack(self, sample_body):
        body_bytes, _ = format_payload(sample_body, "slack")
        parsed = json.loads(body_bytes)
        assert "attachments" in parsed


class TestFormatDiscord:
    def test_returns_embeds(self, sample_body):
        body_bytes, headers = format_discord(sample_body)
        parsed = json.loads(body_bytes)
        assert "embeds" in parsed
        assert len(parsed["embeds"]) == 1

    def test_embed_has_title(self, sample_body):
        body_bytes, _ = format_discord(sample_body)
        parsed = json.loads(body_bytes)
        embed = parsed["embeds"][0]
        assert "memory.learned" in embed["title"]
        assert "description" in embed
        assert "color" in embed

    def test_embed_has_fields(self, sample_body):
        body_bytes, _ = format_discord(sample_body)
        parsed = json.loads(body_bytes)
        fields = parsed["embeds"][0]["fields"]
        assert len(fields) > 0
        assert fields[0]["name"] in ("action", "domain")

    def test_format_payload_discord(self, sample_body):
        body_bytes, _ = format_payload(sample_body, "discord")
        parsed = json.loads(body_bytes)
        assert "embeds" in parsed


class TestAlertFormatting:
    def test_error_event_gets_error_color(self):
        body = {
            "event": "sentinel.alert",
            "payload": {"severity": "error", "source": "docker", "summary": "Container crashed"},
            "timestamp": "2026-03-19T15:00:00Z",
            "namespace": "default",
        }
        # Slack
        slack_bytes, _ = format_slack(body)
        slack = json.loads(slack_bytes)
        assert slack["attachments"][0]["color"] == "#f87171"

        # Discord
        discord_bytes, _ = format_discord(body)
        discord = json.loads(discord_bytes)
        assert discord["embeds"][0]["color"] == 0xF87171


class TestUnknownFormat:
    def test_falls_back_to_generic(self, sample_body):
        body_bytes, headers = format_payload(sample_body, "unknown_format")
        assert headers["Content-Type"] == "application/json"
        parsed = json.loads(body_bytes)
        assert parsed["event"] == "memory.learned"
