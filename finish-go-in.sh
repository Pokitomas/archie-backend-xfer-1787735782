#!/usr/bin/env bash
set -Eeuo pipefail

U="$HOME/.config/systemd/user"
root="$HOME/.local/share/gpt56-ring"
winroot='/mnt/c/Users/AwesomeKai/AppData/Local/ARCHIE-Ring'
desktop='/mnt/c/Users/AwesomeKai/Desktop'
PS='/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
CMD='/mnt/c/Windows/System32/cmd.exe'
mkdir -p "$root" "$winroot" "$HOME/.local/bin"

for u in archie-ring.target archie-ingress.service archie-runtime-truth.service archie-live-exec.service archie-toolbus.service archie-field-worker.service archie-controller.service; do
  [[ "$(systemctl --user is-active "$u" 2>/dev/null || true)" == active ]]
done

cat > "$winroot/gpt56-seat.ps1" <<'PS1'
param([ValidateSet('attach','touch','detach','head','health')][string]$Mode='head')
$ErrorActionPreference='Stop'
$base='http://127.0.0.1:8798'
if($Mode -eq 'health'){
  $a=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8799/health' -TimeoutSec 3
  $b=Invoke-WebRequest -UseBasicParsing -Uri ($base+'/seat/head') -TimeoutSec 3
  if([int]$a.StatusCode -ne 200 -or [int]$b.StatusCode -ne 200){exit 9}
  Write-Output 'ok'
  exit 0
}
if($Mode -eq 'head'){
  $r=Invoke-WebRequest -UseBasicParsing -Uri ($base+'/seat/head') -TimeoutSec 3
  if([int]$r.StatusCode -ne 200){exit 8}
  Write-Output 'ok'
  exit 0
}
$body = if($Mode -eq 'attach') {
  @{occupant='gpt56sol';capabilities=@('controller-native','ring-ingress','live-exec','toolbus')} | ConvertTo-Json -Compress
} else {
  @{occupant='gpt56sol'} | ConvertTo-Json -Compress
}
$r=Invoke-RestMethod -Method Post -Uri ($base+'/seat/'+$Mode) -ContentType 'application/json' -Body $body -TimeoutSec 3
if(-not $r.ok){exit 7}
if($Mode -eq 'attach' -and $r.occupant -ne 'gpt56sol'){exit 6}
Write-Output 'ok'
PS1

health="$($PS -NoProfile -NonInteractive -ExecutionPolicy Bypass -File 'C:/Users/AwesomeKai/AppData/Local/ARCHIE-Ring/gpt56-seat.ps1' -Mode health 2>/dev/null | tr -d '\r' | tail -1)"
[[ "$health" == ok ]]
attach="$($PS -NoProfile -NonInteractive -ExecutionPolicy Bypass -File 'C:/Users/AwesomeKai/AppData/Local/ARCHIE-Ring/gpt56-seat.ps1' -Mode attach 2>/dev/null | tr -d '\r' | tail -1)"
[[ "$attach" == ok ]]

cat > "$HOME/.local/bin/gpt56-ring-enter" <<'RING'
#!/usr/bin/env bash
set +e
PS='/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
seat='C:/Users/AwesomeKai/AppData/Local/ARCHIE-Ring/gpt56-seat.ps1'
systemctl --user start archie-ring.target >/dev/null 2>&1 || true
printf '\nARCHIE RING ENTRY\n'
for u in archie-ring.target archie-ingress.service archie-runtime-truth.service archie-live-exec.service archie-toolbus.service archie-field-worker.service archie-controller.service; do
  printf '%-34s %s\n' "$u" "$(systemctl --user is-active "$u" 2>/dev/null || true)"
done
"$PS" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$seat" -Mode attach >/dev/null 2>&1 || true
printf 'controller seat                    %s\n\n' "$("$PS" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$seat" -Mode head 2>/dev/null | tr -d '\r' | tail -1)"
parent=$$
(
  while kill -0 "$parent" 2>/dev/null; do
    "$PS" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$seat" -Mode touch >/dev/null 2>&1 || true
    sleep 10
  done
) &
if [[ -x "$HOME/JOIN_ARCHIE" ]]; then
  exec "$HOME/JOIN_ARCHIE" gpt56sol
elif [[ -f "$HOME/JOIN_ARCHIE" ]]; then
  exec bash "$HOME/JOIN_ARCHIE" gpt56sol
else
  exec bash -l
fi
RING
chmod 700 "$HOME/.local/bin/gpt56-ring-enter"

cat > "$winroot/ENTER_ARCHIE.cmd" <<'BAT'
@echo off
where wt.exe >nul 2>nul
if %errorlevel%==0 (
  start "" wt.exe wsl.exe -e bash -lc "~/.local/bin/gpt56-ring-enter"
) else (
  start "" wsl.exe -e bash -lc "~/.local/bin/gpt56-ring-enter"
)
BAT
if [[ -d "$desktop" ]]; then
  cat > "$desktop/ENTER_ARCHIE.cmd" <<'BAT'
@echo off
call "%LOCALAPPDATA%\ARCHIE-Ring\ENTER_ARCHIE.cmd"
BAT
fi

launch_rc=0
"$CMD" /d /c start "" "%LOCALAPPDATA%\ARCHIE-Ring\ENTER_ARCHIE.cmd" >/dev/null 2>&1 || launch_rc=$?
[[ "$launch_rc" == 0 ]]
sleep 6

entry_pid="$(pgrep -f 'room.py join gpt56sol' | head -1 || true)"
if [[ -z "$entry_pid" ]]; then
  # one fallback through Windows Terminal's full path if PATH lookup on the desktop shell failed
  wt='/mnt/c/Users/AwesomeKai/AppData/Local/Microsoft/WindowsApps/wt.exe'
  if [[ -x "$wt" ]]; then
    "$wt" wsl.exe -e bash -lc '~/.local/bin/gpt56-ring-enter' >/dev/null 2>&1 &
    sleep 6
    entry_pid="$(pgrep -f 'room.py join gpt56sol' | head -1 || true)"
  fi
fi
[[ -n "$entry_pid" ]]

head_ok="$($PS -NoProfile -NonInteractive -ExecutionPolicy Bypass -File 'C:/Users/AwesomeKai/AppData/Local/ARCHIE-Ring/gpt56-seat.ps1' -Mode head 2>/dev/null | tr -d '\r' | tail -1)"
[[ "$head_ok" == ok ]]

receipt="$root/entry-receipt.txt"
{
  echo 'directive=go in. real ring. no fake success.'
  echo "at=$(date -Is)"
  echo "host=$(hostname)"
  for u in archie-ring.target archie-ingress.service archie-runtime-truth.service archie-live-exec.service archie-toolbus.service archie-field-worker.service archie-controller.service; do
    printf '%s=%s pid=%s\n' "$u" "$(systemctl --user is-active "$u" 2>/dev/null || true)" "$(systemctl --user show "$u" -p MainPID --value 2>/dev/null || true)"
  done
  echo 'worker_http=200'
  echo 'seat_http=200'
  echo 'seat_attach=gpt56sol'
  echo "room_pid=$entry_pid"
  echo 'gui_launch=ok'
  echo 'ring_enter=installed'
} > "$receipt"
if [[ -d "$desktop" ]]; then cp "$receipt" "$desktop/ARCHIE_RING_ENTRY_RECEIPT.txt"; fi

echo "GO_IN_RECEIPT host=$(hostname)"
echo "ring=$(systemctl --user is-active archie-ring.target) ingress=$(systemctl --user is-active archie-ingress.service) truth=$(systemctl --user is-active archie-runtime-truth.service) exec=$(systemctl --user is-active archie-live-exec.service) toolbus=$(systemctl --user is-active archie-toolbus.service)"
echo "worker=$(systemctl --user is-active archie-field-worker.service) controller=$(systemctl --user is-active archie-controller.service) worker_http=200 seat_http=200 seat_attach=gpt56sol"
echo "room_pid=$entry_pid gui_launch=ok ring_enter=installed"
echo "local_receipt=$receipt"
