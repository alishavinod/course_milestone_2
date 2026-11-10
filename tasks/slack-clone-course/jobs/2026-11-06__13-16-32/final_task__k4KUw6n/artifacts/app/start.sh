#!/bin/sh
set -eu
echo $$ > /tmp/start.pid
mkdir -p /app/data
pids=""
trap 'for p in $pids; do kill "$p" 2>/dev/null || true; done; wait' INT TERM EXIT
for spec in "8000:0" "8001:1" "8002:2"; do
  port=${spec%:*}; node=${spec#*:}
  NODE_ID="$node" PORT="$port" python3 /app/server.py &
  pids="$pids $!"
done
while :; do
  alive=0
  for p in $pids; do kill -0 "$p" 2>/dev/null && alive=1 || true; done
  [ "$alive" = 1 ] || exit 1
  sleep 1
done
