#!/usr/bin/env bash
set -u
cd /app
echo $$ >/tmp/start.pid
mkdir -p /app/data
redis_pid=""
sub_pids=()
start_redis() {
  if ! ss -ltnH "sport = :6379" | grep -q LISTEN; then
    redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no >/tmp/redis.log 2>&1 &
    redis_pid=$!
    sleep 0.4
  fi
  for n in 0 1 2; do
    if ! ss -ltnH "sport = :6379" | grep -q LISTEN; then break; fi
    tail -f /dev/null | redis-cli -h 127.0.0.1 -p 6379 subscribe "huddle-node-$n" >/tmp/redis-sub$n.log 2>&1 &
    sub_pids[$n]=$!
  done
}
start_redis
if ! ss -ltnH 'sport = :6667' | grep -q LISTEN; then IRC_ONLY=1 NODE_ID=99 nohup python3 -c 'import time,main; time.sleep(100000000)' >/tmp/irc.log 2>&1 & fi
while true; do
  start_redis
  for n in 0 1 2; do
    p=$((8000+n))
    if ! ss -ltnH "sport = :$p" | grep -q LISTEN; then
      NODE_ID=$n nohup python3 -m uvicorn main:app --host 127.0.0.1 --port "$p" >/tmp/node$n.log 2>&1 &
    fi
  done
  sleep 1
done
