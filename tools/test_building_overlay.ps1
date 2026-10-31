param([switch]$Bootstrap)

# Dedicated Windows x64 environment. No system Python, pip, profiles or global packages.
$ErrorActionPreference = 'Stop'
$overlayRepo = Split-Path -Parent $PSScriptRoot
$overlayEnv = Join-Path $overlayRepo '.venv/overlay-tests'
$overlayDownloads = Join-Path $overlayEnv 'downloads'
$overlayPython = Join-Path $overlayEnv 'python'
$overlayTestTmp = Join-Path $overlayEnv 'tmp'
foreach ($overlayDir in @((Join-Path $overlayRepo '.venv'), $overlayEnv, $overlayDownloads, $overlayPython, $overlayTestTmp)) {
    if (Test-Path -LiteralPath $overlayDir) {
        if ((Get-Item -LiteralPath $overlayDir).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Test environment cannot contain reparse points: $overlayDir"
        }
    }
    New-Item -ItemType Directory -Path $overlayDir -Force | Out-Null
}

function Get-OverlayArchive([string]$Uri, [string]$Name, [string]$Digest) {
    $archivePath = Join-Path $overlayDownloads $Name
    if (-not (Test-Path -LiteralPath $archivePath)) {
        if (-not $Bootstrap) {
            throw 'Pinned runtime archive is missing. Run tools/test_building_overlay.ps1 -Bootstrap once.'
        }
        $partialPath = "$archivePath.partial"
        try {
            Invoke-WebRequest -Uri $Uri -OutFile $partialPath -UseBasicParsing
            if ((Get-FileHash -LiteralPath $partialPath -Algorithm SHA256).Hash -ne $Digest) {
                throw "Checksum mismatch for $Name"
            }
            Move-Item -LiteralPath $partialPath -Destination $archivePath
        } finally {
            if (Test-Path -LiteralPath $partialPath) { Remove-Item -LiteralPath $partialPath }
        }
    }
    if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -ne $Digest) {
        throw "Checksum mismatch for cached $Name"
    }
    return $archivePath
}

$pythonHash = '4ACBED6DD1C744B0376E3B1CF57CE906F9DC9E95E68824584C8099A63025A3C3'
$numpyHash = 'C1F9540BE57940698ED329904DB803CF7A402F3FC200BFE599334C9BD84A40B2'
$pillowHash = 'A6444696FCE635783440B7F7A9FC24B3AD10A9EA3F0AB66C5905BE1C19CCF17D'
$pythonArchive = Get-OverlayArchive 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip' 'python.zip' $pythonHash
$numpyArchive = Get-OverlayArchive 'https://files.pythonhosted.org/packages/36/fa/8c9210162ca1b88529ab76b41ba02d433fd54fecaf6feb70ef9f124683f1/numpy-2.2.6-cp312-cp312-win_amd64.whl' 'numpy.zip' $numpyHash
$pillowArchive = Get-OverlayArchive 'https://files.pythonhosted.org/packages/8c/ce/e7dfc873bdd9828f3b6e5c2bbb74e47a98ec23cc5c74fc4e54462f0d9204/pillow-11.3.0-cp312-cp312-win_amd64.whl' 'pillow.zip' $pillowHash
$luaHash = 'C735A1CE8EE60EDB0FE71D665F1E6B7C55C6021F1D340EB8C865952C602CD36F'
$luaArchive = Get-OverlayArchive 'https://files.pythonhosted.org/packages/b8/15/5121e68aad3584e26e1425a5c9a79cd898f8a152292059e128c206ee817c/lupa-2.6-cp312-cp312-win_amd64.whl' 'lupa.zip' $luaHash
$runtimeMarker = Join-Path $overlayEnv 'runtime.sha256'
$runtimeSignature = "$pythonHash|$numpyHash|$pillowHash|$luaHash"
if (-not (Test-Path -LiteralPath $runtimeMarker) -or
    (Get-Content -LiteralPath $runtimeMarker -Raw).Trim() -ne $runtimeSignature -or
    -not (Test-Path -LiteralPath (Join-Path $overlayPython 'python.exe')) -or
    -not (Test-Path -LiteralPath (Join-Path $overlayPython 'numpy/__init__.py')) -or
    -not (Test-Path -LiteralPath (Join-Path $overlayPython 'PIL/__init__.py')) -or
    -not (Test-Path -LiteralPath (Join-Path $overlayPython 'lupa/__init__.py'))) {
    Expand-Archive -LiteralPath $pythonArchive -DestinationPath $overlayPython -Force
    Expand-Archive -LiteralPath $numpyArchive -DestinationPath $overlayPython -Force
    Expand-Archive -LiteralPath $pillowArchive -DestinationPath $overlayPython -Force
    Expand-Archive -LiteralPath $luaArchive -DestinationPath $overlayPython -Force
    Set-Content -LiteralPath $runtimeMarker -Value $runtimeSignature -Encoding Ascii
}

$overlaySavedTmp, $overlaySavedTemp = $env:TMP, $env:TEMP
$overlaySavedReal = $env:BNGE_ROADGEO_REAL
$env:TMP, $env:TEMP = $overlayTestTmp, $overlayTestTmp
$env:BNGE_ROADGEO_REAL = '0'  # synthetic fixtures only; never inspect the user's real map cache
Push-Location -LiteralPath $overlayRepo
try {
    $overlayFailed = @()
    foreach ($overlayPattern in @('test_buildingoverlay*.py', 'test_osmbuildings.py', 'test_buildingrender.py',
                                  'test_buildingrefresh.py', 'test_buildingmanual.py', 'test_buildingactivation.py',
                                  'test_buildinglive.py', 'test_buildingbatches.py', 'test_buildingretention.py', 'test_buildingrecovery.py',
                                  'test_geotiles.py', 'test_pools.py',
                                  'test_buildings.py', 'test_blockworker.py', 'test_roads.py', 'test_roadwidth.py',
                                  'test_mapgeom.py', 'test_protocol.py', 'test_protocol_agreement.py', 'test_matslots.py', 'test_staging.py', 'test_roadgeo.py')) {
        & (Join-Path $overlayPython 'python.exe') -B -m unittest discover -s server/tests -p $overlayPattern -v
        if ($LASTEXITCODE -ne 0) { $overlayFailed += $overlayPattern }
    }
    if ($overlayFailed.Count) { throw "Failed suites: $($overlayFailed -join ', ')" }
} finally {
    Pop-Location
    $env:TMP, $env:TEMP = $overlaySavedTmp, $overlaySavedTemp
    $env:BNGE_ROADGEO_REAL = $overlaySavedReal
}
