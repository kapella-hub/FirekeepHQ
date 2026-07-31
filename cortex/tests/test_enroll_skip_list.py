"""Only exact device/member enrollment bootstrap routes bypass API-key auth."""

from app.main import AUTH_SKIP_EXACT_PATHS, AUTH_SKIP_PREFIXES


def test_enrollment_paths_are_exact_only():
    assert AUTH_SKIP_EXACT_PATHS == (
        "/dashboard",
        "/dashboard/",
        "/enroll",
        "/enroll/anchor",
        "/members/invites/accept",
        "/members/invites/anchor",
    )
    assert not any("/enroll".startswith(prefix) for prefix in AUTH_SKIP_PREFIXES)
    assert not any("/members".startswith(prefix) for prefix in AUTH_SKIP_PREFIXES)
