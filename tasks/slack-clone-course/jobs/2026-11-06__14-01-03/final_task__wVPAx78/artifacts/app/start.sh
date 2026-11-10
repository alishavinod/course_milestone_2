#!/usr/bin/env bash
set -u
mkdir -p /app/data
echo $$ > /tmp/start.pid
declare -A PIDS
start_node() {
  local n="$1" port="$2"
  NODE_ID="$n" nohup python -m uvicorn app:app --host 127.0.0.1 --port "$port" --log-level debug >/tmp/huddle-$port.log 2>&1 &
  PIDS["node$n"]=$!
}
start_redis() {
  if command -v redis-server >/dev/null 2>&1 && ! ss -ltnH "sport = :6379" | grep -q LISTEN; then
    redis-server --port 6379 --bind 127.0.0.1 --save "" --appendonly no >/tmp/huddle-redis.log 2>&1 &
    PIDS[redis]=$!
  fi
}
start_node 0 8000; start_node 1 8001; start_node 2 8002; start_redis
while true; do
  for n in 0 1 2; do
    port=$((8000+n))
    if ! kill -0 "${PIDS[node$n]}" 2>/dev/null; then start_node "$n" "$port"; fi
  done
  if command -v redis-server >/dev/null 2>&1 && ! kill -0 "${PIDS[redis]}" 2>/dev/null; then start_redis; fi
  sleep 0.25
done
