param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$LabArgs
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv. Check your Python installation.' }
}
& $pythonPath -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('playwright') else 1)"
if ($LASTEXITCODE -ne 0) {
    & $pythonPath -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Playwright installation failed.' }
}
& $pythonPath fanbox_lab.py @LabArgs
exit $LASTEXITCODE
