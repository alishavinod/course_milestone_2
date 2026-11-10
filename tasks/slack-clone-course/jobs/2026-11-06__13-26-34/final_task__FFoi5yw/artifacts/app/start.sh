#!/bin/bash
set -u
echo $$ > /tmp/start.pid
mkdir -p /app/data
redis-server --port 6379 --bind 127.0.0.1 --save "" --appendonly no >/tmp/huddle-redis.log 2>&1 &
REDIS_PID=$!
declare -A PIDS
start_node() {
  local n="$1" p="$2"
  NODE_ID="$n" PORT="$p" python3 -m uvicorn server:app --host 127.0.0.1 --port "$p" --app-dir /app >/tmp/huddle-node-"$n".log 2>&1 &
  PIDS[$n]=$!
}
start_node 0 8000
start_node 1 8001
start_node 2 8002
while true; do
  sleep 0.2
  if ! kill -0 "$REDIS_PID" 2>/dev/null; then
    redis-server --port 6379 --bind 127.0.0.1 --save "" --appendonly no >/tmp/huddle-redis.log 2>&1 &
    REDIS_PID=$!
  fi
  for n in 0 1 2; do
    if ! kill -0 "${PIDS[$n]}" 2>/dev/null; then
      start_node "$n" $((8000+n))
    fi
  done
done
