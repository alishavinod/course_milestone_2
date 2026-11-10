#!/bin/sh
set -eu
echo $$ > /tmp/start.pid
mkdir -p /app/data
pids=""
trap 'for p in $pids; do kill "$p" 2>/dev/null || true; done; exit 0' TERM INT EXIT
python /app/irc.py >/tmp/huddle-irc.log 2>&1 &
irc_pid=$!
pids="$pids $irc_pid"
for n in 0 1 2; do
  NODE_ID=$n python /app/server.py --node $n >/tmp/huddle-$n.log 2>&1 &
  pids="$pids $!"
done
while :; do
  if ! ss -ltnH 'sport = :6667' | grep -q LISTEN; then python /app/irc.py >/tmp/huddle-irc.log 2>&1 & pids="$pids $!"; fi
  alive=""
  for n in 0 1 2; do
    port=$((8000+n))
    if ! ss -ltnH "sport = :$port" | grep -q LISTEN; then
      NODE_ID=$n python /app/server.py --node $n >/tmp/huddle-$n.log 2>&1 &
      pids="$pids $!"
    fi
  done
  sleep 1
done
