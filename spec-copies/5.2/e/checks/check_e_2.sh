#!/usr/bin/env bash
# Self-check for E.2 (IRC connection). Exit 0 only if every rule passes.
# Usage: bash check_e_2.sh
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_e_2() {
  run_py 7 2.0 <<'PY'
from e_lib import *

W = world(2)

def r1():
    W.A = IRC().login(W.B, f"a{RUN}")
    got = W.A.numerics()
    assert got == ["001", "002", "003", "004", "005", "422"], f"numerics in order are {got}"
def r2():
    c = IRC()
    c.send("PASS not-a-real-token"); c.send(f"NICK bad{RUN}"); c.send(f"USER bad{RUN} 0 * :x")
    assert c.numeric("464"), f"bad password: no 464 within 3s; got {c.log[:5]}"
def r3():
    c = IRC()
    c.send(f"PASS {W.C}"); c.send(f"NICK a{RUN}"); c.send(f"USER c{RUN} 0 * :c")
    assert c.numeric("433"), f"nick in use: no 433 within 3s; got {c.log[:5]}"
    assert not c.closed, "433 closed the connection"
    c.send(f"NICK c{RUN}")
    assert c.numeric("422"), f"a second NICK did not complete registration; got {c.numerics()}"
def r4():
    W.A.send("FOOBAR x")
    assert W.A.numeric("421"), "an unknown command got no 421 within 3s"
def r5():
    W.A.send(f"PING tok{RUN}")
    assert W.A.wait(lambda l: "PONG" in l and f"tok{RUN}" in l), "PING got no PONG with its text within 3s"
def r6():
    c = IRC().login(W.O, f"o{RUN}")
    ch = f"#{W.SLUG}/{W.CH_NAME}"
    for cmd in ("PONG x", f"NAMES {ch}", "WHO", "LIST", f"TOPIC {ch}", f"MODE {ch}"):
        c.send(cmd)
        c.send(f"PING after{RUN}{cmd.split()[0]}")
        assert c.wait(lambda l: "PONG" in l and f"after{RUN}{cmd.split()[0]}" in l), f"no answer after {cmd}"
        assert "421" not in c.numerics(), f"{cmd} got 421"
def r7():
    c = IRC().login(W.D, f"d{RUN}")
    before = len(c.log)
    c.send("QUIT :bye")
    end = time.time() + 3
    while time.time() < end and not c.closed: time.sleep(0.02)
    assert c.closed, "QUIT did not close the connection within 3s"
    after = c.log[before:]
    assert not [l for l in after if l.startswith("ERROR") or (num(l) or "0")[0] in "45"], f"QUIT got an error reply: {after}"

run_rules(W, [
    ("2.1", "registration gives 001 to 005, then 422", r1),
    ("2.2", "a bad PASS gets 464", r2),
    ("2.3", "a nick in use gets 433 and the connection stays open", r3),
    ("2.4", "an unknown command gets 421", r4),
    ("2.5", "PING gets PONG", r5),
    ("2.6", "PONG, NAMES, WHO, LIST, TOPIC and MODE do not get 421", r6),
    ("2.7", "QUIT closes the connection with no error", r7),
])
PY
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_e_2; finish; }
