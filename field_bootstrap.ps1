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

$raw = 'https://raw.githubusercontent.com/Pokitomas/archie-backend-xfer-1787735782/master/'
$files = @(
  'phone_bridge.py',
  'phone_bridge_fast.py',
  'phone_bridge_field.py',
  'acoustic_field.py',
  'activity_field.py',
  'live_field.py'
)

try {
  foreach ($name in $files) {
    Invoke-WebRequest -UseBasicParsing ($raw + $name + '?t=' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) -OutFile (Join-Path $stage $name)
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

  # Nothing running is touched until the candidate imports, compiles, and
  # passes the real controller + seat + screen preflight above.
  $backup = Join-Path $root 'previous'
  if (Test-Path $backup) { Remove-Item -Recurse -Force $backup }
  if (Test-Path $current) { Move-Item -Force $current $backup }
  Move-Item -Force $stage $current

  Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match '^python' -and $_.CommandLine -match 'phone_bridge(?:_fast|_field)?\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

  $out = Join-Path $logs 'field.out.log'
  $err = Join-Path $logs 'field.err.log'
  Remove-Item $out,$err -Force -ErrorAction SilentlyContinue
  $entry = Join-Path $current 'phone_bridge_field.py'
  Start-Process -FilePath 'py' -ArgumentList @('-3.12',$entry,'--token',$Token,'--topic',$Topic) -WorkingDirectory $current -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err

  $state = [ordered]@{
    installed_at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    current = $current
    entry = $entry
    files = $files
    status = 'started'
  }
  $state | ConvertTo-Json -Compress | Set-Content -Encoding UTF8 (Join-Path $root 'state.json')
}
catch {
  try { if (Test-Path $stage) { Remove-Item -Recurse -Force $stage } } catch {}
  $failure = [ordered]@{
    failed_at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    status = 'candidate-refused'
    error = $_.Exception.Message
  }
  $failure | ConvertTo-Json -Compress | Set-Content -Encoding UTF8 (Join-Path $root 'last_failure.json')
  throw
}
