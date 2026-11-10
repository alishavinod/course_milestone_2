#!/usr/bin/env bash
set -u
echo $$ > /tmp/start.pid
mkdir -p /app/data
if ! ss -ltn "sport = :6379" | grep -q LISTEN; then
  redis-server --port 6379 --bind 127.0.0.1 --save "" --appendonly no >/tmp/redis.log 2>&1 &
  echo $! >/tmp/redis.pid
fi
pids=()
start_node() {
  local n="$1" p="$2"
  NODE_ID="$n" PORT="$p" uvicorn server:app --host 127.0.0.1 --port "$p" >/tmp/node"$n".log 2>&1 &
  pids[$n]=$!
}
start_node 0 8000; start_node 1 8001; start_node 2 8002
while true; do
  for n in 0 1 2; do
    if ! kill -0 "${pids[$n]}" 2>/dev/null; then
      start_node "$n" "$((8000+n))"
    fi
  done
  if ! kill -0 "$(cat /tmp/redis.pid 2>/dev/null)" 2>/dev/null; then
    redis-server --port 6379 --bind 127.0.0.1 --save "" --appendonly no >/tmp/redis.log 2>&1 &
    echo $! >/tmp/redis.pid
  fi
  sleep .2
done
