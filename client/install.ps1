# Firekeep client kit installer — DEVELOPER path (install from a checkout).
# Teammates should use the release bootstrap instead: client/bootstrap/install.ps1, which
# brings its own Python and needs no repo. This script requires a system python >= 3.10.
# Thin entry -> python -m firekeep_client.cli install. Runs the CLI from the unpacked/checked-out
# dir so firekeep_client resolves before it is pip-installed.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
if ($env:PYTHONPATH) { $env:PYTHONPATH = "$here;$env:PYTHONPATH" } else { $env:PYTHONPATH = $here }
python -m firekeep_client.cli install @args
exit $LASTEXITCODE
