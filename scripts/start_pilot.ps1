$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$appPath = Join-Path $projectRoot 'app.py'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Project .venv is missing. Install the project dependencies first.'
}
$previousPythonPath = $env:PYTHONPATH
$previousPythonUtf8 = $env:PYTHONUTF8
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONUTF8 = '1'
try {
    & $pythonPath -m streamlit run $appPath --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
    $streamlitExit = $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $previousPythonPath
    $env:PYTHONUTF8 = $previousPythonUtf8
}
exit $streamlitExit
