#!/bin/sh
set -eu
echo $$ > /tmp/start.pid
mkdir -p /app/data
run_node() {
  p="$1"; n=$((p-8000))
  while :; do
    NODE_ID="$n" PORT="$p" python3 /app/app.py >>"/tmp/node-$p.log" 2>&1 || true
    sleep 0.2
  done
}
run_redis() { while :; do redis-server --bind 127.0.0.1 --port 6379 --daemonize yes >/tmp/redis.log 2>&1 || true; sleep 1; done; }
run_redis & echo $! > /tmp/huddle-redis.pid
sleep 1
for n in 0 1 2; do (while :; do redis-cli -h 127.0.0.1 -p 6379 subscribe huddle-events >/dev/null 2>&1 || true; sleep 1; done) & echo $! > /tmp/huddle-redis-sub-$n.pid; done
run_node 8000 & echo $! > /tmp/huddle-8000.pid
run_node 8001 & echo $! > /tmp/huddle-8001.pid
run_node 8002 & echo $! > /tmp/huddle-8002.pid
run_irc() { while :; do python3 /app/irc.py >>/tmp/irc.log 2>&1 || true; sleep 0.2; done; }
run_irc & echo $! > /tmp/huddle-irc.pid
trap 'kill $(cat /tmp/huddle-8000.pid) $(cat /tmp/huddle-8001.pid) $(cat /tmp/huddle-8002.pid) $(cat /tmp/huddle-irc.pid) $(cat /tmp/huddle-redis.pid) $(cat /tmp/huddle-redis-sub-0.pid) $(cat /tmp/huddle-redis-sub-1.pid) $(cat /tmp/huddle-redis-sub-2.pid) 2>/dev/null || true' TERM INT EXIT
wait
