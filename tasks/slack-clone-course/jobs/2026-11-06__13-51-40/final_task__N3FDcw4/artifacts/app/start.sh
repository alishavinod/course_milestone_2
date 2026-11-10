#!/usr/bin/env bash
set -u
echo $$ > /tmp/start.pid
mkdir -p /app/data
pids=()
trap 'for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; wait' INT TERM EXIT
for i in 0 1 2; do
  NODE_ID=$i PORT=$((8000+i)) python3 /app/main.py &
  pids+=("$!")
done
while :; do
  for i in "${!pids[@]}"; do
    if ! kill -0 "${pids[$i]}" 2>/dev/null; then
      NODE_ID=$i PORT=$((8000+i)) python3 /app/main.py &
      pids[$i]=$!
    fi
  done
  sleep 1
done
