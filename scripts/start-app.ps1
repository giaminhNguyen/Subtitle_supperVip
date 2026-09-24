$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $projectRoot '.runtime'
$logDir = Join-Path $runtimeDir 'logs'
$pidFile = Join-Path $runtimeDir 'processes.json'
$backend = Join-Path $projectRoot 'backend'
$frontend = Join-Path $projectRoot 'frontend'
$venvPython = Join-Path $backend '.venv\Scripts\python.exe'

function Require-Command([string]$name, [string]$installHint) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) { throw "Khong tim thay $name. $installHint" }
}
function Test-ListeningPort([int]$port) {
    try { $client = [Net.Sockets.TcpClient]::new(); $client.Connect('127.0.0.1', $port); $client.Dispose(); return $true } catch { return $false }
}
function Start-AppProcess([string]$name, [string]$filePath, [string[]]$arguments, [string]$workingDirectory) {
    $outLog = Join-Path $logDir "$name.out.log"
    $errLog = Join-Path $logDir "$name.err.log"
    $process = Start-Process -FilePath $filePath -ArgumentList $arguments -WorkingDirectory $workingDirectory -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
    return [pscustomobject]@{ name=$name; pid=$process.Id; startedAt=$process.StartTime.ToUniversalTime().ToString('o') }
}
function Assert-LastExit([string]$step) {
    if ($LASTEXITCODE -ne 0) { throw "$step that bai (ma loi $LASTEXITCODE)." }
}

Require-Command 'py' 'Cai Python 3.12+ tu https://www.python.org/downloads/ va chon Add Python to PATH.'
Require-Command 'node' 'Cai Node.js 22+ tu https://nodejs.org/.'
Require-Command 'npm' 'Cai Node.js 22+ tu https://nodejs.org/.'
$pythonVersion = (& py -3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim(); Assert-LastExit 'Kiem tra Python'
if ([version]$pythonVersion -lt [version]'3.12') { throw "Can Python 3.12+ (dang co $pythonVersion)." }
$nodeVersion = (& node -p 'process.versions.node').Trim(); Assert-LastExit 'Kiem tra Node.js'
if ([version]$nodeVersion -lt [version]'22.0') { throw "Can Node.js 22+ (dang co $nodeVersion)." }

New-Item -ItemType Directory -Force -Path $runtimeDir,$logDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot 'data') | Out-Null
if (-not (Test-Path (Join-Path $projectRoot '.env'))) { Copy-Item (Join-Path $projectRoot '.env.example') (Join-Path $projectRoot '.env'); Write-Host 'Da tao .env. Nhap YouTube API key trong trang web sau khi ung dung mo.' }
$venvUsable = $false
if (Test-Path $venvPython) {
    & $venvPython -c "import sys" 2>$null
    $venvUsable = $LASTEXITCODE -eq 0
}
if (-not $venvUsable) {
    $venvDir = Join-Path $backend '.venv'
    if (Test-Path $venvDir) { Remove-Item -LiteralPath $venvDir -Recurse -Force }
    & py -3 -m venv $venvDir
    Assert-LastExit 'Tao Python virtual environment'
}

$requirementsHash = (Get-FileHash (Join-Path $backend 'requirements.txt')).Hash
$requirementsStamp = Join-Path $runtimeDir 'requirements.sha256'
if (-not (Test-Path $requirementsStamp) -or (Get-Content $requirementsStamp -Raw).Trim() -ne $requirementsHash) {
    & $venvPython -m pip install --upgrade pip
    Assert-LastExit 'Cap nhat pip'
    & $venvPython -m pip install -r (Join-Path $backend 'requirements.txt')
    Assert-LastExit 'Cai Python dependencies'
    Set-Content -Path $requirementsStamp -Value $requirementsHash -NoNewline
}
$packageHash = (Get-FileHash (Join-Path $frontend 'package.json')).Hash
$packageStamp = Join-Path $runtimeDir 'package.sha256'
if (-not (Test-Path (Join-Path $frontend 'node_modules')) -or -not (Test-Path $packageStamp) -or (Get-Content $packageStamp -Raw).Trim() -ne $packageHash) {
    Push-Location $frontend; npm install; Assert-LastExit 'Cai frontend dependencies'; Pop-Location
    Set-Content -Path $packageStamp -Value $packageHash -NoNewline
}

# 1) Stop what a previous run left behind, so nothing is using the database while it is backed up/migrated.
if (Test-Path $pidFile) { & (Join-Path $PSScriptRoot 'stop-app.ps1'); Start-Sleep -Seconds 1 }
Push-Location $backend
# 2) Keep last run's logs as timestamped archives and drop the old ones (never touches a log in use).
& $venvPython -m app.dbtools rotate-logs | Out-Null
# 3) Back up the existing database, but only when a migration is pending. If the backup fails we stop here and do NOT migrate.
& $venvPython -m app.dbtools pre-migrate; Assert-LastExit 'Backup database truoc khi cap nhat (chua migrate, du lieu duoc giu nguyen)'
# 4) Migrate. On failure the backup in .runtime\backups is kept; use restore-db.bat to go back.
& $venvPython -m alembic upgrade head; Assert-LastExit 'Cap nhat database (backup nam trong .runtime\backups, dung restore-db.bat de khoi phuc)'
Pop-Location
$processes = @()
if (-not (Test-ListeningPort 8000)) { $processes += Start-AppProcess 'api' $venvPython @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000') $backend }
if (-not (Test-ListeningPort 5173)) {
    $viteEntry = Join-Path $frontend 'node_modules\vite\bin\vite.js'
    $processes += Start-AppProcess 'web' (Get-Command node).Source @($viteEntry,'--host','127.0.0.1') $frontend
}
$processes += Start-AppProcess 'worker' $venvPython @('-m','app.worker') $backend
$processes | ConvertTo-Json | Set-Content -Path $pidFile -Encoding utf8

$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) { try { if ((Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8000/health' -TimeoutSec 2).StatusCode -eq 200) { $ready=$true; break } } catch {}; Start-Sleep -Seconds 1 }
if (-not $ready) { throw "API chua khoi dong. Xem log tai $logDir" }
Start-Process 'http://localhost:5173'
Write-Host "Da khoi dong ung dung. Mo http://localhost:5173"
Write-Host "Log: $logDir"
