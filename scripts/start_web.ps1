$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Project .venv is missing. Install the project dependencies first.'
}
$previousPythonPath = $env:PYTHONPATH
$previousPythonUtf8 = $env:PYTHONUTF8
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONUTF8 = '1'
try {
    & $pythonPath -m uvicorn web.server:app --app-dir $projectRoot --host 127.0.0.1 --port 8600
    $webExit = $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $previousPythonPath
    $env:PYTHONUTF8 = $previousPythonUtf8
}
exit $webExit
