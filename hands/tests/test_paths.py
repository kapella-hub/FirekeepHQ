from firekeep_hands import paths


def test_everything_lives_under_hands_home(isolated_home):
    home = paths.hands_home()
    assert home == isolated_home / "hands"
    for fn in (paths.config_path, paths.policy_path, paths.broker_info_path,
               paths.machine_id_path, paths.evidence_root, paths.chrome_profile_dir):
        assert fn().is_relative_to(home)
