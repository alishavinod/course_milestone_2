#!/usr/bin/env bash
# Self-check for Channels.4 (read state, auth and unknown ids). Prints PASS/FAIL per rule in /spec/channels/channels-4.md.
# Usage: bash /spec/channels/checks/check_channels_4.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_channels_4() {
  local a b c d e g
  # setup from Spec A and Channels.1: users O and B in workspace WS, public channel CH
  need_base; need_ch

  # B reads channel RS, where O posts 3 messages, one mentioning B. Event ids come from a WebSocket.
  run_py 6 6.0 <<'PY'
import os, sys; sys.path.insert(0, os.environ["CHECKS"])
from ws_lib import *

S = {}
s, b = call("POST", f"/api/workspaces/{SLUG}/channels", {"name": f"rs-{RUN}"}, O)
RS = (b or {}).get("channel", {}).get("id")

def r61():
    assert call("POST", f"/api/channels/{RS}/join", {}, B)[0] == 200, "B could not join the channel"
    w = WS(8000, B); w.send({"type": "subscribe", "channel_id": RS}); w.first(is_sub)
    ids = []
    for body in ("one", "two", f"hi @u{RUN}_B"):
        s, b = call("POST", f"/api/channels/{RS}/messages", {"body": body}, O)
        assert s == 201, "post message failed"
        ids.append(b["message"]["id"])
    # a thread reply: it adds an event but must not count as unread
    assert call("POST", f"/api/channels/{RS}/messages", {"body": "a reply", "parent_id": ids[0]}, O)[0] == 201, "post reply failed"
    msgs = []  # event ids of the 3 top-level messages (other event types are ignored here)
    for f in w.frames():
        if str(f.get("channel_id")) == str(RS) and f.get("type") == "message.created":
            msgs.append(f["event_id"])
            if len(msgs) == 3: break
    assert len(msgs) == 3, f"expected 3 message.created events on the WebSocket, got {msgs}"
    S["h2"], S["h3"] = msgs[1], msgs[2]
    s, b = call("GET", f"/api/channels/{RS}/read", token=B)
    rs = (b or {}).get("read_state") or {}
    assert s == 200, f"GET read got {s}"
    missing = [k for k in ("channel_id", "last_read_event_id", "unread_count", "mention_count") if k not in rs]
    assert not missing, f"read_state is missing {missing}"
    assert rs["last_read_event_id"] == 0, f"last_read_event_id starts at {rs['last_read_event_id']}, want 0"
    S["rs"] = rs
def r62():
    rs = S["rs"]
    assert (rs["unread_count"], rs["mention_count"]) == (3, 1), \
        f"unread_count {rs['unread_count']}, mention_count {rs['mention_count']} (want 3 and 1)"
    # the author's own messages never count as unread for the author
    s, b = call("GET", f"/api/channels/{RS}/read", token=O)
    mine = ((b or {}).get("read_state") or {}).get("unread_count")
    assert mine == 0, f"the 3 messages count as unread for their own author: unread_count {mine} (want 0)"
def r63():
    # mark the 2nd message read: only the 3rd (which mentions B) is still unread
    s, b = call("POST", f"/api/channels/{RS}/read", {"last_read_event_id": S["h2"]}, B)
    rs = (b or {}).get("read_state") or {}
    assert s == 200, f"POST read got {s}"
    got = (rs.get("last_read_event_id"), rs.get("unread_count"), rs.get("mention_count"))
    assert got == (S["h2"], 1, 1), f"after marking the 2nd of 3 messages read: last_read, unread, mentions = {got}, want ({S['h2']}, 1, 1)"
    # mark the latest read: both counts 0, in the POST answer and in a later GET
    call("POST", f"/api/channels/{RS}/read", {"last_read_event_id": S["h3"]}, B)
    s, b = call("GET", f"/api/channels/{RS}/read", token=B)
    rs = (b or {}).get("read_state") or {}
    got = (rs.get("last_read_event_id"), rs.get("unread_count"), rs.get("mention_count"))
    assert got == (S["h3"], 0, 0), f"GET read after marking the latest read: last_read, unread, mentions = {got}, want ({S['h3']}, 0, 0)"
def r64():
    call("POST", f"/api/channels/{RS}/read", {"last_read_event_id": S["h3"]}, B)
    call("POST", f"/api/channels/{RS}/read", {"last_read_event_id": S["h3"] - 2}, B)
    s, b = call("GET", f"/api/channels/{RS}/read", token=B)
    lr = ((b or {}).get("read_state") or {}).get("last_read_event_id")
    assert lr == S["h3"], f"last_read_event_id moved back to {lr}, want {S['h3']}"
def r65():
    s, b = call("GET", f"/api/workspaces/{SLUG}", token=B)
    rs = (b or {}).get("read_state")
    items = rs if isinstance(rs, list) else list(rs.values()) if isinstance(rs, dict) else []
    mine = [r for r in items if isinstance(r, dict) and str(r.get("channel_id")) == str(RS)]
    assert mine, f"no read_state entry for the channel in workspace detail (got {json.dumps(rs)[:80]})"
    assert mine[0].get("last_read_event_id") == S["h3"], f"last_read_event_id there is {mine[0].get('last_read_event_id')}"

rule("6.1", "GET read returns read_state starting at 0", r61)
rule("6.2", "unread_count and mention_count count new top-level messages by others, not replies", r62)
rule("6.3", "marking read recounts unread and mentions (partly read, then all read)", r63)
rule("6.4", "last_read_event_id never moves back", r64)
rule("6.5", "workspace detail has the same read_state", r65)
def r66():
    s, b = call("POST", f"/api/workspaces/{SLUG}/channels", {"name": f"ro-{RUN}"}, O)
    assert s == 201, f"create channel got {s}"
    ch, first = b["channel"]["id"], None
    # B joins, then topic changes before, between and after messages, then a reply
    steps = [("join", ""), ("topic", "a"), ("topic", "b"), ("msg", "m1"), ("topic", "c"), ("msg", "m2"),
             ("msg", f"hi @u{RUN}_B"), ("topic", "d"), ("reply", "r")]
    for kind, val in steps:
        if kind == "join": s = call("POST", f"/api/channels/{ch}/join", {}, B)[0]
        elif kind == "topic": s = call("PATCH", f"/api/channels/{ch}", {"topic": val}, O)[0]
        elif kind == "msg":
            s, b = call("POST", f"/api/channels/{ch}/messages", {"body": val}, O)
            first = first or (b or {}).get("message", {}).get("id")
        else: s = call("POST", f"/api/channels/{ch}/messages", {"body": val, "parent_id": first}, O)[0]
        assert s in (200, 201), f"{kind} request got {s}"
    w = WS(8000, B); w.send({"type": "resume", "channel_id": ch, "since_event_id": 0})
    msgs = []
    for f in w.frames():
        if f.get("type") == "resumed": break
        if f.get("type") == "message.created": msgs.append(f["event_id"])
    assert len(msgs) == 3, f"resume from 0 gave {len(msgs)} message.created events, want 3"
    got = []
    for e in msgs:
        s, b = call("POST", f"/api/channels/{ch}/read", {"last_read_event_id": e}, B)
        rs = (b or {}).get("read_state") or {}
        got.append((rs.get("unread_count"), rs.get("mention_count")))
    want = [(2, 1), (1, 1), (0, 0)]
    assert got == want, f"(unread, mentions) after marking message 1, 2, 3 read: {got}, want {want}"
rule("6.6", "read counts follow the event order (joins and topic changes in between)", r66)
PY

  a=$(call GET $N0/api/channels/${CH:-0}/messages); b=$(call POST $N0/api/channels/${CH:-0}/messages '{"body":"x"}')
  c=$(call GET "$N0/api/search?q=x"); d=$(call GET $N0/api/channels/${CH:-0}/pins)
  e=$(call GET $N0/api/channels/${CH:-0}/read); g=$(call PATCH $N0/api/channels/${CH:-0} '{"topic":"x"}')
  if [ "$a$b$c$d$e$g" = 401401401401401401 ]; then ok 7.1 "Spec B routes return 401 without a token"
  else bad 7.1 "no token: list messages $a, post $b, search $c, pins $d, read $e, set topic $g (want 401 each)"; fi

  a=$(call GET $N0/api/channels/999999999/messages "" "$O_T"); b=$(call POST $N0/api/channels/999999999/messages '{"body":"x"}' "$O_T")
  c=$(call POST $N0/api/channels/999999999/archive "{}" "$O_T"); d=$(call POST $N0/api/messages/999999999/pin "{}" "$O_T")
  e=$(call GET $N0/api/messages/999999999/replies "" "$O_T")
  if [ "$a$b$c$d$e" = 404404404404404 ]; then ok 7.2 "unknown channel or message returns 404"
  else bad 7.2 "unknown id: list messages $a, post $b, archive $c, pin $d, replies $e (want 404 each)"; fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_channels_4; finish; }
