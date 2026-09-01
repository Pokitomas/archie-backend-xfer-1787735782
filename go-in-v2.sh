#!/usr/bin/env bash
set -Eeuo pipefail

directive='go in. real ring only. revive the actual seat + worker, attach me, open the resident room. no fake success; receipts or it didnt happen.'
U="$HOME/.config/systemd/user"
root="$HOME/.local/share/gpt56-ring"
py='/mnt/c/Users/AwesomeKai/AppData/Local/Programs/Python/Python312/python.exe'
controller_tmp='/mnt/c/Users/AwesomeKai/AppData/Local/Temp/ARCHIE_CONTROLLER.py'
controller_src='/mnt/c/Users/AwesomeKai/Documents/New project/ARCHIE_CONTROLLER.py'
worker_src='/mnt/c/Users/AwesomeKai/AppData/Local/Temp/ARCHIE_LIVE_STATE_WORKER.py'
mkdir -p "$U" "$root" "$HOME/.local/bin" "$(dirname "$controller_src")"

[[ -x "$py" ]]
[[ -f "$controller_tmp" ]]
[[ -f "$worker_src" ]]
[[ -e "$HOME/JOIN_ARCHIE" ]]

# Restore the known universal-seat body into its canonical Windows source location.
cp -f "$controller_tmp" "$controller_src"

# One lifecycle root owns the controller and its action worker. No old watchdog/provider loop.
cat > "$U/archie-field-worker.service" <<'UNIT'
[Unit]
Description=ARCHIE field action worker
PartOf=archie-ring.target
Before=archie-controller.service

[Service]
Type=simple
Environment=PYTHONUTF8=1
ExecStart=/mnt/c/Users/AwesomeKai/AppData/Local/Programs/Python/Python312/python.exe "C:/Users/AwesomeKai/AppData/Local/Temp/ARCHIE_LIVE_STATE_WORKER.py" --host 127.0.0.1 --port 8799
Restart=on-failure
RestartSec=1

[Install]
WantedBy=archie-ring.target
UNIT

cat > "$U/archie-controller.service" <<'UNIT'
[Unit]
Description=ARCHIE universal controller seat
PartOf=archie-ring.target
After=archie-field-worker.service
Wants=archie-field-worker.service

[Service]
Type=simple
Environment=PYTHONUTF8=1
Environment=ARCHIE_CONTROLLER_ALLOWED_NETS=172.16.0.0/12
ExecStart=/mnt/c/Users/AwesomeKai/AppData/Local/Programs/Python/Python312/python.exe "C:/Users/AwesomeKai/Documents/New project/ARCHIE_CONTROLLER.py" --host 0.0.0.0 --port 8798 --worker http://127.0.0.1:8799/action --sensors
Restart=on-failure
RestartSec=1

[Install]
WantedBy=archie-ring.target
UNIT

systemctl --user daemon-reload
systemctl --user enable archie-field-worker.service archie-controller.service >/dev/null
systemctl --user restart archie-field-worker.service
sleep 1
systemctl --user restart archie-controller.service
sleep 4

[[ "$(systemctl --user is-active archie-field-worker.service)" == active ]]
[[ "$(systemctl --user is-active archie-controller.service)" == active ]]
worker_http="$(curl -sS --max-time 2 -o "$root/worker-health.json" -w '%{http_code}' http://127.0.0.1:8799/health || true)"
seat_http="$(curl -sS --max-time 2 -o "$root/seat-head-before.json" -w '%{http_code}' http://127.0.0.1:8798/seat/head || true)"
[[ "$worker_http" == 200 ]]
[[ "$seat_http" == 200 ]]

attach_http="$(curl -sS --max-time 3 -o "$root/seat-attach.json" -w '%{http_code}' \
  -H 'content-type: application/json' \
  --data '{"occupant":"gpt56sol","capabilities":["controller-native","ring-ingress","live-exec","toolbus"]}' \
  http://127.0.0.1:8798/seat/attach || true)"
if [[ "$attach_http" != 200 ]]; then
  /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -NonInteractive -Command \
    '$ProgressPreference="SilentlyContinue"; $r=Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8798/seat/attach" -ContentType "application/json" -Body ''{"occupant":"gpt56sol","capabilities":["controller-native","ring-ingress","live-exec","toolbus"]}''; if(-not $r.ok){exit 2}' \
    >/dev/null
  attach_http=200
fi
[[ "$attach_http" == 200 ]]

seat_after_http="$(curl -sS --max-time 2 -o "$root/seat-head-after.json" -w '%{http_code}' http://127.0.0.1:8798/seat/head)"
[[ "$seat_after_http" == 200 ]]
seat_sha="$(sha256sum "$root/seat-head-after.json" | awk '{print $1}')"

cat > "$HOME/.local/bin/gpt56-ring-enter" <<'RING'
#!/usr/bin/env bash
set +e
ctl='http://127.0.0.1:8798'
systemctl --user start archie-ring.target >/dev/null 2>&1 || true
curl -sS --max-time 2 -o /dev/null -H 'content-type: application/json' \
  --data '{"occupant":"gpt56sol","capabilities":["controller-native","ring-ingress","live-exec","toolbus"]}' \
  "$ctl/seat/attach" >/dev/null 2>&1 || true
printf '\nARCHIE RING ENTRY\n'
for u in archie-ring.target archie-ingress.service archie-runtime-truth.service archie-live-exec.service archie-toolbus.service archie-field-worker.service archie-controller.service; do
  printf '%-34s %s\n' "$u" "$(systemctl --user is-active "$u" 2>/dev/null || true)"
done
printf 'seat/head                          %s\n\n' "$(curl -sS --max-time 2 -o /dev/null -w '%{http_code}' "$ctl/seat/head" 2>/dev/null || true)"
parent=$$
(
  while kill -0 "$parent" 2>/dev/null; do
    curl -sS --max-time 2 -o /dev/null -H 'content-type: application/json' --data '{"occupant":"gpt56sol"}' "$ctl/seat/touch" >/dev/null 2>&1 || true
    sleep 10
  done
  curl -sS --max-time 2 -o /dev/null -H 'content-type: application/json' --data '{"occupant":"gpt56sol"}' "$ctl/seat/detach" >/dev/null 2>&1 || true
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

winlocal='/mnt/c/Users/AwesomeKai/AppData/Local/ARCHIE-Ring'
desktop='/mnt/c/Users/AwesomeKai/Desktop'
mkdir -p "$winlocal"
cat > "$winlocal/ENTER_ARCHIE.cmd" <<'CMD'
@echo off
where wt.exe >nul 2>nul
if %errorlevel%==0 (
  start "" wt.exe wsl.exe -e bash -lc "~/.local/bin/gpt56-ring-enter"
) else (
  start "" wsl.exe -e bash -lc "~/.local/bin/gpt56-ring-enter"
)
CMD
if [[ -d "$desktop" ]]; then
  cat > "$desktop/ENTER_ARCHIE.cmd" <<'CMD'
@echo off
call "%LOCALAPPDATA%\ARCHIE-Ring\ENTER_ARCHIE.cmd"
CMD
fi

launch_rc=0
/mnt/c/Windows/System32/cmd.exe /d /c start "" "%LOCALAPPDATA%\ARCHIE-Ring\ENTER_ARCHIE.cmd" >/dev/null 2>&1 || launch_rc=$?
[[ "$launch_rc" == 0 ]]
sleep 4

entry_seen=0
if ps -eo pid,ppid,etimes,args | grep -E '[r]oom\.py join gpt56sol|[J]OIN_ROOM gpt56sol|[g]pt56-ring-enter' >/dev/null 2>&1; then
  entry_seen=1
fi
[[ "$entry_seen" == 1 ]]

bridge_http="$(curl -sS --max-time 2 -o /dev/null -w '%{http_code}' http://127.0.0.1:8796/health 2>/dev/null || true)"

receipt="$root/entry-receipt.txt"
{
  echo "directive=$directive"
  echo "at=$(date -Is)"
  echo "host=$(hostname)"
  for u in archie-ring.target archie-ingress.service archie-runtime-truth.service archie-live-exec.service archie-toolbus.service archie-field-worker.service archie-controller.service; do
    printf '%s=%s pid=%s\n' "$u" "$(systemctl --user is-active "$u" 2>/dev/null || true)" "$(systemctl --user show "$u" -p MainPID --value 2>/dev/null || true)"
  done
  echo "worker_http=$worker_http"
  echo "seat_http=$seat_after_http"
  echo "seat_sha256=$seat_sha"
  echo "bridge_http=$bridge_http"
  echo "ring_enter=installed"
  echo "join_archie=present"
  echo "gui_launch=ok"
  echo "entry_process_seen=$entry_seen"
} > "$receipt"
[[ -d "$desktop" ]] && cp "$receipt" "$desktop/ARCHIE_RING_ENTRY_RECEIPT.txt"

echo "GO_IN_RECEIPT host=$(hostname)"
echo "ring=$(systemctl --user is-active archie-ring.target) ingress=$(systemctl --user is-active archie-ingress.service) truth=$(systemctl --user is-active archie-runtime-truth.service) exec=$(systemctl --user is-active archie-live-exec.service) toolbus=$(systemctl --user is-active archie-toolbus.service)"
echo "worker=$(systemctl --user is-active archie-field-worker.service) controller=$(systemctl --user is-active archie-controller.service) worker_http=$worker_http seat_http=$seat_after_http attach_http=$attach_http"
echo "seat_sha256=$seat_sha gui_launch=ok entry_process_seen=$entry_seen bridge_http=$bridge_http"
echo "local_receipt=$receipt"
