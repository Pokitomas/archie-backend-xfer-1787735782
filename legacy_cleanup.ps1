param([switch]$Apply)
$ErrorActionPreference = 'Stop'

$units = @(
  'archie-ring.target','archie-ingress.service','archie-runtime-truth.service',
  'archie-live-exec.service','archie-toolbus.service','archie-field-worker.service',
  'archie-controller.service','archie-autoscale.service','archie-observatory.service'
)
$paths = @(
  "$env:LOCALAPPDATA\ARCHIE-Ring",
  "$env:LOCALAPPDATA\ArchieSeat",
  "$env:USERPROFILE\archie-vercel-relay",
  "$env:USERPROFILE\Desktop\ARCHIE_RING_ENTRY_RECEIPT.txt",
  "$env:USERPROFILE\Desktop\ENTER_ARCHIE.cmd"
)

Write-Host 'Legacy ARCHIE cleanup scope:'
$units | ForEach-Object { Write-Host "  systemd user unit: $_" }
$paths | ForEach-Object { Write-Host "  path: $_" }
if (-not $Apply) {
  Write-Host 'Dry run only. Re-run with -Apply to mutate.'
  exit 0
}

$wsl = (Get-Command wsl.exe -ErrorAction SilentlyContinue)
if ($wsl) {
  foreach ($u in $units) {
    & wsl.exe -e bash -lc "systemctl --user disable --now '$u' >/dev/null 2>&1 || true"
  }
  & wsl.exe -e bash -lc "crontab -l 2>/dev/null | grep -v 'archie-watchdog' | crontab - 2>/dev/null || true"
  & wsl.exe -e bash -lc "rm -f ~/.config/systemd/user/archie-{ring.target,ingress.service,runtime-truth.service,live-exec.service,toolbus.service,field-worker.service,controller.service,autoscale.service,observatory.service} ~/archie-watchdog.sh ~/.local/bin/gpt56-ring-enter; systemctl --user daemon-reload >/dev/null 2>&1 || true"
}
foreach ($p in $paths) {
  if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force }
}
Write-Host 'Legacy local control-plane artifacts removed from the enumerated scope.'
