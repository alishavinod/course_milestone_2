#!/usr/bin/env bash
# Self-check for C.5 (profiles). Exit 0 only if every rule passes.
# Usage: bash check_c_5.sh
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_c_5() {
  run_py 5 5.0 <<'PY'
from c_lib import *

W = world(5)
FIELDS = ("id", "username", "display_name", "timezone", "avatar_url", "status_text", "status_emoji")

def me(body, token=None):
    return call("PATCH", "/api/users/me", body, token or W.B)

def r1():
    s, b = call("GET", f"/api/users/{W.C_ID}", None, W.B)
    missing = [k for k in FIELDS if k not in ((b or {}).get("user") or {})]
    assert s == 200 and not missing, f"GET user: status {s}, missing {missing}"
    a = call("GET", "/api/users/999999999", None, W.B)[0]
    c = call("GET", f"/api/users/{W.C_ID}")[0]
    assert (a, c) == (404, 401), f"unknown id {a} (want 404), no token {c} (want 401)"
def r2():
    s, b = me({"display_name": "Bee", "status_text": "busy"})
    u = (b or {}).get("user") or {}
    assert s == 200 and u.get("display_name") == "Bee" and u.get("status_text") == "busy", f"PATCH: status {s}, user {u}"
    s, b = me({"status_emoji": "bee"})
    u = (b or {}).get("user") or {}
    assert s == 200 and u.get("display_name") == "Bee" and u.get("status_emoji") == "bee", f"a second PATCH lost earlier fields: status {s}, user {u}"
    u = call("GET", f"/api/users/{W.B_ID}", None, W.B)[1]["user"]
    got = (u.get("display_name"), u.get("status_text"), u.get("status_emoji"))
    assert got == ("Bee", "busy", "bee"), f"GET user shows {got}, so the change was not saved"
    want(call("PATCH", "/api/users/me", {"status_text": "x"})[0], 401, "PATCH with no token")
def r3():
    s, b = me({"timezone": "America/New_York"})
    assert s == 200 and b["user"]["timezone"] == "America/New_York", f"valid time zone: status {s}, user {b}"
    want(me({"timezone": "Not/AZone"})[0], 400, "invalid time zone")
def r4():
    limits = {"display_name": 80, "status_text": 100, "status_emoji": 32, "avatar_url": 1024}
    for k, n in limits.items():
        a, c = me({k: "y" * n})[0], me({k: "y" * (n + 1)})[0]
        assert (a, c) == (200, 400), f"{k} of {n} characters {a} (want 200), of {n + 1} characters {c} (want 400)"
def r5():
    # N has not subscribed to anything
    w0, w2 = WS(8000, W.N), WS(8002, W.N)
    time.sleep(0.5)
    want(me({"status_text": f"away {RUN}"}, W.C)[0], 200, "PATCH")
    match = lambda f: f.get("type") == "user.updated" and same((f.get("user") or {}).get("id"), W.C_ID)
    for w, p in ((w0, 8000), (w2, 8002)):
        f = w.first(match, 3)
        assert f, f"a WebSocket on {p} got no user.updated within 3s"
        assert "channel_id" not in f and "event_id" not in f, f"user.updated must have no channel_id or event_id; got {str(f)[:160]}"
    w0.close(); w2.close()

run_rules(W, [
    ("5.1", "GET user returns every field, unknown 404, no token 401", r1),
    ("5.2", "PATCH saves any subset of the fields", r2),
    ("5.3", "a valid time zone is kept and an invalid one is 400", r3),
    ("5.4", "field lengths over the limits are 400", r4),
    ("5.5", "user.updated reaches every WebSocket, with no channel_id or event_id", r5),
])
PY
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_c_5; finish; }
