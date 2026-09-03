"""Beat/include pin for the nightly skill-ladder pass (mirrors test_dreams_task.py's
`test_task_is_registered_on_beat_with_matching_name`)."""
from datetime import timedelta

from app.config import get_settings


def test_ladder_registered_on_beat_with_matching_schedule():
    from app.workers.sleep_cycle import celery_app

    name = "app.skills.ladder.run_skill_ladder"
    assert "app.skills.ladder" in celery_app.conf.include
    entry = celery_app.conf.beat_schedule["skill-ladder"]
    assert entry["task"] == name
    s = get_settings()
    assert entry["schedule"] == timedelta(hours=s.SKILL_LADDER_SCHEDULE_HOURS)
