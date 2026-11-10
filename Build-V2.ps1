param(
    [string]$Wheelhouse = (Join-Path $PSScriptRoot 'vendor/wheelhouse'),
    [string]$Python = 'python',
    [string]$ReleaseDir = (Join-Path $PSScriptRoot 'releases/v2-windows-x64')
)
$ErrorActionPreference = 'Stop'
$taskBuild = Join-Path $env:TEMP ('sas-v2-build-' + [guid]::NewGuid().ToString('N'))
& $Python -m venv (Join-Path $taskBuild 'venv')
if ($LASTEXITCODE -ne 0) { throw 'Cannot create build environment' }
$taskPython = Join-Path $taskBuild 'venv/Scripts/python.exe'
& $taskPython -m pip --isolated install --no-index --only-binary=:all: --require-hashes --find-links $Wheelhouse -r (Join-Path $PSScriptRoot 'packaging_v2/requirements-build.lock')
if ($LASTEXITCODE -ne 0) { throw 'Offline dependency installation failed' }
$taskRelease = $ReleaseDir
& $taskPython -m PyInstaller --noconfirm --distpath $taskRelease --workpath (Join-Path $taskBuild 'build') (Join-Path $PSScriptRoot 'packaging_v2/SasSqlMigrationV2.spec')
if ($LASTEXITCODE -ne 0) { throw 'Executable build failed' }
$taskExe = Join-Path $taskRelease 'SasSqlMigrationV2-local.exe'
$taskReport = Join-Path $taskRelease 'smoke-report.json'
$taskProcess = Start-Process -FilePath $taskExe -ArgumentList @('--smoke-test', '--report', ('"' + $taskReport + '"')) -WindowStyle Hidden -PassThru
if (-not $taskProcess.WaitForExit(120000)) { throw 'Packaged smoke test timeout' }
if ($taskProcess.ExitCode -ne 0 -or -not (Get-Content -LiteralPath $taskReport -Raw | ConvertFrom-Json).passed) { throw 'Packaged smoke failed' }
Write-Output $taskExe
