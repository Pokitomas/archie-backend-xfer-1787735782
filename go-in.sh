set -Eeuo pipefail

directive='go in. use the real ring. old door junk is dead. no fake success. receipts or it didnt happen.'
root="$HOME/.local/share/gpt56-ring"
mkdir -p "$HOME/.local/bin" "$root"

find_ctl() {
  local c code
  local from_file=""
  if [[ -r "$HOME/archie/.ctl-endpoint" ]]; then
    from_file="$(sed -n 's/^ARCHIE_CTL=//p' "$HOME/archie/.ctl-endpoint" | tail -1)"
  fi
  for c in "$from_file" "http://127.0.0.1:8798" "http://172.22.64.1:8798"; do
    [[ -n "$c" ]] || continue
    code="$(curl -sS --max-time 2 -o /tmp/gpt56-seat-head.json -w '%{http_code}' "$c/seat/head" 2>/dev/null || true)"
    if [[ "$code" == "200" ]]; then
      printf '%s' "$c"
      return 0
    fi
  done
  return 1
}

cat > "$HOME/.local/bin/gpt56-ring-enter" <<'RING'
#!/usr/bin/env bash
set +e

find_ctl() {
  local c code
  local from_file=""
  if [[ -r "$HOME/archie/.ctl-endpoint" ]]; then
    from_file="$(sed -n 's/^ARCHIE_CTL=//p' "$HOME/archie/.ctl-endpoint" | tail -1)"
  fi
  for c in "$from_file" "http://127.0.0.1:8798" "http://172.22.64.1:8798"; do
    [[ -n "$c" ]] || continue
    code="$(curl -sS --max-time 2 -o /tmp/gpt56-ring-seat.json -w '%{http_code}' "$c/seat/head" 2>/dev/null || true)"
    if [[ "$code" == "200" ]]; then
      printf '%s' "$c"
      return 0
    fi
  done
  return 1
}

printf '\nARCHIE RING ENTRY\n'
printf 'host=%s time=%s\n' "$(hostname)" "$(date -Is)"

systemctl --user start archie-ring.target >/dev/null 2>&1 || true
for u in archie-runtime-truth.service archie-live-exec.service archie-toolbus.service; do
  systemctl --user cat "$u" >/dev/null 2>&1 && systemctl --user restart "$u" >/dev/null 2>&1 || true
done

for u in archie-ring.target archie-ingress.service archie-runtime-truth.service archie-live-exec.service archie-toolbus.service; do
  printf '%-34s %s\n' "$u" "$(systemctl --user is-active "$u" 2>/dev/null || true)"
done

ctl="$(find_ctl 2>/dev/null || true)"
if [[ -n "$ctl" ]]; then
  printf 'seat/head                          200\n'
else
  printf 'seat/head                          unavailable\n'
fi
echo

if [[ -x "$HOME/JOIN_ARCHIE" ]]; then
  exec "$HOME/JOIN_ARCHIE" gpt56sol
elif [[ -f "$HOME/JOIN_ARCHIE" ]]; then
  exec bash "$HOME/JOIN_ARCHIE" gpt56sol
else
  echo 'JOIN_ARCHIE missing'
  exec bash -l
fi
RING
chmod 700 "$HOME/.local/bin/gpt56-ring-enter"

# The ingress is executing this command, so never restart the ingress out from under itself.
systemctl --user start archie-ring.target >/dev/null
for u in archie-runtime-truth.service archie-live-exec.service archie-toolbus.service; do
  systemctl --user cat "$u" >/dev/null 2>&1
  systemctl --user restart "$u" >/dev/null
done

bad=0
for u in archie-ring.target archie-ingress.service archie-runtime-truth.service archie-live-exec.service archie-toolbus.service; do
  [[ "$(systemctl --user is-active "$u" 2>/dev/null || true)" == "active" ]] || bad=1
done
[[ "$bad" == 0 ]]

ctl="$(find_ctl)"
seat_code="$(curl -sS --max-time 2 -o "$root/seat-head.json" -w '%{http_code}' "$ctl/seat/head")"
[[ "$seat_code" == "200" ]]
seat_sha="$(sha256sum "$root/seat-head.json" | awk '{print $1}')"

[[ -e "$HOME/JOIN_ARCHIE" ]]
[[ -x "$HOME/.local/bin/gpt56-ring-enter" ]]

winlocal="/mnt/c/Users/AwesomeKai/AppData/Local/ARCHIE-Ring"
desktop="/mnt/c/Users/AwesomeKai/Desktop"
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

receipt="$root/entry-receipt.txt"
{
  echo "directive=$directive"
  echo "at=$(date -Is)"
  echo "host=$(hostname)"
  for u in archie-ring.target archie-ingress.service archie-runtime-truth.service archie-live-exec.service archie-toolbus.service; do
    printf '%s=%s pid=%s\n' "$u" \
      "$(systemctl --user is-active "$u" 2>/dev/null || true)" \
      "$(systemctl --user show "$u" -p MainPID --value 2>/dev/null || true)"
  done
  echo "seat_http=$seat_code"
  echo "seat_sha256=$seat_sha"
  echo "ring_enter=installed"
  echo "join_archie=present"
} > "$receipt"
if [[ -d "$desktop" ]]; then
  cp "$receipt" "$desktop/ARCHIE_RING_ENTRY_RECEIPT.txt"
fi

launch_rc=0
/mnt/c/Windows/System32/cmd.exe /d /c start "" "%LOCALAPPDATA%\ARCHIE-Ring\ENTER_ARCHIE.cmd" >/dev/null 2>&1 || launch_rc=$?
[[ "$launch_rc" == 0 ]]
sleep 3

entry_seen=0
if ps -eo pid,ppid,etimes,args | grep -E '[g]pt56-ring-enter|[J]OIN_ARCHIE[[:space:]]+gpt56sol|[r]oom\.py.*gpt56sol' >/dev/null 2>&1; then
  entry_seen=1
fi

echo "GO_IN_RECEIPT host=$(hostname)"
echo "ring=$(systemctl --user is-active archie-ring.target) ingress=$(systemctl --user is-active archie-ingress.service) truth=$(systemctl --user is-active archie-runtime-truth.service) exec=$(systemctl --user is-active archie-live-exec.service) toolbus=$(systemctl --user is-active archie-toolbus.service)"
echo "seat_http=$seat_code seat_sha256=$seat_sha"
echo "ring_enter=installed join_archie=present gui_launch=ok entry_process_seen=$entry_seen"
echo "local_receipt=$receipt"
