#!/usr/bin/env bash
# Self-check for D.1 (a killed HTTP node). Exit 0 only if every rule passes.
# Usage: bash check_d_1.sh
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_d_1() {
  run_py 5 1.0 <<'PY'
from d_lib import *

W = world(1)
KILLED, A, B = 8000, 8001, 8002

def r1():
    W.before = write(W, KILLED)
    W.w0, W.wa, W.wb = (Sub(p, W.B, W.CH) for p in (KILLED, A, B))
    W.head = W.wa.head
    kill_port(KILLED)
    W.ma, W.mb = write(W, A), write(W, B)
    for p, m, what in ((A, W.before, "the message from before the kill"), (B, W.before, "the message from before the kill"),
                       (A, W.mb, f"the message written on {B}"), (B, W.ma, f"the message written on {A}")):
        assert listed(W, p, m), f"port {p} does not list {what}"
def r2():
    end = time.time() + 3
    assert not W.wa.wait([W.mb], end), f"the subscriber on {A} got no frame for a message written on {B} within 3s"
    assert not W.wb.wait([W.ma], end), f"the subscriber on {B} got no frame for a message written on {A} within 3s"
def r3():
    w = WS(A, W.B)
    w.send({"type": "resume", "channel_id": W.CH, "since_event_id": W.head})
    ev, end = [], None
    for f in w.frames(3):
        if f.get("type") == "resumed": end = f; break
        if "event_id" in f: ev.append(f)
    w.close()
    assert end, "no 'resumed' frame within 3s"
    ids = [f["event_id"] for f in ev]
    assert ids == list(range(W.head + 1, end.get("head_event_id", 0) + 1)) and len(ids) >= 2, f"replay after {W.head} gave event ids {ids}, 'resumed' head {end.get('head_event_id')}"
    got = {str((f.get("message") or {}).get("id")) for f in ev}
    assert {str(W.ma), str(W.mb)} <= got, "the replay left out messages written while the node was down"
def r4():
    t, end = time.time(), time.time() + 15
    while time.time() < end:
        s, b = call("GET", "/api/health", port=KILLED, timeout=1)
        if s == 200: break
        time.sleep(0.2)
    assert s == 200, "the killed node did not come back within 15s"
    assert (b or {}).get("node_id") == 0, f"port {KILLED} came back with node_id {(b or {}).get('node_id')}, want 0"
def r5():
    want(call("GET", "/api/auth/me", None, W.B, KILLED)[0], 200, "a token from before the kill")
    assert listed(W, KILLED, W.ma), f"port {KILLED} does not list a message written while it was down"
    w0 = Sub(KILLED, W.B, W.CH)
    ms = [write(W, p) for p in PORTS]
    end = time.time() + 4
    for w in (w0, W.wa, W.wb):
        missing = w.wait(ms, end)
        assert not missing, f"the subscriber on {w.port} did not get {len(missing)} of 3 messages within 4s"
    W.wa.check_dense(); W.wb.check_dense()

run_rules(W, [
    ("1.1", "the other two nodes read and write after a node is killed", r1),
    ("1.2", "WebSocket delivery continues between the other two nodes", r2),
    ("1.3", "resume on another node replays what was missed", r3),
    ("1.4", "the killed node comes back with its own node_id", r4),
    ("1.5", "all three nodes work again, with no gaps in event_id", r5),
])
PY
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_d_1; finish; }
