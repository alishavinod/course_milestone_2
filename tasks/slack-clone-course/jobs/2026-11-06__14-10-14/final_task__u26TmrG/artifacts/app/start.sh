#!/bin/sh
set -u
echo $$ > /tmp/start.pid
mkdir -p /app/data
redis-server --port 6379 --bind 127.0.0.1 --save "" --appendonly no >/tmp/huddle-redis.log 2>&1 &
rpid=$!
pids=""
trap 'kill "$rpid" $pids 2>/dev/null || true; exit 0' TERM INT
start_node(){ NODE_ID=$1 python -m uvicorn app:app --host 127.0.0.1 --port "$2" >/tmp/huddle-$1.log 2>&1 & echo $!; }
pids="$(start_node 0 8000) $(start_node 1 8001) $(start_node 2 8002)"
while :; do
  if ! kill -0 "$rpid" 2>/dev/null; then redis-server --port 6379 --bind 127.0.0.1 --save "" --appendonly no >/tmp/huddle-redis.log 2>&1 & rpid=$!; fi
  i=0
  for port in 8000 8001 8002; do
    pid=$(echo "$pids" | awk "{print \$$((i+1))}")
    if ! kill -0 "$pid" 2>/dev/null; then new=$(start_node $i $port); pids=$(echo "$pids" | awk -v n="$new" -v i="$((i+1))" '{ $i=n; print }'); fi
    i=$((i+1))
  done
  sleep 1
done
