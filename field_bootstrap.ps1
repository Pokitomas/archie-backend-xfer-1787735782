param(
  [Parameter(Mandatory=$true)][string]$Token,
  [string]$Topic = ''
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$root = Join-Path $env:LOCALAPPDATA 'ARCHIE\field'
$nativeRoot = Join-Path $root 'native'
$stagingRoot = Join-Path $root 'staging'
$current = Join-Path $root 'current'
$logs = Join-Path $root 'logs'
New-Item -ItemType Directory -Force $stagingRoot,$logs,$nativeRoot | Out-Null
$stage = Join-Path $stagingRoot ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $stage | Out-Null

# This is deliberately a migration transport, not the runtime architecture.
# It resolves one coherent source snapshot, proves it locally, then the direct
# resident takes over and all hosted relay/rendezvous processes are killed.
$repo = 'Pokitomas/archie-backend-xfer-1787735782'
$head = Invoke-RestMethod -UseBasicParsing -Headers @{ 'User-Agent'='archie-field-migration/4'; 'Cache-Control'='no-cache' } ('https://api.github.com/repos/' + $repo + '/commits/master?t=' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
$sha = [string]$head.sha
if ($sha.Length -lt 40) { throw 'could not resolve coherent migration snapshot' }
$raw = 'https://raw.githubusercontent.com/' + $repo + '/' + $sha + '/'
$files = @(
  'phone_bridge.py','phone_bridge_field.py','field_protocol.py','field_transport.py','field_mcp.py','field_entry_server.py',
  'native_aperture.py','native_secret.py','native_resident.py','native_field_server.py',
  'field_acme.ps1','native_index.html','native_field_client.js','field_kernel.js','field_surface.js','field_ios_adapter.js','phone_scene.json'
)
$pyFiles = @('phone_bridge.py','phone_bridge_field.py','field_protocol.py','field_transport.py','field_mcp.py','field_entry_server.py','native_aperture.py','native_secret.py','native_resident.py','native_field_server.py')

function Start-LegacyProcess([string]$dir) {
  $entryName = if (Test-Path (Join-Path $dir 'phone_bridge_field.py')) { 'phone_bridge_field.py' } else { 'phone_bridge.py' }
  $out = Join-Path $logs 'legacy.out.log'; $err = Join-Path $logs 'legacy.err.log'
  Remove-Item $out,$err -Force -ErrorAction SilentlyContinue
  return Start-Process -FilePath 'py' -ArgumentList @('-3.12',(Join-Path $dir $entryName),'--token',$Token,'--topic',$Topic) -WorkingDirectory $dir -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
}

function Stop-LegacyProcesses {
  Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match '^python' -and $_.CommandLine -match 'phone_bridge(?:_fast|_field)?\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Stop-NativeProcesses {
  Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match '^python' -and $_.CommandLine -match 'native_(?:resident|field_server)\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Stop-HostedRuntime {
  Get-CimInstance Win32_Process |
    Where-Object { ($_.Name -match '^cloudflared') -or ($_.CommandLine -match 'ntfy\.sh') } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Wait-Native([int]$milliseconds=14000) {
  $state = Join-Path $nativeRoot 'native-state.json'
  $deadline = [Environment]::TickCount64 + $milliseconds
  while ([Environment]::TickCount64 -lt $deadline) {
    try {
      if (Test-Path $state) {
        $v = Get-Content -Raw $state | ConvertFrom-Json
        if ($v.status -eq 'native-live' -and $v.surface_url -and $v.mcp_url) { return $v }
        if ($v.status -in @('native-unavailable','certificate-refused','server-refused')) { return $v }
      }
    } catch {}
    Start-Sleep -Milliseconds 120
  }
  return $null
}

function Register-NativeTask([string]$dir) {
  try {
    $tokenFile = Join-Path $nativeRoot 'token.dpapi'
    $args = '-3.12 "' + (Join-Path $dir 'native_resident.py') + '" --token-file "' + $tokenFile + '"'
    $action = New-ScheduledTaskAction -Execute 'py.exe' -Argument $args -WorkingDirectory $dir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName 'ARCHIE Native Field' -Action $action -Trigger $trigger -Settings $settings -User $env:USERNAME -RunLevel Limited -Force | Out-Null
    return $true
  } catch { return $false }
}

function Publish-Cutover([object]$native) {
  if (-not $Topic -or -not $native -or -not $native.surface_url) { return $false }
  try {
    $message = [ordered]@{
      url = [string]$native.surface_url
      token = $Token
      native = $true
      transport = 'native-direct-https'
    } | ConvertTo-Json -Compress
    $uri = 'https://ntfy.sh/' + [uri]::EscapeDataString($Topic)
    Invoke-RestMethod -UseBasicParsing -Method Post -Uri $uri -ContentType 'text/plain; charset=utf-8' -Body $message | Out-Null
    return $true
  } catch { return $false }
}

try {
  foreach ($name in $files) { Invoke-WebRequest -UseBasicParsing ($raw + $name) -OutFile (Join-Path $stage $name) }

  Push-Location $stage
  try {
    & py -3.12 -m py_compile @pyFiles
    if ($LASTEXITCODE -ne 0) { throw 'py_compile failed' }
    $null = [scriptblock]::Create((Get-Content -Raw (Join-Path $stage 'field_acme.ps1')))
    $probe = @"
import json
import phone_bridge as base
import phone_bridge_field
import field_protocol, field_transport, field_mcp, native_aperture, native_resident, native_field_server
base.TOKEN = 'x' * 40
value = base.run_selftest()
print(json.dumps(value, separators=(',', ':')))
raise SystemExit(0 if value.get('ok') else 3)
"@
    & py -3.12 -c $probe
    if ($LASTEXITCODE -ne 0) { throw 'controller/screen preflight failed' }

    # Prepare the direct route and publicly trusted IP certificate while the
    # existing bridge is still serving. Only the final socket swap is brief.
    $prepare = @"
import json
from pathlib import Path
import native_resident as r
root=Path(r'''$nativeRoot''')
cert=root/'tls'/'field-cert.pem'; key=root/'tls'/'field-key.pem'; account=root/'tls'/'acme-account-key.pem'
route,errors=r.choose_route()
if route is None:
 print(json.dumps({'ok':False,'error':'native-route','errors':errors})); raise SystemExit(4)
errors.extend(r.firewall_allow([route.internal_https_port,route.challenge_port]))
issued={'ok':True,'cached':True}
if not r.certificate_ready(cert,route.identifier):
 issued=r.issue_certificate(route,cert=cert,key=key,account=account)
ok=bool(issued.get('ok')) and r.certificate_ready(cert,route.identifier,minimum_left=3600)
print(json.dumps({'ok':ok,'route':r.asdict(route),'issued':issued,'errors':errors},separators=(',',':')))
raise SystemExit(0 if ok else 5)
"@
    $prepOut = & py -3.12 -c $prepare
    if ($LASTEXITCODE -ne 0) { throw ('native reachability/certificate preparation failed: ' + ($prepOut -join ' ')) }
  }
  finally { Pop-Location }

  $backup = Join-Path $root 'previous'
  if (Test-Path $backup) { Remove-Item -Recurse -Force $backup }
  if (Test-Path $current) { Move-Item -Force $current $backup }
  Move-Item -Force $stage $current

  # Certificate and route are already prepared. This is the only intentional
  # cutover gap: stop old localhost HTTP, start direct TLS on the same machine.
  Stop-NativeProcesses
  Stop-LegacyProcesses
  Remove-Item (Join-Path $nativeRoot 'native-state.json') -Force -ErrorAction SilentlyContinue
  $nativeOut = Join-Path $logs 'resident.out.log'; $nativeErr = Join-Path $logs 'resident.err.log'
  $resident = Start-Process -FilePath 'py' -ArgumentList @('-3.12',(Join-Path $current 'native_resident.py'),'--token',$Token) -WorkingDirectory $current -WindowStyle Hidden -RedirectStandardOutput $nativeOut -RedirectStandardError $nativeErr -PassThru
  $native = Wait-Native 14000

  if (-not $native -or $native.status -ne 'native-live') {
    Stop-NativeProcesses
    if (Test-Path $backup) {
      Remove-Item -Recurse -Force $current -ErrorAction SilentlyContinue
      Move-Item -Force $backup $current
      $null = Start-LegacyProcess $current
    }
    throw ('native cutover refused: ' + ($(if ($native) { $native | ConvertTo-Json -Compress } else { 'timeout' })))
  }

  # Hand the already-open browser the direct endpoint once. This is the last
  # hosted rendezvous use; the native field itself never depends on it.
  $announced = Publish-Cutover $native

  # The direct path proved local health after an externally validated ACME
  # challenge. Hosted runtime dependencies are now dead, not standby fallbacks.
  Stop-HostedRuntime
  $taskOk = Register-NativeTask $current

  [ordered]@{
    installed_at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    commit = $sha
    current = $current
    entry = 'native_resident.py'
    resident_pid = $resident.Id
    native = $native
    autostart = $taskOk
    cutover_announced = $announced
    migration_transport_retired = $true
    status = 'native-live'
  } | ConvertTo-Json -Depth 8 -Compress | Set-Content -Encoding UTF8 (Join-Path $root 'state.json')

  Get-ChildItem $stagingRoot -Directory -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
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
