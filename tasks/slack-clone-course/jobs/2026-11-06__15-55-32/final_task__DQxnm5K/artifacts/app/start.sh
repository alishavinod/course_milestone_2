#!/bin/sh
set -eu
echo $$ >/tmp/start.pid
mkdir -p /app/data
start_redis() {
  if ! ss -ltnH "sport = :6379" | grep -q LISTEN; then
    redis-server --bind 127.0.0.1 --port 6379 --daemonize yes --pidfile /tmp/huddle-redis.pid --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 || true
  fi
}
start_node() {
  n="$1"; port=$((8000+n))
  if ! ss -ltnH "sport = :$port" | grep -q LISTEN; then
    NODE_ID="$n" PORT="$port" python -m uvicorn app:app --host 127.0.0.1 --port "$port" >>"/tmp/huddle-$n.log" 2>&1 &
    echo $! >"/tmp/huddle-$n.pid"
  fi
}
start_redis
start_node 0
start_node 1
start_node 2
start_irc() {
  if ! ss -ltnH 'sport = :6667' | grep -q LISTEN; then
    python /app/irc.py >>/tmp/huddle-irc.log 2>&1 &
    echo $! >/tmp/huddle-irc.pid
  fi
}
start_irc
trap 'for n in 0 1 2; do if [ -f /tmp/huddle-$n.pid ]; then kill "$(cat /tmp/huddle-$n.pid)" 2>/dev/null || true; fi; done; if [ -f /tmp/huddle-irc.pid ]; then kill "$(cat /tmp/huddle-irc.pid)" 2>/dev/null || true; fi; exit 0' INT TERM
while :; do
  sleep 1
  start_redis
  start_node 0
  start_node 1
  start_node 2
  start_irc
done
