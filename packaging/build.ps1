$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'frontend/dist/index.html'))) {
        throw 'Build the frontend first: cd frontend; pnpm install; pnpm build'
    }
    python -m PyInstaller --noconfirm UPM.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}
