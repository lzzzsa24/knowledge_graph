$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$oldPythonPath = $env:PYTHONPATH
$oldPythonUtf8 = $env:PYTHONUTF8
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONUTF8 = '1'
try {
    & $pythonPath -m kg_pilot.ingestion @args
    $intakeExit = $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $oldPythonPath
    $env:PYTHONUTF8 = $oldPythonUtf8
}
exit $intakeExit
