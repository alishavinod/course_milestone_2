#!/usr/bin/env bash
# Self-check for E.1 (message edit and delete). Exit 0 only if every rule passes.
# Usage: bash check_e_1.sh
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_e_1() {
  run_py 6 1.0 <<'PY'
from e_lib import *

W = world(1)

def edit(m, token, body="new text", port=8000):
    return call("PATCH", f"/api/messages/{m}", {"body": body}, token, port)
def remove(m, token):
    return call("DELETE", f"/api/messages/{m}", None, token)
def by(token):
    s, b = call("POST", f"/api/channels/{W.CH}/messages", {"body": "text"}, token)
    want(s, 201, "setup: post")
    return b["message"]["id"]

def r1():
    m = by(W.B)
    s, b = edit(m, W.B)
    got = (b or {}).get("message") or {}
    assert s == 200 and got.get("body") == "new text" and got.get("edited_at"), f"edit: status {s}, message {got}"
    for p in PORTS:
        s, b = call("GET", f"/api/channels/{W.CH}/messages?limit=100", None, W.B, p)
        row = [x for x in (b or {}).get("messages", []) if str(x.get("id")) == str(m)]
        assert row and row[0].get("body") == "new text" and row[0].get("edited_at"), f"port {p} shows {row}"
def r2():
    m = by(W.B)
    a, c, d, e = edit(m, W.C)[0], edit(m, W.O)[0], edit(999999999, W.B)[0], edit(m, None)[0]
    assert (a, c, d, e) == (403, 403, 404, 401), f"other member {a} (want 403), workspace owner {c} (want 403), unknown id {d} (want 404), no token {e} (want 401)"
def r3():
    m = by(W.B)
    want(remove(m, W.B)[0], 200, "delete")
    want(edit(m, W.B)[0], 409, "editing a deleted message")
def r4():
    m = by(W.B)
    want(remove(m, W.C)[0], 403, "delete by another member")
    want(remove(m, None)[0], 401, "delete with no token")
    for token, who in ((W.B, "the author"), (W.B, "the author again")):
        s, b = remove(m, token)
        assert s == 200 and b == {"deleted": True}, f"delete by {who}: status {s}, body {b}"
    m = by(W.B)
    s, b = remove(m, W.O)
    assert s == 200 and b == {"deleted": True}, f"delete by the workspace owner: status {s}, body {b}"
def r5():
    m = by(W.B)
    want(remove(m, W.B)[0], 200, "delete")
    s, b = call("GET", f"/api/channels/{W.CH}/messages?limit=100", None, W.B)
    assert str(m) not in {str(x.get("id")) for x in (b or {}).get("messages", [])}, "the deleted message is still listed"
def r6():
    m = by(W.B)
    w = Sub(8001, W.B, W.CH)
    want(edit(m, W.B)[0], 200, "edit")
    want(remove(m, W.B)[0], 200, "delete")
    want(remove(m, W.B)[0], 200, "second delete")
    n = by(W.B)
    end = time.time() + 3
    while time.time() < end and len(w.got) < 3: time.sleep(0.02)
    fr = [f for _, f in w.got]
    kinds = [f.get("type") for f in fr]
    assert kinds == ["message.updated", "message.deleted", "message.created"], f"frames in order are {kinds}; a repeated delete adds no event"
    assert [f["event_id"] for f in fr] == [w.head + 1, w.head + 2, w.head + 3], f"event ids {[f['event_id'] for f in fr]}, head {w.head}"
    assert (fr[0].get("message") or {}).get("body") == "new text", f"message.updated has {fr[0]}"
    assert str(fr[1].get("message_id")) == str(m), f"message.deleted has {fr[1]}"

run_rules(W, [
    ("1.1", "the author edits, edited_at is set, every node shows it", r1),
    ("1.2", "only the author can edit, unknown id 404", r2),
    ("1.3", "editing a deleted message is 409", r3),
    ("1.4", "the author or owner deletes, and doing it again is fine", r4),
    ("1.5", "a deleted message is not listed", r5),
    ("1.6", "edit and delete each add one event", r6),
])
PY
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_e_1; finish; }
