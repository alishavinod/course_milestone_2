#!/bin/sh
echo $$ > /tmp/start.pid
mkdir -p /app/data
for i in 0 1 2; do NODE_ID=$i python -m uvicorn app:app --host 127.0.0.1 --port $((8000+i)) >/tmp/huddle-$i.log 2>&1 & echo $! >/tmp/huddle-$i.pid; done
python /app/irc.py >/tmp/huddle-irc.log 2>&1 & echo $! >/tmp/huddle-irc.pid
trap 'kill -9 $(cat /tmp/huddle-0.pid /tmp/huddle-1.pid /tmp/huddle-2.pid /tmp/huddle-irc.pid) 2>/dev/null; exit' TERM INT
while :; do sleep 2; for i in 0 1 2; do if ! kill -0 $(cat /tmp/huddle-$i.pid) 2>/dev/null; then NODE_ID=$i python -m uvicorn app:app --host 127.0.0.1 --port $((8000+i)) >/tmp/huddle-$i.log 2>&1 & echo $! >/tmp/huddle-$i.pid; fi; done; if ! kill -0 $(cat /tmp/huddle-irc.pid) 2>/dev/null; then python /app/irc.py >/tmp/huddle-irc.log 2>&1 & echo $! >/tmp/huddle-irc.pid; fi; done
