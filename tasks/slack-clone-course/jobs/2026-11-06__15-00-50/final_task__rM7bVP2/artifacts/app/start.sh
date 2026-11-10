#!/bin/sh
set -u
echo $$ > /tmp/start.pid
mkdir -p /app/data
while :; do
 for n in 0 1 2; do
  p=$((8000+n))
  if ! ss -ltnH "sport = :$p" 2>/dev/null | grep -q LISTEN; then
   NODE_ID=$n nohup uvicorn app:app --host 127.0.0.1 --port $p >/tmp/huddle-$n.log 2>&1 &
  fi
 done
 if ! ss -ltnH "sport = :6667" 2>/dev/null | grep -q LISTEN; then
  nohup python /app/irc.py >/tmp/huddle-irc.log 2>&1 &
 fi
 sleep 1
done
