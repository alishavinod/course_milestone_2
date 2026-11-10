#!/usr/bin/env bash
# Self-check for C.1 (reactions and DMs). Exit 0 only if every rule passes.
# Usage: bash check_c_1.sh
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_c_1() {
  run_py 9 1.0 <<'PY'
from c_lib import *

W = world(1)
T = "\U0001F44D"

def react(mid, emoji, token):
    return call("POST", f"/api/messages/{mid}/reactions", {"emoji": emoji}, token)
def unreact(mid, emoji, token):
    return call("DELETE", f"/api/messages/{mid}/reactions/{urllib.parse.quote(emoji)}", None, token)
def entry(body, emoji):
    return next((r for r in (body or {}).get("reactions") or [] if r.get("emoji") == emoji), None)

def r1():
    m = message(W, "react one"); W.M = m
    s, b = react(m, T, W.B)
    want(s, 200, "first reaction")
    e = entry(b, T)
    assert e, f"no entry for the emoji in {b}"
    assert e.get("count") == 1 and idset(e.get("user_ids")) == idset([W.B_ID]), f"entry is {e}, want count 1 and user_ids [{W.B_ID}]"
def r2():
    s, b = react(W.M, T, W.B)
    want(s, 200, "same reaction again")
    assert entry(b, T).get("count") == 1, f"same user reacted twice: count {entry(b, T).get('count')}, want 1"
    s, b = react(W.M, T, W.C)
    e = entry(b, T)
    assert s == 200 and e.get("count") == 2 and idset(e.get("user_ids")) == idset([W.B_ID, W.C_ID]), f"second user: status {s}, entry {e}, want count 2 and both ids"
def r3():
    s, b = call("GET", f"/api/channels/{W.CH}/messages", None, W.O)
    want(s, 200, "list messages")
    m = next((x for x in b["messages"] if same(x["id"], W.M)), None)
    assert m, "the message is not listed"
    e = entry(m, T)
    assert e and e.get("count") == 2, f"reactions on the listed message are {m.get('reactions')}, want count 2 for the emoji"
def r4():
    s, b = unreact(W.M, T, W.B)
    want(s, 200, "remove reaction")
    e = entry(b, T)
    assert e and e.get("count") == 1 and idset(e.get("user_ids")) == idset([W.C_ID]), f"after B removes: {e}, want only C left"
def r5():
    a = call("POST", f"/api/messages/{W.M}/reactions", {}, W.B)[0]
    c = react(999999999, T, W.B)[0]
    d = react(W.M, T, None)[0]
    assert (a, c, d) == (400, 404, 401), f"missing emoji {a} (want 400), unknown message {c} (want 404), no token {d} (want 401)"
def r6():
    e = channel(W, f"ev1-{RUN}")
    w, h = subscribed(8000, W.O, e)
    m = message(W, "events", channel_id=e)
    want(react(m, T, W.O)[0], 200, "react")
    want(unreact(m, T, W.O)[0], 200, "remove")
    got = []
    for f in w.frames(2):
        if isinstance(f.get("event_id"), int): got.append((f["event_id"], f.get("type")))
        if len(got) >= 3: break
    wanted = [(h + 1, "message.created"), (h + 2, "reaction.added"), (h + 3, "reaction.removed")]
    assert got == wanted, f"got {got}, want {wanted}"
    w.close()
def r7():
    s, b = call("POST", "/api/dms", {"user_id": W.C_ID}, W.B)
    want(s, 200, "open DM")
    W.DM = (b or {}).get("channel_id")
    assert W.DM is not None, f"no channel_id in {b}"
    again = call("POST", "/api/dms", {"user_id": W.C_ID}, W.B)[1]["channel_id"]
    other = call("POST", "/api/dms", {"user_id": W.B_ID}, W.C)[1]["channel_id"]
    assert same(again, W.DM) and same(other, W.DM), f"channel ids {W.DM}, repeated {again}, other direction {other}; want one id"
def r8():
    a = call("POST", "/api/dms", {}, W.B)[0]
    c = call("POST", "/api/dms", {"user_id": 999999999}, W.B)[0]
    d = call("POST", "/api/dms", {"user_id": W.C_ID})[0]
    assert (a, c, d) == (400, 404, 401), f"missing user_id {a} (want 400), unknown user {c} (want 404), no token {d} (want 401)"
def r9():
    want(post(W, "hi", W.B, W.DM)[0], 201, "first user posts to the DM")
    want(post(W, "hi back", W.C, W.DM)[0], 201, "second user posts to the DM")
    s, b = call("GET", f"/api/channels/{W.DM}/messages", None, W.C)
    assert s == 200 and len(b["messages"]) == 2, f"DM list: status {s}, want 200 with 2 messages"

run_rules(W, [
    ("1.1", "adding a reaction returns 200 with emoji, count and user_ids", r1),
    ("1.2", "reactions are once per user and emoji, and counted across users", r2),
    ("1.3", "the listed message shows the reactions", r3),
    ("1.4", "removing takes away only your own reaction", r4),
    ("1.5", "bad reaction requests return 400, 404 and 401", r5),
    ("1.6", "reactions add reaction.added and reaction.removed events", r6),
    ("1.7", "POST /api/dms returns one channel_id in both directions", r7),
    ("1.8", "bad DM requests return 400, 404 and 401", r8),
    ("1.9", "both users can post to and list the DM", r9),
])
PY
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_c_1; finish; }
