$ErrorActionPreference = 'Stop'
$uiRepo = Split-Path -Parent $PSScriptRoot
$uiEnv = Join-Path $uiRepo '.venv/overlay-ui'
New-Item -ItemType Directory -Force -Path $uiEnv | Out-Null
$uiLock = Join-Path $PSScriptRoot 'overlay-ui/package-lock.json'
$uiSignature = (Get-FileHash -LiteralPath $uiLock -Algorithm SHA256).Hash
$uiMarker = Join-Path $uiEnv 'lock.sha256'
$uiSavedTmp, $uiSavedTemp = $env:TMP, $env:TEMP
$env:TMP, $env:TEMP = (Join-Path $uiRepo '.venv/overlay-tests/tmp'), (Join-Path $uiRepo '.venv/overlay-tests/tmp')
New-Item -ItemType Directory -Force -Path $env:TMP | Out-Null
Push-Location -LiteralPath $uiRepo
try {
    if (-not (Test-Path -LiteralPath $uiMarker) -or (Get-Content -LiteralPath $uiMarker -Raw).Trim() -ne $uiSignature -or
        -not (Test-Path -LiteralPath (Join-Path $uiEnv 'node_modules/@vue/compiler-sfc/package.json'))) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'overlay-ui/package.json') -Destination $uiEnv
        Copy-Item -LiteralPath $uiLock -Destination $uiEnv
        Set-Content -LiteralPath (Join-Path $uiEnv 'user.npmrc') -Value '' -Encoding Ascii
        Set-Content -LiteralPath (Join-Path $uiEnv 'global.npmrc') -Value '' -Encoding Ascii
        & npm.cmd ci --prefix $uiEnv --cache (Join-Path $uiEnv 'npm-cache') --userconfig (Join-Path $uiEnv 'user.npmrc') --globalconfig (Join-Path $uiEnv 'global.npmrc') --ignore-scripts --no-audit --no-fund --fetch-retries=0 --fetch-timeout=15000
        if ($LASTEXITCODE -ne 0) { throw 'Repository-local Vue compiler bootstrap failed' }
        Set-Content -LiteralPath $uiMarker -Value $uiSignature -Encoding Ascii
    }
    & node (Join-Path $PSScriptRoot 'test_building_ui.mjs')
    if ($LASTEXITCODE -ne 0) { throw 'Building UI checks failed' }
} finally {
    Pop-Location
    $env:TMP, $env:TEMP = $uiSavedTmp, $uiSavedTemp
}
