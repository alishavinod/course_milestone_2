#!/bin/sh
mkdir -p /app/data
echo $$ >/tmp/start.pid
launch_redis() {
 /usr/bin/redis-server --port 6379 --bind 127.0.0.1 --save "" --appendonly no >/tmp/huddle-redis.log 2>&1 &
 echo $! >/tmp/redis.pid
}
launch() {
 n="$1"; port=$((8000+n))
 NODE_ID="$n" /opt/venv/bin/uvicorn server:app --host 127.0.0.1 --port "$port" >>"/tmp/huddle-$n.log" 2>&1 &
 echo $! >"/tmp/huddle-$n.pid"
}
launch_redis
launch_irc() {
 /opt/venv/bin/python /app/irc.py >>/tmp/huddle-irc.log 2>&1 &
 echo $! >/tmp/irc.pid
}
launch_irc
for n in 0 1 2; do launch "$n"; done
trap 'for n in 0 1 2; do [ -f "/tmp/huddle-$n.pid" ] && kill -9 "$(cat "/tmp/huddle-$n.pid")" 2>/dev/null || true; done; [ -f /tmp/redis.pid ] && kill -9 "$(cat /tmp/redis.pid)" 2>/dev/null || true
 [ -f /tmp/irc.pid ] && kill -9 "$(cat /tmp/irc.pid)" 2>/dev/null || true
 exit' INT TERM
while :; do
 if [ ! -f /tmp/redis.pid ] || ! kill -0 "$(cat /tmp/redis.pid)" 2>/dev/null; then launch_redis; fi
 if [ ! -f /tmp/irc.pid ] || ! kill -0 "$(cat /tmp/irc.pid)" 2>/dev/null; then launch_irc; fi
 for n in 0 1 2; do
  f="/tmp/huddle-$n.pid"
  if [ ! -f "$f" ] || ! kill -0 "$(cat "$f")" 2>/dev/null; then launch "$n"; fi
 done
 sleep 1
done
