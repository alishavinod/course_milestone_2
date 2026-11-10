#!/bin/sh
set -eu
echo $$ >/tmp/start.pid
mkdir -p /app/data
launch_http() { i="$1"; port=$((8000+i)); NODE_ID="$i" PORT="$port" uvicorn server:app --host 127.0.0.1 --port "$port" --app-dir /app >/tmp/huddle-node-$i.log 2>&1 & echo $! >/tmp/huddle-node-$i.pid; }
launch_irc() { NODE_ID=0 python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 & echo $! >/tmp/huddle-irc.pid; }
cleanup() { for f in /tmp/huddle-node-0.pid /tmp/huddle-node-1.pid /tmp/huddle-node-2.pid /tmp/huddle-irc.pid; do [ -f "$f" ] && kill -TERM "$(cat "$f")" 2>/dev/null || true; done; exit 0; }
trap cleanup INT TERM EXIT
for i in 0 1 2; do launch_http "$i"; done
launch_irc
while :; do
  for i in 0 1 2; do f=/tmp/huddle-node-$i.pid; if [ ! -s "$f" ] || ! kill -0 "$(cat "$f")" 2>/dev/null; then launch_http "$i"; fi; done
  f=/tmp/huddle-irc.pid; if [ ! -s "$f" ] || ! kill -0 "$(cat "$f")" 2>/dev/null; then launch_irc; fi
  sleep 1
done
