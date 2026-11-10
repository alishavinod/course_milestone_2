#!/usr/bin/env bash
set -u
echo $$ >/tmp/start.pid
start_redis() {
  (redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no >/tmp/redis.log 2>&1 & echo $! >/tmp/redis.pid)
}
start_sub() {
  local i="$1"
  (redis-cli -h 127.0.0.1 -p 6379 --csv SUBSCRIBE huddle-node-$i >/tmp/redis-sub$i.log 2>&1 & echo $! >/tmp/redis-sub$i.pid)
}
start_irc() { (python3 /app/irc.py >/tmp/irc.log 2>&1 & echo $! >/tmp/irc.pid); }
start_node() {
  local i="$1"
  (NODE_ID=$i PORT=$((8000+i)) python3 -m uvicorn app:app --host 127.0.0.1 --port $((8000+i)) >>/tmp/node$i.log 2>&1 & echo $! >/tmp/node$i.pid)
}
start_redis
for i in 0 1 2; do start_node "$i"; done
start_irc
sleep 1
for i in 0 1 2; do start_sub "$i"; done
cleanup() {
  for f in /tmp/node0.pid /tmp/node1.pid /tmp/node2.pid /tmp/redis.pid /tmp/redis-sub0.pid /tmp/redis-sub1.pid /tmp/redis-sub2.pid /tmp/irc.pid; do
    [ -f "$f" ] && kill -9 "$(cat "$f")" 2>/dev/null || true
  done
  exit 0
}
trap cleanup SIGTERM SIGINT
while true; do
  if [ ! -f /tmp/redis.pid ] || ! kill -0 "$(cat /tmp/redis.pid)" 2>/dev/null; then start_redis; sleep 1; for i in 0 1 2; do start_sub "$i"; done; fi
  for i in 0 1 2; do if [ ! -f "/tmp/redis-sub$i.pid" ] || ! kill -0 "$(cat "/tmp/redis-sub$i.pid")" 2>/dev/null; then start_sub "$i"; fi; done
  if [ ! -f /tmp/irc.pid ] || ! kill -0 "$(cat /tmp/irc.pid)" 2>/dev/null; then start_irc; fi
  for i in 0 1 2; do
    if [ ! -f "/tmp/node$i.pid" ] || ! kill -0 "$(cat "/tmp/node$i.pid")" 2>/dev/null; then start_node "$i"; fi
  done
  sleep 1
done
