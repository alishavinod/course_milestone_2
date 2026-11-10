#!/usr/bin/env bash
# Self-check for D.2 (a killed Redis). Exit 0 only if every rule passes.
# Usage: bash check_d_2.sh
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_d_2() {
  run_py 6 2.0 <<'PY'
from d_lib import *

W = world(2)
if not W.error:
    try:
        W.subs = [Sub(p, W.B, W.CH) for p in PORTS]
    except Exception as e:
        W.error = f"setup: {e}"

def subscribers_at_least_3(seconds):
    end = time.time() + seconds
    while time.time() < end and redis_subscribers() < 3:
        time.sleep(0.1)
    return redis_subscribers()

def r1():
    assert redis("PING") == "PONG", f"127.0.0.1:6379 answered PING with {redis('PING')!r}"
    n = subscribers_at_least_3(2)
    assert n >= 3, f"{n} of the 3 node processes hold a Redis pub/sub subscription, want all 3"
def r2():
    W.t0, W.sent = time.time(), []
    kill_port(6379)
    for _ in range(3):
        W.sent += [write(W, p) for p in PORTS]
        time.sleep(0.3)
def r3():
    assert W.sent, "no writes succeeded"
    for w in W.subs:
        missing = w.wait(W.sent, W.t0 + 5)
        assert not missing, f"the subscriber on {w.port} did not get {len(missing)} of {len(W.sent)} messages within 5s of the kill"
def r4():
    assert W.sent, "no writes succeeded"
    for w in W.subs: w.check_dense()
def r5():
    end = W.t0 + 10
    while time.time() < end and redis("PING", 0.5) != "PONG":
        time.sleep(0.2)
    assert redis("PING") == "PONG", "Redis did not answer PING within 10s of the kill"
def r6():
    n = subscribers_at_least_3(5)
    assert n >= 3, f"5s after Redis came back {n} of the 3 node processes hold a subscription, want all 3"
    ms = [write(W, p) for p in PORTS]
    end = time.time() + 3
    for w in W.subs:
        missing = w.wait(ms, end)
        assert not missing, f"the subscriber on {w.port} did not get {len(missing)} of 3 messages within 3s"
        w.check_dense()

run_rules(W, [
    ("2.1", "Redis answers on 6379 and each node holds a subscription", r1),
    ("2.2", "writes on every node succeed right after Redis is killed", r2),
    ("2.3", "every subscriber gets those messages within 5s of the kill", r3),
    ("2.4", "event_id has no gaps and no repeats across the outage", r4),
    ("2.5", "Redis comes back without any action", r5),
    ("2.6", "pub/sub subscriptions and delivery resume", r6),
])
PY
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_d_2; finish; }
