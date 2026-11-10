#!/usr/bin/env bash
set -u
echo $$ > /tmp/start.pid
mkdir -p /app/data
pids=()
gateway_pid=""
redis_pid=""
cleanup(){ for p in "$gateway_pid"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done; for p in "${pids[@]:-}" "$redis_pid"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT
while true; do
  if [ -z "$gateway_pid" ] || ! kill -0 "$gateway_pid" 2>/dev/null; then
    if ! ss -ltnH "sport = :6667" 2>/dev/null | grep -q .; then
      GATEWAY_ONLY=1 nohup /opt/venv/bin/python /app/main.py >>/tmp/irc.log 2>&1 &
      gateway_pid=$!
      for _ in $(seq 1 10); do ss -ltnH "sport = :6667" 2>/dev/null | grep -q . && break; sleep 0.2; done
    fi
  fi
  if [ -z "$redis_pid" ] || ! kill -0 "$redis_pid" 2>/dev/null; then
    if ! (ss -ltnpH "sport = :6379" 2>/dev/null | grep -q 'redis-server'); then
      redis-server --bind 127.0.0.1 --port 6379 --daemonize no >>/tmp/redis.log 2>&1 &
      redis_pid=$!
      pids+=("$redis_pid")
    else
      redis_pid=$(ss -ltnpH "sport = :6379" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
    fi
  fi
  for i in 0 1 2; do
    port=$((8000+i))
    alive=0
    for p in $(ss -ltnpH "sport = :$port" 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u); do kill -0 "$p" 2>/dev/null && alive=1; done
    if [ "$alive" = 0 ]; then
      NODE_ID=$i PORT=$port nohup /opt/venv/bin/python /app/main.py >>"/tmp/node-$i.log" 2>&1 &
      pids+=("$!")
    fi
  done
  sleep 1
done
