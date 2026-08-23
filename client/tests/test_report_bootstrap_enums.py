# client/tests/test_report_bootstrap_enums.py
"""The three implementations (py, sh, ps1) share one vocabulary; a literal in
either bootstrap outside report.py's tables is a silent schema drift (spec,
'Cross-language enums')."""
import re
from pathlib import Path

from firekeep_client import report

BOOTSTRAP = Path(__file__).resolve().parents[1] / "bootstrap"


def _literals(text, patterns):
    out = set()
    for pat in patterns:
        out.update(re.findall(pat, text))
    return out


def test_install_sh_literals_are_canonical():
    text = (BOOTSTRAP / "install.sh").read_text(encoding="utf-8")
    stages = _literals(text, [r'REPORT_STAGE="([a-z-]+)"'])
    assert stages, "install.sh lost its stage assignments"
    assert stages <= set(report.BOOTSTRAP_STAGES), stages - set(report.BOOTSTRAP_STAGES)
    errors = _literals(text, [r'REPORT_ERROR="([a-z0-9-]+)"'])
    assert errors <= set(report.ERRORS), errors - set(report.ERRORS)
    oses = _literals(text, [r'REPORT_OS="([a-z-]+)"'])
    assert oses <= set(report.OS_FAMILIES) | {""}


def test_install_ps1_literals_are_canonical():
    text = (BOOTSTRAP / "install.ps1").read_text(encoding="utf-8")
    stages = _literals(text, [r"\$ReportStage = '([a-z-]+)'"])
    assert stages, "install.ps1 lost its stage assignments"
    assert stages <= set(report.BOOTSTRAP_STAGES), stages - set(report.BOOTSTRAP_STAGES)
    errors = _literals(text, [r"\$ReportError = '([a-z0-9-]+)'"])
    assert errors <= set(report.ERRORS), errors - set(report.ERRORS)


def test_every_bootstrap_stage_is_assigned_somewhere():
    sh = (BOOTSTRAP / "install.sh").read_text(encoding="utf-8")
    ps1 = (BOOTSTRAP / "install.ps1").read_text(encoding="utf-8")
    for stage in report.BOOTSTRAP_STAGES:
        assert f'REPORT_STAGE="{stage}"' in sh, f"install.sh misses {stage}"
        assert f"$ReportStage = '{stage}'" in ps1, f"install.ps1 misses {stage}"
