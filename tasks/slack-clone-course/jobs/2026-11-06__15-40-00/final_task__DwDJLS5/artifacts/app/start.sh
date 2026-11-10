#!/bin/sh
set -eu
echo $$ >/tmp/start.pid
cd /app
start_redis() {
  if ! ss -ltnH 'sport = :6379' | grep -q .; then
    redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 &
    echo $! >/tmp/huddle-redis.pid
  fi
}
start_node() {
  i="$1"
  NODE_ID="$i" uvicorn server:app --host 127.0.0.1 --port "$((8000+i))" --app-dir /app >/tmp/huddle-$i.log 2>&1 &
  echo $! >/tmp/huddle-$i.pid
  redis-cli -h 127.0.0.1 -p 6379 subscribe "huddle-node-$i" >/tmp/huddle-redis-$i.log 2>&1 &
  echo $! >/tmp/huddle-redis-$i.pid
}
start_redis
sleep 1
start_irc() {
 python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 &
 echo $! >/tmp/huddle-irc.pid
}
start_irc
for i in 0 1 2; do start_node "$i"; done
while :; do
  start_redis
  if ! kill -0 "$(cat /tmp/huddle-irc.pid 2>/dev/null || echo 0)" 2>/dev/null; then start_irc; fi
  for i in 0 1 2; do
    if ! kill -0 "$(cat /tmp/huddle-$i.pid 2>/dev/null || echo 0)" 2>/dev/null; then start_node "$i"; fi
  done
  sleep 1
done
