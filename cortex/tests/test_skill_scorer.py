import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.skills.scorer import (
    RESOLUTION_PHRASES,
    SkillScore,
    _score_resolution_language,
    _score_session_anomaly,
    compute_skill_score,
)


def test_skill_score_dataclass_fields():
    s = SkillScore(
        session_id="s1", total=0.7, error_density=0.5,
        session_anomaly=0.3, resolution_language=0.8,
        manual_flag=False, triggered=True,
    )
    assert s.session_id == "s1"
    assert s.triggered is True


def test_resolution_phrases_not_empty():
    assert len(RESOLUTION_PHRASES) >= 5


@pytest.mark.asyncio
async def test_manual_flag_overrides_threshold():
    score = await compute_skill_score("ses1", skill_worthy=True)
    assert score.manual_flag is True
    assert score.triggered is True
    assert score.total == 1.0


@pytest.mark.asyncio
async def test_score_below_threshold_not_triggered():
    with (
        patch("app.skills.scorer._score_error_density", new=AsyncMock(return_value=0.0)),
        patch("app.skills.scorer._score_session_anomaly", new=AsyncMock(return_value=0.0)),
        patch("app.skills.scorer._score_resolution_language", new=AsyncMock(return_value=0.0)),
    ):
        score = await compute_skill_score("ses2")
    assert score.triggered is False
    assert score.total == 0.0


@pytest.mark.asyncio
async def test_score_above_threshold_triggered():
    with (
        patch("app.skills.scorer._score_error_density", new=AsyncMock(return_value=1.0)),
        patch("app.skills.scorer._score_session_anomaly", new=AsyncMock(return_value=1.0)),
        patch("app.skills.scorer._score_resolution_language", new=AsyncMock(return_value=1.0)),
    ):
        score = await compute_skill_score("ses3")
    assert score.triggered is True
    assert score.total > 0.6


@pytest.mark.asyncio
async def test_scorer_falls_back_on_error():
    """If all sub-scorers raise, total should be 0.0 and not triggered."""
    with (
        patch("app.skills.scorer._score_error_density", new=AsyncMock(side_effect=Exception("fail"))),
        patch("app.skills.scorer._score_session_anomaly", new=AsyncMock(side_effect=Exception("fail"))),
        patch("app.skills.scorer._score_resolution_language", new=AsyncMock(side_effect=Exception("fail"))),
    ):
        score = await compute_skill_score("ses4")
    assert score.triggered is False


# --- SP1a final-review FIX 3: internal-key threading on cortex->bridge calls ---

@pytest.mark.asyncio
async def test_compute_skill_score_threads_internal_key_to_bridge_scorers():
    """compute_skill_score must pass settings.FIREKEEP_INTERNAL_KEY through to
    the two bridge-calling sub-scorers (not to _score_error_density, which
    talks to Redis directly, never Bridge)."""
    mock_settings = MagicMock()
    mock_settings.RP_REDIS_URL = "redis://redis:6379/6"
    mock_settings.BRIDGE_URL = "http://bridge:8070"
    mock_settings.FIREKEEP_INTERNAL_KEY = "nxs_internal_test_key"
    mock_settings.SKILL_ERROR_DENSITY_WEIGHT = 0.30
    mock_settings.SKILL_ANOMALY_WEIGHT = 0.20
    mock_settings.SKILL_RESOLUTION_WEIGHT = 0.35
    mock_settings.SKILL_SCORE_THRESHOLD = 0.6

    with (
        patch("app.skills.scorer.get_settings", return_value=mock_settings),
        patch("app.skills.scorer._score_error_density", new=AsyncMock(return_value=0.0)),
        patch("app.skills.scorer._score_session_anomaly", new=AsyncMock(return_value=0.0)) as mock_anomaly,
        patch("app.skills.scorer._score_resolution_language", new=AsyncMock(return_value=0.0)) as mock_resolution,
    ):
        await compute_skill_score("ses5")

    mock_anomaly.assert_awaited_once_with("ses5", "http://bridge:8070", "nxs_internal_test_key")
    mock_resolution.assert_awaited_once_with("ses5", "http://bridge:8070", "nxs_internal_test_key")


@pytest.mark.asyncio
async def test_session_anomaly_sends_x_api_key_header_when_internal_key_set():
    """Both httpx.get calls inside _score_session_anomaly must carry
    X-API-Key when an internal key is supplied."""
    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)

    session_resp = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"duration_seconds": 10.0, "goal": "fix neo4j"}),
    )
    hist_resp = MagicMock(status_code=200, json=MagicMock(return_value={"sessions": []}))
    mock_http.get = AsyncMock(side_effect=[session_resp, hist_resp])

    with patch("app.skills.scorer.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        await _score_session_anomaly("ses6", "http://bridge:8070", "nxs_internal_test_key")

    assert mock_http.get.await_count == 2
    for call in mock_http.get.await_args_list:
        assert call.kwargs["headers"] == {"X-API-Key": "nxs_internal_test_key"}


@pytest.mark.asyncio
async def test_session_anomaly_omits_header_when_internal_key_unset():
    """Personal-VPS default (no internal key configured): headers stay {}."""
    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    session_resp = MagicMock(status_code=200, json=MagicMock(return_value={"duration_seconds": 0}))
    mock_http.get = AsyncMock(return_value=session_resp)

    with patch("app.skills.scorer.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        await _score_session_anomaly("ses7", "http://bridge:8070", None)

    assert mock_http.get.await_args.kwargs["headers"] == {}


@pytest.mark.asyncio
async def test_resolution_language_sends_x_api_key_header_when_internal_key_set():
    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    resp = MagicMock(status_code=200, json=MagicMock(return_value={"shadow": {}}))
    mock_http.get = AsyncMock(return_value=resp)

    with patch("app.skills.scorer.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        await _score_resolution_language("ses8", "http://bridge:8070", "nxs_internal_test_key")

    mock_http.get.assert_awaited_once_with(
        "http://bridge:8070/sessions/ses8",
        headers={"X-API-Key": "nxs_internal_test_key"},
    )
