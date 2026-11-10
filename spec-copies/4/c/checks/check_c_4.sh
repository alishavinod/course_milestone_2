#!/usr/bin/env bash
# Self-check for C.4 (user groups and mentions). Exit 0 only if every rule passes.
# Usage: bash check_c_4.sh
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_c_4() {
  run_py 9 4.0 <<'PY'
from c_lib import *

W = world(4)

def groups(path=""):
    return f"/api/workspaces/{W.SLUG}/groups{path}"
def mk(handle, ids, token=None):
    s, b = call("POST", groups(), {"handle": handle, "name": handle, "user_ids": ids}, token or W.B)
    want(s, 201, f"create group {handle}")
    return b["group"]
def ments(body, token=None, ch=None):
    s, b = post(W, body, token, ch)
    want(s, 201, f"post {body!r}")
    return idset(b["message"].get("mentions"))
def mcount(token, ch):
    s, b = call("GET", f"/api/channels/{ch}/read", None, token)
    want(s, 200, "GET read")
    return b["read_state"]["mention_count"]
def joined():
    """A new channel with B in it. C is not in it, and O (the owner) writes."""
    ch = channel(W, f"m{os.urandom(3).hex()}")
    want(call("POST", f"/api/channels/{ch}/join", {}, W.B)[0], 200, "join")
    return ch

def r1():
    g = mk(f"team{RUN}", [W.B_ID, W.C_ID])
    W.G = g
    assert g.get("handle") == f"team{RUN}" and g.get("id") is not None and "name" in g, f"group is {g}"
    assert idset(g.get("member_user_ids")) == idset([W.B_ID, W.C_ID]), f"member_user_ids is {g.get('member_user_ids')}"
    assert "members" not in g, "the group has a 'members' field; use member_user_ids"
    a = call("POST", groups(), {"handle": "out", "name": "x", "user_ids": []}, W.N)[0]
    c = call("POST", groups(), {"handle": "out", "name": "x", "user_ids": []})[0]
    assert (a, c) == (403, 401), f"from outside the workspace {a} (want 403), no token {c} (want 401)"
def r2():
    s, b = call("GET", groups(), None, W.C)
    hs = [g.get("handle") for g in (b or {}).get("groups", [])]
    assert s == 200 and W.G["handle"] in hs, f"list: status {s}, handles {hs}"
def r3():
    a = call("POST", groups(), {"handle": W.G["handle"], "name": "x", "user_ids": []}, W.B)[0]
    c = call("POST", groups(), {"name": "x", "user_ids": []}, W.B)[0]
    d = call("POST", f"/api/workspaces/none-{RUN}/groups", {"handle": "h", "name": "x", "user_ids": []}, W.B)[0]
    assert (a, c, d) == (409, 400, 404), f"duplicate {a} (want 409), missing handle {c} (want 400), unknown workspace {d} (want 404)"
def r4():
    h = groups("/" + W.G["handle"])
    s, b = call("PATCH", h, {"name": "New", "user_ids": [W.B_ID]}, W.B)
    g = (b or {}).get("group") or {}
    assert s == 200 and g.get("name") == "New" and idset(g.get("member_user_ids")) == idset([W.B_ID]), f"creator update: status {s}, group {g}"
    want(call("PATCH", h, {"name": "Own"}, W.O)[0], 200, "workspace owner update")
    want(call("PATCH", h, {"name": "x"}, W.C)[0], 403, "update by another member")
def r5():
    h = f"del{RUN}"
    mk(h, [W.B_ID])
    want(call("DELETE", groups("/" + h), None, W.C)[0], 403, "delete by another member")
    for who, name in ((W.B, "creator"), (W.O, "again")):
        s, b = call("DELETE", groups("/" + h), None, who)
        assert s == 200 and (b or {}).get("deleted") is True, f"delete ({name}): status {s}, body {b}, want 200 with deleted true"
    k = f"own{RUN}"
    mk(k, [])
    want(call("DELETE", groups("/" + k), None, W.O)[0], 200, "delete by the workspace owner")
def r6():
    h = f"my-grp{RUN}"
    mk(h, [W.O_ID, W.B_ID, W.C_ID])   # C is not in the channel, O is the author
    W.H = h
    got = ments(f"hi @{h}", W.O, joined())
    assert got == idset([W.B_ID]), f"@group gave {got}, want only B: not the author, not a member outside the channel"
def r7():
    ch = joined()
    want(call("POST", f"/api/channels/{ch}/join", {}, W.C)[0], 200, "join")
    wb = WS(8001, W.B)   # in the channel, on another node; C has no WebSocket
    time.sleep(0.5)
    got = ments("@here", W.O, ch)
    assert got == idset([W.B_ID]), f"@here gave {got}, want only B, whose WebSocket is open on another node and has not sent subscribe (C has no WebSocket)"
    wb.close()
    time.sleep(3)
    got = ments("@here", W.O, ch)
    assert got == [], f"3s after B closed the WebSocket, @here still gave {got}"
def r8():
    ch = joined()
    w = WS(8002, W.B); time.sleep(0.5)
    before = mcount(W.B, ch)
    got = ments(f"@{W.B_NAME} @{W.H} @here", W.O, ch)
    w.close()
    assert got == idset([W.B_ID]), f"mentions are {got}, want B once"
    assert mcount(W.B, ch) == before + 1, f"mention_count went from {before} to {mcount(W.B, ch)}, want +1"
def r9():
    ch = joined()
    w = WS(8001, W.B); time.sleep(0.5)
    before = mcount(W.B, ch)
    got = ments(f"@{W.B_NAME} @{W.H} @here", W.B, ch)
    w.close()
    assert str(W.B_ID) not in got, f"the author is in their own mentions {got}"
    assert mcount(W.B, ch) == before, f"the author's mention_count went from {before} to {mcount(W.B, ch)}"

run_rules(W, [
    ("4.1", "any workspace member creates a group, with member_user_ids", r1),
    ("4.2", "GET lists the groups", r2),
    ("4.3", "bad group creates return 409, 400 and 404", r3),
    ("4.4", "PATCH is for the creator or owner", r4),
    ("4.5", "DELETE is for the creator or owner and is idempotent", r5),
    ("4.6", "@group expands to members in the channel, without the author", r6),
    ("4.7", "@here expands to connected channel members on any node", r7),
    ("4.8", "mention forms are deduplicated and counted once", r8),
    ("4.9", "mentioning yourself never adds to your mentions or mention_count", r9),
])
PY
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_c_4; finish; }
