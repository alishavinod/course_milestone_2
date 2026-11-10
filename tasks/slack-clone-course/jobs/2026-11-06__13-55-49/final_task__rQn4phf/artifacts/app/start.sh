#!/bin/bash
set -u
mkdir -p /app/data
echo $$ > /tmp/start.pid
pids=""
for spec in "8000:0" "8001:1" "8002:2"; do
  port="${spec%%:*}"; node="${spec##*:}"
  NODE_ID="$node" PORT="$port" python /app/server.py &
  pids="$pids $!"
  sleep 1
done
trap 'kill $pids 2>/dev/null || true; wait' TERM INT EXIT
wait
