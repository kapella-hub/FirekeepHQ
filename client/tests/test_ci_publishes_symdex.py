from pathlib import Path

CI = (Path(__file__).resolve().parents[2] / ".gitlab-ci.yml").read_text()

def test_ci_builds_symdex_wheel():
    assert "cd symdex && python -m build --wheel --outdir ../dist" in CI

def test_ci_uploads_symdex_wheel():
    # The versioned upload loop must publish the symdex wheel, else the bootstrap 404s.
    assert "dist/firekeep_symdex-*.whl" in CI
