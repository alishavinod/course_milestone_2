#!/bin/sh
set -eu
echo $$ > /tmp/start.pid
mkdir -p /app/data
pids=""
cleanup(){ for p in $pids; do kill "$p" 2>/dev/null || true; done; }
trap cleanup INT TERM EXIT
for n in 0 1 2; do
  NODE_ID=$n uvicorn server:app --host 127.0.0.1 --port $((8000+n)) >>"/tmp/huddle-$n.log" 2>&1 &
  pids="$pids $!"
done
wait
