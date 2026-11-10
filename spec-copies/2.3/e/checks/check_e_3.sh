#!/usr/bin/env bash
# Self-check for E.3 (IRC channels and bridging). Exit 0 only if every rule passes.
# Usage: bash check_e_3.sh
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_e_3() {
  run_py 6 3.0 <<'PY'
from e_lib import *

W = world(3)
if not W.error:
    W.IRCNAME = f"#{W.SLUG}/{W.CH_NAME}"

def bodies(w):
    return {(f.get("message") or {}).get("body") for _, f in list(w.got)}
def web_line(c, body, timeout=3):
    return c.wait(lambda l: l.endswith(f" PRIVMSG {W.IRCNAME} :{body}"), timeout)
def web_post(port, body):
    return write(W, port, body)

def r1():
    # D is in the workspace but has not joined the channel on the web
    W.I = IRC().login(W.D, f"d{RUN}")
    W.I.send(f"JOIN {W.IRCNAME}")
    assert W.I.numeric("366"), f"JOIN: no 366 within 3s; got {W.I.log[-4:]}"
    lines = [l for l in W.I.log if num(l) in ("353", "366") or (" JOIN " in l and l.startswith(f":d{RUN}!"))]
    assert " JOIN " in lines[0] and num(lines[1]) == "353" and num(lines[-1]) == "366", f"JOIN lines in order are {lines}"
    assert f"d{RUN}" in lines[1], f"353 does not list the nick: {lines[1]}"
    s, b = call("GET", f"/api/channels/{W.CH}/members", None, W.O)
    assert W.D_NAME in str(b), "IRC JOIN did not make the user a channel member"
def r2():
    W.I.send(f"JOIN #none-{RUN}/nothing")
    assert W.I.numeric("403"), "an unknown channel got no 403 within 3s"
    n = W.I.numerics().count("403")
    W.I.send(f"JOIN #{W.SLUG}-{W.CH_NAME}")
    assert W.I.wait(lambda l: W.I.numerics().count("403") > n), "the hyphenated form got no 403 within 3s"
def r3():
    subs = [Sub(p, W.B, W.CH) for p in PORTS]
    body = f"from irc {RUN}"
    W.I.send(f"PRIVMSG {W.IRCNAME} :{body}")
    end = time.time() + 3
    for w in subs:
        while time.time() < end and body not in bodies(w): time.sleep(0.02)
        assert body in bodies(w), f"the subscriber on {w.port} got no frame within 3s"
    s, b = call("GET", f"/api/channels/{W.CH}/messages?limit=100", None, W.B)
    row = [m for m in (b or {}).get("messages", []) if m.get("body") == body]
    assert row, "the message is not listed"
    assert (row[0].get("author") or {}).get("username") == W.D_NAME, f"the author is {row[0].get('author')}, want {W.D_NAME}"
def r4():
    w = Sub(8001, W.B, W.CH)
    t = threading.Thread(target=lambda: [web_post(PORTS[i % 3], f"mix-w{i}") for i in range(6)])
    t.start()
    for i in range(6): W.I.send(f"PRIVMSG {W.IRCNAME} :mix-i{i}")
    t.join()
    end = time.time() + 4
    while time.time() < end and len(w.got) < 12: time.sleep(0.02)
    assert len(w.got) == 12, f"got {len(w.got)} frames for 12 messages"
    w.check_dense()
def r5():
    for p in PORTS:
        body = f"from web {p} {RUN}"
        web_post(p, body)
        line = web_line(W.I, body)
        assert line, f"a message posted on {p} did not reach IRC within 3s"
        assert re.fullmatch(rf":{re.escape(W.O_NAME)}!\S+@\S+ PRIVMSG {re.escape(W.IRCNAME)} :{re.escape(body)}", line), f"the line is {line!r}"
def r6():
    w = Sub(8001, W.B, W.CH)
    kill_port(8000)
    try:
        body = f"irc during kill {RUN}"
        W.I.send(f"PRIVMSG {W.IRCNAME} :{body}")
        end = time.time() + 4
        while time.time() < end and body not in bodies(w): time.sleep(0.02)
        assert body in bodies(w), "with a node killed, a PRIVMSG did not reach the web within 4s"
        body = f"web during kill {RUN}"
        web_post(8002, body)
        assert web_line(W.I, body, 4), "with a node killed, a web message did not reach IRC within 4s"
        assert not W.I.closed, "the IRC connection closed when a node was killed"
    finally:
        end = time.time() + 10   # let it come back, so the steps after this one have all three nodes
        while time.time() < end and call("GET", "/api/health", port=8000, timeout=1)[0] != 200: time.sleep(0.2)

run_rules(W, [
    ("3.1", "JOIN gives JOIN, 353 and 366, and joins the channel", r1),
    ("3.2", "unknown and hyphenated channels get 403", r2),
    ("3.3", "PRIVMSG creates a message that the web sees on all nodes", r3),
    ("3.4", "IRC and web messages share a dense event_id", r4),
    ("3.5", "a web message reaches IRC in the pinned format, from any node", r5),
    ("3.6", "bridging keeps working both ways after a node is killed", r6),
])
PY
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_e_3; finish; }
