#!/usr/bin/env bash
set -u
cd /app
exec 9>/tmp/huddle-supervisor.lock
flock -n 9 || exit 0
echo $$ > /tmp/start.pid
pids=()
irc_pid=''
cleanup() {
  if [ -n "${irc_pid:-}" ]; then kill "$irc_pid" 2>/dev/null || true; fi
  for p in "${pids[@]:-}"; do
    if [ -n "${p:-}" ] && kill -0 "$p" 2>/dev/null; then kill "$p" 2>/dev/null || true; fi
  done
  exit 0
}
trap cleanup INT TERM EXIT
start_node() {
  local i="$1" port=$((8000+i))
  if ss -ltnH "sport = :$port" 2>/dev/null | grep -q LISTEN; then
    pids[$i]=""
    return
  fi
  NODE_ID="$i" python3 -m uvicorn app:app --host 127.0.0.1 --port "$port" >>"/tmp/huddle-$i.log" 2>&1 &
  pids[$i]=$!
}
for i in 0 1 2; do start_node "$i"; done
python3 /app/irc.py >>/tmp/huddle-irc.log 2>&1 & irc_pid=$!
while true; do
  for i in 0 1 2; do
    p="${pids[$i]:-}"
    if [ -z "$p" ] || ! kill -0 "$p" 2>/dev/null || [ "$(awk '{print $3}' "/proc/$p/stat" 2>/dev/null)" = Z ]; then
      wait "$p" 2>/dev/null || true
      start_node "$i"
    fi
  done
  sleep 0.2
done
