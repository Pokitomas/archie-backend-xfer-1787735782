param(
  [Parameter(Mandatory=$true)][string]$Token,
  [Parameter(Mandatory=$true)][string]$Topic
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$root = Join-Path $env:LOCALAPPDATA 'ARCHIE\field'
$stagingRoot = Join-Path $root 'staging'
$current = Join-Path $root 'current'
$logs = Join-Path $root 'logs'
New-Item -ItemType Directory -Force $stagingRoot,$logs | Out-Null
$stage = Join-Path $stagingRoot ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $stage | Out-Null

$repo = 'Pokitomas/archie-backend-xfer-1787735782'
$head = Invoke-RestMethod -UseBasicParsing -Headers @{ 'User-Agent'='archie-field-bootstrap/2'; 'Cache-Control'='no-cache' } ('https://api.github.com/repos/' + $repo + '/commits/master?t=' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
$sha = [string]$head.sha
if ($sha.Length -lt 40) { throw 'could not resolve coherent repository head' }
$raw = 'https://raw.githubusercontent.com/' + $repo + '/' + $sha + '/'
$files = @('phone_bridge.py','phone_bridge_field.py','field_transport.py')

function Start-FieldProcess([string]$dir, [string]$entryName) {
  $entry = Join-Path $dir $entryName
  if (-not (Test-Path $entry)) { throw ('missing entry ' + $entryName) }
  $out = Join-Path $logs 'field.out.log'
  $err = Join-Path $logs 'field.err.log'
  Remove-Item $out,$err -Force -ErrorAction SilentlyContinue
  return Start-Process -FilePath 'py' -ArgumentList @('-3.12',$entry,'--token',$Token,'--topic',$Topic) -WorkingDirectory $dir -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
}

function Wait-FieldHealth([int]$milliseconds=6500) {
  $deadline = [Environment]::TickCount64 + $milliseconds
  while ([Environment]::TickCount64 -lt $deadline) {
    try {
      $h = Invoke-RestMethod -UseBasicParsing -Headers @{ Authorization=('Bearer ' + $Token); 'Cache-Control'='no-cache' } 'http://127.0.0.1:8844/api/health' -TimeoutSec 1
      if ($h.ok -eq $true) { return $true }
    } catch {}
    Start-Sleep -Milliseconds 120
  }
  return $false
}

try {
  foreach ($name in $files) {
    Invoke-WebRequest -UseBasicParsing ($raw + $name) -OutFile (Join-Path $stage $name)
  }

  Push-Location $stage
  try {
    & py -3.12 -m py_compile @files
    if ($LASTEXITCODE -ne 0) { throw 'py_compile failed' }
    $probe = @"
import json
import phone_bridge as base
import phone_bridge_field
base.TOKEN = 'x' * 40
value = base.run_selftest()
print(json.dumps(value, separators=(',', ':')))
raise SystemExit(0 if value.get('ok') else 3)
"@
    & py -3.12 -c $probe
    if ($LASTEXITCODE -ne 0) { throw 'controller/screen preflight failed' }
  }
  finally { Pop-Location }

  # Only a coherent, importable candidate that already passed the real
  # controller+screen probe can replace the current aperture.
  $backup = Join-Path $root 'previous'
  if (Test-Path $backup) { Remove-Item -Recurse -Force $backup }
  if (Test-Path $current) { Move-Item -Force $current $backup }
  Move-Item -Force $stage $current

  Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match '^python' -and $_.CommandLine -match 'phone_bridge(?:_fast|_field)?\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match '^cloudflared' -and $_.CommandLine -match '127\.0\.0\.1:8844' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

  $proc = Start-FieldProcess $current 'phone_bridge_field.py'
  if (-not (Wait-FieldHealth 7000)) {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    if (Test-Path $backup) {
      Remove-Item -Recurse -Force $current -ErrorAction SilentlyContinue
      Move-Item -Force $backup $current
      $rollbackEntry = if (Test-Path (Join-Path $current 'phone_bridge_field.py')) { 'phone_bridge_field.py' } else { 'phone_bridge.py' }
      $null = Start-FieldProcess $current $rollbackEntry
    }
    throw 'candidate started but failed authenticated local health; rolled back'
  }

  [ordered]@{
    installed_at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    commit = $sha
    current = $current
    entry = 'phone_bridge_field.py'
    pid = $proc.Id
    files = $files
    status = 'healthy'
  } | ConvertTo-Json -Compress | Set-Content -Encoding UTF8 (Join-Path $root 'state.json')

  Get-ChildItem $stagingRoot -Directory -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}
catch {
  try { if (Test-Path $stage) { Remove-Item -Recurse -Force $stage } } catch {}
  [ordered]@{
    failed_at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    commit = $sha
    status = 'candidate-refused'
    error = $_.Exception.Message
  } | ConvertTo-Json -Compress | Set-Content -Encoding UTF8 (Join-Path $root 'last_failure.json')
  throw
}
