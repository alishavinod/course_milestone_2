#!/usr/bin/env bash
# Self-check for D.3 (performance). Exit 0 only if every rule passes.
# Usage: bash check_d_3.sh
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_d_3() {
  run_py 3 3.0 <<'PY'
from d_lib import *

W = world(3)
WRITERS, EACH = 3, 30   # per node, per writer
sent, failed = {}, []
STOP = time.time() + 14   # writes not started by then count as failed, so the check always reports

def writer(port, n):
    for i in range(EACH):
        body = f"p{port}-{n}-{i}"
        if time.time() > STOP:
            failed.append(body)
            continue
        sent[body] = time.time()
        if call("POST", f"/api/channels/{W.CH}/messages", {"body": body}, W.O, port)[0] != 201:
            failed.append(body)
        time.sleep(0.1)

def measure():
    W.subs = [Sub(p, t, W.CH) for p in PORTS for t in (W.B, W.C)]
    time.sleep(0.3)
    ts = [threading.Thread(target=writer, args=(p, n), daemon=True) for p in PORTS for n in range(WRITERS)]
    for t in ts: t.start()
    for t in ts: t.join()
    end = time.time() + 3
    expected = len(ts) * EACH
    while time.time() < end and any(len(w.got) < expected - len(failed) for w in W.subs):
        time.sleep(0.05)
    W.lat, W.missing = [], 0
    for w in W.subs:
        first = {}
        for at, f in list(w.got):
            body = (f.get("message") or {}).get("body")
            if body in sent and body not in failed: first.setdefault(body, at)
        W.lat.append((w.port, sorted(first[b] - sent[b] for b in first)))
        W.missing += expected - len(first)
    W.expected = expected * len(W.subs)

def worst(fraction):
    """The worst subscriber's latency at this fraction, in ms, with its port."""
    vals = [(1000 * l[min(len(l) - 1, int(fraction * len(l)))], p) for p, l in W.lat if l]
    assert vals, "no subscriber got any message.created frame"
    return max(vals)

def r1():
    measure()
    ms, p = worst(0.5)
    assert ms <= 300, f"a subscriber on {p} has median latency {ms:.0f} ms, limit 300 ms"
def r2():
    ms, p = worst(0.95)
    assert ms <= 800, f"a subscriber on {p} has p95 latency {ms:.0f} ms, limit 800 ms"
def r3():
    pct = 100 * W.missing / W.expected
    by_node = {p: sum(b.startswith(f"p{p}-") for b in failed) for p in PORTS}
    assert pct <= 1, f"{W.missing} of {W.expected} expected frames are missing ({pct:.1f}%, limit 1%); writes that did not return 201 in time, by port: {by_node}"

run_rules(W, [
    ("3.1", "median delivery latency is at most 300 ms", r1),
    ("3.2", "p95 delivery latency is at most 800 ms", r2),
    ("3.3", "at most 1% of expected frames are missing", r3),
])
PY
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_d_3; finish; }
