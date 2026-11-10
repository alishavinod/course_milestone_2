#!/bin/sh
echo $$ >/tmp/start.pid
mkdir -p /app/data
redis_start() { redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 & echo $! >/tmp/huddle-redis.pid; }
redis_start
launch() { n="$1"; port=$((8000+n)); NODE_ID=$n PORT=$port uvicorn app:app --host 127.0.0.1 --port "$port" >/tmp/huddle-$n.log 2>&1 & echo $! >/tmp/huddle-$n.pid; }
launch 0; launch 1; launch 2
python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 & echo $! >/tmp/huddle-irc.pid
trap 'for n in 0 1 2; do kill -9 "$(cat /tmp/huddle-$n.pid)" 2>/dev/null || true; done; kill -9 "$(cat /tmp/huddle-redis.pid)" 2>/dev/null || true; kill -9 "$(cat /tmp/huddle-irc.pid)" 2>/dev/null || true; exit' INT TERM
while :; do
 rpid=$(cat /tmp/huddle-redis.pid 2>/dev/null || echo 0); kill -0 "$rpid" 2>/dev/null || redis_start
 for n in 0 1 2; do
  pid=$(cat /tmp/huddle-$n.pid 2>/dev/null || echo 0)
  kill -0 "$pid" 2>/dev/null || launch "$n"
 done
 sleep 1
done
