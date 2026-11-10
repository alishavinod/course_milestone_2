#!/usr/bin/env bash
set -u
exec 9>/tmp/huddle.start.lock
flock -n 9 || exit 0
mkdir -p /app/data
echo $$ > /tmp/start.pid
run_redis() {
  while true; do
    if ! redis-cli -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1; then
      redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no >/tmp/redis.log 2>&1 &
      redis_pid=$!
      echo "$redis_pid" > /tmp/redis.pid
      wait "$redis_pid" 2>/dev/null || true
    else
      sleep 0.5
    fi
  done
}
run_redis & pr=$!
run_irc() {
  while true; do
    python3 /app/irc.py
    sleep 0.2
  done
}
run_irc & pi=$!
run_node() {
  local port="$1" node="$2"
  while true; do
    NODE_ID="$node" PORT="$port" python3 /app/main.py
    sleep 0.2
  done
}
run_node 8000 0 & p0=$!
sleep 2
run_node 8001 1 & p1=$!
run_node 8002 2 & p2=$!
trap 'kill "$p0" "$p1" "$p2" "$pr" "$pi" 2>/dev/null || true; wait' TERM INT EXIT
wait
