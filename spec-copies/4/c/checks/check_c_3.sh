#!/usr/bin/env bash
# Self-check for C.3 (slash commands). Exit 0 only if every rule passes.
# Usage: bash check_c_3.sh
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_c_3() {
  run_py 10 3.0 <<'PY'
from c_lib import *

W = world(3)
SHRUG = "¯\\_(ツ)_/¯"

def body_is(text, wanted, what):
    s, b = post(W, text)
    want(s, 201, what)
    got = (b or {}).get("message", {}).get("body")
    assert got == wanted, f"{what}: body is {got!r}, want {wanted!r}"
def side_effect(s, b, what):
    want(s, 201, what)
    assert isinstance(b, dict) and "message" in b and b["message"] is None, f"{what}: want message: null, got {str(b)[:120]}"

def r1(): body_is("/me waves", "/me waves", "/me with text")
def r2(): want(post(W, "/me")[0], 400, "/me without text")
def r3(): body_is("/shrug ok", f"ok {SHRUG}", "/shrug with text")
def r4(): body_is("/shrug", SHRUG, "/shrug alone")
def r5(): body_is("//literal", "//literal", "//literal")
def r6(): want(post(W, "/nope")[0], 400, "unknown command /nope")
def r7():
    s, b = post(W, "/topic new words")
    side_effect(s, b, "/topic")
    assert (b.get("channel") or {}).get("topic") == "new words", f"channel in the response is {b.get('channel')}, want topic 'new words'"
def r8():
    ch = channel(W, f"in3-{RUN}")
    s, b = post(W, f"/invite @{W.B_NAME}", channel_id=ch)
    side_effect(s, b, "/invite")
    u = b.get("invited_user") or {}
    assert same(u.get("id"), W.B_ID) and "display_name" in u, f"invited_user is {u}, want the UserObj"
    ids = idset(m.get("id") for m in call("GET", f"/api/channels/{ch}/members", None, W.O)[1]["members"])
    assert str(W.B_ID) in ids, f"the invited user is not in the members list {ids}"
def r9():
    W.A = channel(W, f"ar3-{RUN}")
    s, b = post(W, "/archive", W.O, W.A)
    side_effect(s, b, "/archive")
    assert (b.get("channel") or {}).get("is_archived") is True, f"channel in the response is {b.get('channel')}, want is_archived true"
    want(post(W, "hello", W.O, W.A)[0], 423, "posting after /archive")
def r10():
    s, b = post(W, "/unarchive", W.O, W.A)
    side_effect(s, b, "/unarchive")
    assert (b.get("channel") or {}).get("is_archived") is False, f"channel in the response is {b.get('channel')}, want is_archived false"
    want(post(W, "back", W.O, W.A)[0], 201, "posting after /unarchive")

run_rules(W, [
    ("3.1", "/me keeps the body literally", r1),
    ("3.2", "/me without text is 400", r2),
    ("3.3", "/shrug text appends the emoticon after one space", r3),
    ("3.4", "/shrug alone is just the emoticon", r4),
    ("3.5", "//literal is posted literally", r5),
    ("3.6", "unknown commands are 400", r6),
    ("3.7", "/topic returns 201, message null and channel", r7),
    ("3.8", "/invite returns 201, message null and invited_user, and adds the member", r8),
    ("3.9", "/archive returns 201, message null and channel, then posting is 423", r9),
    ("3.10", "/unarchive works on an archived channel and restores posting", r10),
])
PY
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_c_3; finish; }
