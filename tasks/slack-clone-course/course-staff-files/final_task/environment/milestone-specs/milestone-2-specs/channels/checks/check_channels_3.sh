#!/usr/bin/env bash
# Self-check for Channels.3 (mentions and events). Prints PASS/FAIL per rule in /spec/channels/channels-3.md.
# Usage: bash /spec/channels/checks/check_channels_3.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# ment <body>  -> posts as O in CH and prints the sorted mention ids
ment() {
  call POST $N0/api/channels/${CH:-0}/messages "{\"body\":\"$1\"}" "$O_T" >/dev/null
  pick '",".join(sorted(str(x) for x in d["message"]["mentions"]))'
}
sorted_ids() { printf '%s\n' "$@" | sort | paste -sd, -; }

rules_channels_3() {
  local m u t want MCH
  # setup from Spec A and Channels.1: workspace WS and public channel CH; B and C join CH here
  need_base; need_ch
  for u in B C; do t="${u}_T"; call POST $N0/api/channels/${CH:-0}/join "{}" "${!t}" >/dev/null; done

  m=$(ment "hi @u${RUN}_B")
  if [ "$m" = "$B_ID" ]; then ok 4.1 "@username is mentioned"
  else bad 4.1 "'hi @user' gave mentions [$m] (want [$B_ID])"; fi

  m=$(ment "@u${RUN}_B @u${RUN}_B @u${RUN}_O")
  if [ "$m" = "$B_ID" ]; then ok 4.2 "mentions are deduped and skip yourself"
  else bad 4.2 "'@b @b @me' gave mentions [$m] (want [$B_ID])"; fi

  m=$(ment "mail me@u${RUN}_B.com or abc@u${RUN}_B")
  if [ -z "$m" ]; then ok 4.3 "email and in-word @ are not mentions"
  else bad 4.3 "email and in-word @ gave mentions [$m] (want [])"; fi

  m=$(ment "hello @u${RUN}_N")
  if [ -z "$m" ]; then ok 4.4 "users outside the workspace are not mentioned"
  else bad 4.4 "a user outside the workspace gave mentions [$m] (want [])"; fi

  m=$(ment "@channel look"); want=$(sorted_ids "$B_ID" "$C_ID")
  if [ "$m" = "$want" ]; then ok 4.5 "@channel mentions every member except the author"
  else bad 4.5 "@channel gave mentions [$m] (want [$want])"; fi

  call POST $N0/api/workspaces/$WS/channels "{\"name\":\"men-$RUN\"}" "$O_T" >/dev/null; MCH=$(field channel.id)
  call POST $N0/api/channels/${MCH:-0}/messages "{\"body\":\"ping @u${RUN}_B\"}" "$O_T" >/dev/null
  m=$(pick '",".join(sorted(str(x) for x in d["message"]["mentions"]))')
  if [ "$m" = "$B_ID" ]; then ok 4.6 "a workspace member who is not in the channel is mentioned"
  else bad 4.6 "'@user' for a workspace member not in the channel gave mentions [$m] (want [$B_ID])"; fi

  m=$(ment "hi @u${RUN}_B, and @u${RUN}_C."); want=$(sorted_ids "$B_ID" "$C_ID")
  if [ "$m" = "$want" ]; then ok 4.7 "punctuation right after a username ends the mention"
  else bad 4.7 "'hi @b, and @c.' gave mentions [$m] (want [$want])"; fi

  # section 5 needs a WebSocket client, so it runs in Python (helpers in ws_lib.py)
  run_py 11 5.0 <<'PY'
import os, sys; sys.path.insert(0, os.environ["CHECKS"])
from ws_lib import *

S = {}
s, b = call("POST", f"/api/workspaces/{SLUG}/channels", {"name": f"ev-{RUN}"}, O)
EV = (b or {}).get("channel", {}).get("id")

def r51():
    S["w0"], S["w1"] = WS(8000, O), WS(8001, O)
def r52():
    # events from before the subscription: subscribe must not send them again
    assert call("POST", f"/api/channels/{EV}/messages", {"body": "before subscribe"}, O)[0] == 201, "post message failed"
    assert call("PATCH", f"/api/channels/{EV}", {"topic": "before"}, O)[0] == 200, "topic change failed"
    subs, old = {}, []
    for k in ("w0", "w1"):
        S[k].send({"type": "subscribe", "channel_id": EV})
        for f in S[k].frames():
            if f.get("type") == "subscribed": subs[k] = f; break
            if isinstance(f.get("event_id"), int): old.append((f["event_id"], f.get("type")))
        old += [(f["event_id"], f.get("type")) for f in S[k].frames(0.5) if isinstance(f.get("event_id"), int)]
    f0, f1 = subs.get("w0"), subs.get("w1")
    assert f0 and f1, "no 'subscribed' frame within 3s"
    assert not old, f"subscribe sent old events {old}; only resume replays history, subscribe starts at the head"
    assert isinstance(f0.get("head_event_id"), int), f"head_event_id is {f0.get('head_event_id')!r}, want an int"
    assert f0["head_event_id"] == f1["head_event_id"], f"head on 8000 is {f0['head_event_id']}, on 8001 {f1['head_event_id']}"
    S["h"] = f0["head_event_id"]
def r53():
    h = S["h"]
    s, b = call("POST", f"/api/channels/{EV}/messages", {"body": "event one"}, O)
    assert s == 201, f"post message got {s}"
    S["m"] = b["message"]["id"]
    got = S["w0"].events(EV, 1)
    assert got == [(h + 1, "message.created")], f"8000 got {got}, want [({h + 1}, 'message.created')]"
def r54():
    got = S["w1"].events(EV, 1)
    assert got == [(S["h"] + 1, "message.created")], f"8001 got {got}, want [({S['h'] + 1}, 'message.created')]"
def r55():
    h = S["h"]
    steps = [(call("POST", f"/api/channels/{EV}/messages", {"body": "event reply", "parent_id": S["m"]}, O), "reply"),
             (call("PATCH", f"/api/channels/{EV}", {"topic": "events"}, O), "topic"),
             (call("POST", f"/api/channels/{EV}/join", {}, B), "join"),
             (call("POST", f"/api/channels/{EV}/leave", {}, B), "leave")]
    for (s, _), what in steps:
        assert s in (200, 201), f"{what} request got {s}"
    want = [(h + 2, "message.reply"), (h + 3, "channel.updated"), (h + 4, "member.joined"), (h + 5, "member.left")]
    got = S["w0"].events(EV, 4)
    assert got == want, f"got {got}, want {want}"
def r56():
    h = S["h"]
    w = WS(8002, O)
    w.send({"type": "resume", "channel_id": EV, "since_event_id": h})
    got, done = [], None
    for f in w.frames():
        if f.get("type") == "resumed": done = f; break
        if isinstance(f.get("event_id"), int): got.append((f["event_id"], f.get("type")))
    want = [(h + 1, "message.created"), (h + 2, "message.reply"), (h + 3, "channel.updated"),
            (h + 4, "member.joined"), (h + 5, "member.left")]
    assert got == want, f"replayed {got}, want {want}"
    assert done and done.get("head_event_id") == h + 5, f"'resumed' frame was {done}, want head_event_id {h + 5}"
def r57():
    h = S["h"]
    for act in ("archive", "unarchive"):
        s, _ = call("POST", f"/api/channels/{EV}/{act}", {}, O)
        assert s == 200, f"{act} got {s}"
    want = [(h + 6, "channel.archived"), (h + 7, "channel.unarchived")]
    got = S["w0"].events(EV, 2)
    assert got == want, f"got {got}, want {want}"

rule("5.1", "a valid token connects on 8000 and 8001", r51)
rule("5.2", "subscribe answers 'subscribed' with the same head_event_id on every node", r52)
rule("5.3", "posting a message sends message.created with event_id head+1", r53)
rule("5.4", "a subscriber on 8001 gets the event for a message posted on 8000", r54)
rule("5.5", "reply, topic, join, leave each add one event, with no gaps", r55)
rule("5.6", "resume on 8002 replays every event after since_event_id, then 'resumed'", r56)
rule("5.7", "archive and unarchive add channel.archived and channel.unarchived", r57)
def r58():
    from concurrent.futures import ThreadPoolExecutor
    s, b = call("POST", f"/api/workspaces/{SLUG}/channels", {"name": f"cc-{RUN}"}, O)
    assert s == 201, f"create channel got {s}"
    cc = b["channel"]["id"]
    w = WS(8000, O); w.send({"type": "subscribe", "channel_id": cc}); f = w.first(is_sub)
    assert f, "no 'subscribed' frame within 3s"
    h = f["head_event_id"]
    post = lambda i: call("POST", f"/api/channels/{cc}/messages", {"body": f"c{i}"}, O, port=8000 + i % 3)[0]
    with ThreadPoolExecutor(30) as ex:
        codes = list(ex.map(post, range(30)))
    assert codes.count(201) == 30, f"{codes.count(201)} of 30 concurrent posts returned 201 (statuses {sorted(set(codes))})"
    r = WS(8001, O); r.send({"type": "resume", "channel_id": cc, "since_event_id": h})
    ids = []
    for fr in r.frames():
        if fr.get("type") == "resumed": break
        if isinstance(fr.get("event_id"), int): ids.append(fr["event_id"])
    assert ids == list(range(h + 1, h + 31)), \
        f"after 30 concurrent posts the event ids were {ids[:6]}... ({len(ids)} ids, {len(set(ids))} distinct), want {h + 1} to {h + 30} in order"
    s, b = call("GET", f"/api/channels/{cc}/messages?limit=50", token=O)
    n = len((b or {}).get("messages") or [])
    assert n == 30, f"the channel lists {n} messages, want 30"
rule("5.8", "30 concurrent posts across the three nodes get 30 dense, unique event ids", r58)
def r59():
    w = WS(8002, O); w.send({"type": "subscribe", "channel_id": EV})
    assert w.first(is_sub), "no 'subscribed' frame within 3s"
    s, b = call("POST", f"/api/channels/{EV}/messages", {"body": "frame check"}, O, port=8001)
    assert s == 201, f"post message got {s}"
    f = w.first(lambda f: f.get("type") == "message.created")
    m = (f or {}).get("message")
    assert isinstance(m, dict) and m.get("body") == "frame check" and str(m.get("id")) == str(b["message"]["id"]), \
        f"the message.created frame should carry the message under \"message\"; got {str(f)[:160]}"
def r510():
    s, b = call("POST", f"/api/workspaces/{SLUG}/channels", {"name": f"pws-{RUN}", "is_private": True}, O)
    assert s == 201, f"create private channel got {s}"
    pc = b["channel"]["id"]
    w = WS(8001, B); w.send({"type": "subscribe", "channel_id": pc})
    call("POST", f"/api/channels/{pc}/messages", {"body": "secret"}, O, port=8000)
    f = w.first(lambda f: f.get("type") == "message.created" and str(f.get("channel_id")) == str(pc), 1.5)
    assert not f, "a user who is not in the private channel got its message.created frame"
def r511():
    w = WS(8002, O); w.send({"type": "subscribe", "channel_id": EV})
    assert w.first(is_sub), "no 'subscribed' frame within 3s"
    s, b = call("POST", f"/api/channels/{EV}/messages", {"body": "parent 511"}, O, port=8000)
    pid = b["message"]["id"]
    call("POST", f"/api/channels/{EV}/messages", {"body": "child 511", "parent_id": pid}, O, port=8001)
    f = w.first(lambda f: f.get("type") == "message.reply")
    m = (f or {}).get("message")
    assert isinstance(m, dict) and m.get("body") == "child 511" and str(m.get("parent_id")) == str(pid), \
        f"the message.reply frame should carry the reply (body, parent_id) under \"message\"; got {str(f)[:160]}"
rule("5.9", "message.created frames carry the message object under \"message\"", r59)
rule("5.10", "no message.created frames from a private channel you are not in", r510)
rule("5.11", "message.reply frames carry the reply under \"message\"", r511)
PY
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_channels_3; finish; }
