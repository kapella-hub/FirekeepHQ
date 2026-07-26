from pathlib import Path

BOOT = Path(__file__).resolve().parent.parent / "bootstrap"

def test_install_sh_installs_symdex_wheel_by_path_not_name():
    sh = (BOOT / "install.sh").read_text()
    assert "firekeep_symdex-" in sh                       # reads the wheel name from SHA256SUMS
    assert "verify_against_sums" in sh and sh.count("uv") and "pip install" in sh
    assert "pip install firekeep-symdex" not in sh         # NEVER by name

def test_install_ps1_installs_symdex_wheel_by_path_not_name():
    ps = (BOOT / "install.ps1").read_text()
    assert "firekeep_symdex-" in ps
    assert "Verify-AgainstSums" in ps and "pip install" in ps
    assert "pip install firekeep-symdex" not in ps
