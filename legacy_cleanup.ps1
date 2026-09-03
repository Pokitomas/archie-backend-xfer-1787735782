param([switch]$Apply)
$ErrorActionPreference = 'Stop'

$units = @(
  'archie-ring.target','archie-ingress.service','archie-runtime-truth.service',
  'archie-live-exec.service','archie-toolbus.service','archie-field-worker.service',
  'archie-controller.service','archie-autoscale.service','archie-observatory.service',
  'archie-shell-sidecar.service','archie-chatgpt-takeover.service',
  'archie-chatgpt-visible-stream.service','archie-gpt56-terminal-wire.service',
  'archie-notepad-sensor.service','archie-uia-bridge.service',
  'archie-local-semantic-supervisor.service','archie-control-plane-watchdog.timer',
  'archie-chatgpt-ingress-v2.service'
)

$startup = [Environment]::GetFolderPath('Startup')
$paths = @(
  "$env:LOCALAPPDATA\ARCHIE-Ring",
  "$env:LOCALAPPDATA\ArchieSeat",
  "$env:LOCALAPPDATA\ARCHIE\resident",
  "$env:USERPROFILE\.archie-sidecar",
  "$env:USERPROFILE\archie-vercel-relay",
  "$env:USERPROFILE\Desktop\ARCHIE_RING_ENTRY_RECEIPT.txt",
  "$env:USERPROFILE\Desktop\ENTER_ARCHIE.cmd",
  (Join-Path $startup 'ARCHIE-ControlPlane.cmd'),
  (Join-Path $startup 'ARCHIE-SIDECAR-BOOT.cmd')
)

Write-Host 'Legacy ARCHIE cleanup scope:'
$units | ForEach-Object { Write-Host "  systemd user unit: $_" }
$paths | ForEach-Object { Write-Host "  path: $_" }
Write-Host '  scheduled task: ARCHIE Interactive Worker'
Write-Host '  HKCU Run value: ArchieChatGPTIngressV2'
if (-not $Apply) {
  Write-Host 'Dry run only. Re-run with -Apply to mutate.'
  exit 0
}

# Stop the native resident if this exact image is running.
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
  Where-Object { $_.ExecutablePath -eq "$env:LOCALAPPDATA\ARCHIE\resident\archie.exe" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# Remove the exact Windows persistence roots observed in the forensic dump.
Unregister-ScheduledTask -TaskName 'ARCHIE Interactive Worker' -Confirm:$false -ErrorAction SilentlyContinue
Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'ArchieChatGPTIngressV2' -ErrorAction SilentlyContinue

$wsl = (Get-Command wsl.exe -ErrorAction SilentlyContinue)
if ($wsl) {
  foreach ($u in $units) {
    & wsl.exe -e bash -lc "systemctl --user disable --now '$u' >/dev/null 2>&1 || true"
  }
  & wsl.exe -e bash -lc "crontab -l 2>/dev/null | grep -v 'archie-watchdog' | crontab - 2>/dev/null || true"
  & wsl.exe -e bash -lc "rm -f ~/.config/systemd/user/archie-*.service ~/.config/systemd/user/archie-*.timer ~/.config/systemd/user/archie-*.target ~/archie-watchdog.sh ~/.local/bin/gpt56-ring-enter; systemctl --user daemon-reload >/dev/null 2>&1 || true"
}
foreach ($p in $paths) {
  if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force }
}
Write-Host 'Observed legacy local control-plane and resident artifacts removed.'
