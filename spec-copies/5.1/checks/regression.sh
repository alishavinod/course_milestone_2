#!/usr/bin/env bash
# Regression suite: every rule of Roles (Spec A) and Channels (Spec B) in one script.
# Prints only what broke, then a total. Usage: bash regression.sh      Exit 0 only if nothing broke.
set -u

FEEDBACK=verbose

TMP=$(mktemp -d /tmp/regression.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
CHECKS="$TMP"
BASE="$(date +%s | tail -c 6)$RANDOM"
SECONDS=0

cat > "$TMP/ws_lib.py" <<'WS_LIB_EOF'
"""Helpers for the WebSocket parts of the staged channels checks. Imported by check_channels_3.sh and _4.sh."""
import base64, json, os, socket, struct, time, urllib.request, urllib.error

E = os.environ
RUN, SLUG, O, B = E["RUN"], E["WS"], E["O_T"], E["B_T"]


def call(method, path, body=None, token=None, port=8000):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    if data is not None: req.add_header("Content-Type", "application/json")
    if token: req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r: return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e: return e.code, None
    except Exception: return 0, None


class WS:
    """Tiny WebSocket client: connect, send JSON, receive JSON frames."""
    def __init__(self, port, token):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /api/ws?token={token} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk: raise AssertionError(f"port {port}: connection closed during the handshake")
            buf += chunk
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        status = head.split(b" ")[1].decode()
        assert status == "101", f"port {port}: handshake answered {status}, want 101"

    def send(self, obj):
        data, mask = json.dumps(obj).encode(), os.urandom(4)
        n = len(data)
        hdr = bytes([0x81, 0x80 | n]) if n < 126 else bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk: raise AssertionError("server closed the WebSocket")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def frames(self, timeout=3):
        """Yields JSON frames until `timeout` seconds pass with nothing new."""
        end = time.time() + timeout
        while time.time() < end:
            self.sock.settimeout(max(0.05, end - time.time()))
            try:
                b1, b2 = self._read(2)
            except socket.timeout:
                return
            n = b2 & 0x7F
            if n == 126: n = struct.unpack(">H", self._read(2))[0]
            elif n == 127: n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if b1 & 0x0F == 1: yield json.loads(data)
            elif b1 & 0x0F == 8: raise AssertionError("server closed the WebSocket")

    def first(self, pred, timeout=3):
        return next((f for f in self.frames(timeout) if pred(f)), None)

    def events(self, cid, count, timeout=3):
        got = []
        for f in self.frames(timeout):
            if str(f.get("channel_id")) == str(cid) and isinstance(f.get("event_id"), int):
                got.append((f["event_id"], f.get("type")))
                if len(got) == count: break
        return got


def is_sub(frame):
    return frame.get("type") == "subscribed"


def rule(rid, desc, fn):
    try:
        fn(); print(f"PASS {rid} {desc}")
    except AssertionError as e:
        print(f"FAIL {rid} {desc}: {e}")
    except KeyError:
        print(f"FAIL {rid} {desc}: skipped, an earlier rule in this section failed")
    except Exception as e:
        print(f"FAIL {rid} {desc}: {type(e).__name__}: {e}")
WS_LIB_EOF

# ===== roles =====
(
  RUN="${BASE}1"

N0=http://127.0.0.1:8000; N1=http://127.0.0.1:8001; N2=http://127.0.0.1:8002
BODY=/tmp/check_roles_body.json
RUN="${RUN:-$(date +%s | tail -c 6)$RANDOM}"   # kept when check_roles_all.sh sources several checks
PW="correct-horse-1"
pass=0; fail=0

ok()  { [ "$FEEDBACK" = verbose ] && echo "PASS $1 $2"; pass=$((pass+1)); }
bad() { [ "$FEEDBACK" = verbose ] && echo "FAIL $1 $2"; fail=$((fail+1)); }
note() { [ "$FEEDBACK" = verbose ] && echo "NOTE $1"; return 0; }
# expect <rule> <description> <expected-status> <actual-status>
expect() { if [ "$4" = "$3" ]; then ok "$1" "$2"; else bad "$1" "$2: expected $3, got $4"; fi; }

# call <METHOD> <URL> [json-body] [token]  -> prints the HTTP status, saves the body in $BODY
call() {
  local args=(-s -o "$BODY" -w '%{http_code}' --max-time 5 -X "$1" "$2")
  [ -n "${3:-}" ] && args+=(-H 'Content-Type: application/json' -d "$3")
  [ -n "${4:-}" ] && args+=(-H "Authorization: Bearer $4")
  curl "${args[@]}" 2>/dev/null || true   # status 000 means no answer within 5s
}
# field <dotted.path>  -> prints that field of the last response body, or nothing
field() {
  python3 -c 'import json,sys
try:
    v=json.load(open(sys.argv[1]))
    for k in sys.argv[2].split("."): v=v[k]
    print(v)
except Exception: pass' "$BODY" "$1"
}
# list_has <list-key> <item-field> <value>  -> prints "yes" if that list in the last body has a matching item
list_has() {
  python3 -c 'import json,sys
try:
    if any(str(i.get(sys.argv[3]))==sys.argv[4] for i in json.load(open(sys.argv[1]))[sys.argv[2]]): print("yes")
except Exception: pass' "$BODY" "$1" "$2" "$3"
}
# role_of <slug> <token> <user_id>  -> prints that user's role in the workspace
role_of() {
  call GET "$N0/api/workspaces/$1/members" "" "$2" >/dev/null
  python3 -c 'import json,sys
try:
    for m in json.load(open(sys.argv[1]))["members"]:
        if str(m.get("user_id"))==sys.argv[2]: print(m.get("role"))
except Exception: pass' "$BODY" "$3"
}
# new_user <NAME>  -> registers a user, sets NAME_T (token) and NAME_ID
new_user() {
  call POST $N0/api/auth/register "{\"username\":\"u${RUN}_$1\",\"password\":\"$PW\"}" >/dev/null
  printf -v "$1_T" '%s' "$(field token)"; printf -v "$1_ID" '%s' "$(field user.id)"
}
# need_users <NAME>...  -> registers each test user this run does not have yet
need_users() { local u t; for u in "$@"; do t="${u}_T"; [ -n "${!t:-}" ] || new_user "$u"; done; }
# need_ws1  -> workspace WS1 owned by O, with A, M, G and Z as members (made by Roles.2, or here if a check runs alone)
need_ws1() {
  local u t
  need_users O A M G Z X Y
  [ -n "${WS1:-}" ] && return
  WS1="ws-$RUN-a"
  call POST $N0/api/workspaces "{\"slug\":\"$WS1\",\"name\":\"Check A\"}" "$O_T" >/dev/null; GEN1=$(field general_channel.id)
  for u in A M G Z; do t="${u}_T"; call POST $N0/api/channels/${GEN1:-0}/join "{}" "${!t}" >/dev/null; done
}
# jv <id>  -> the id as a JSON value (number stays a number, anything else is quoted)
jv() { if [[ "$1" =~ ^[0-9]+$ ]]; then echo "$1"; else echo "\"$1\""; fi; }
# py <python expression on d>  -> evaluates it on the last response body d, or prints nothing
py() {
  python3 -c 'import json,sys
try:
    d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))
except Exception: pass' "$BODY" "$1"
}
healthy() {
  for u in $N0 $N1 $N2; do curl -fs --max-time 2 "$u/api/health" >/dev/null 2>&1 || return 1; done
}
# boot  -> starts the cluster if it is not answering. Waits at most 15s so a check fits the 30s limit.
boot() {
  if ! healthy; then
    # only start.sh goes to the background (with its own output), so the check still exits cleanly
    (cd /app; setsid nohup /app/start.sh </dev/null >/tmp/start.log 2>&1 &)
    for _ in $(seq 1 15); do healthy && break; sleep 1; done
  fi
  healthy || { bad 0 "cluster is not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)"; finish; exit 1; }
}
finish() { echo "RESULT: $pass passed, $fail failed"; [ "$fail" -eq 0 ]; }
# Passing rules print nothing; each failure names its component.
ok()   { pass=$((pass+1)); }
bad()  { [ "$FEEDBACK" = verbose ] && echo "REGRESSION FAIL roles $1 $2"; fail=$((fail+1)); }
note() { [ "$FEEDBACK" = verbose ] && echo "REGRESSION NOTE roles $1"; return 0; }
finish() { echo "$fail" > "$TMP/roles"; }
section() {
  if [ "$SECONDS" -ge 24 ]; then
    bad time "$1 did not run: the suite ran out of time after ${SECONDS}s. The app is too slow; fix that and run it again"
  else "$1"; fi
}


rules_roles_1() {
  local s a b c t i dn f g bad_nodes="" bad_ws=""
  for i in 0 1 2; do
    s=$(call GET "http://127.0.0.1:800$i/api/health")
    [ "$s" = 200 ] && [ "$(field status)" = ok ] && [ "$(field node_id)" = "$i" ] || bad_nodes="$bad_nodes 800$i"
  done
  if [ -z "$bad_nodes" ]; then ok 0.1 "health returns 200 with the right node_id on all nodes"
  else bad 0.1 "health: wrong status or node_id on port(s)$bad_nodes"; fi

  bad_ws=""
  for i in 0 1 2; do
    s=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
      -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' "http://127.0.0.1:800$i/api/ws?token=bogus$RUN" 2>/dev/null)
    [ "$s" = 101 ] && bad_ws="$bad_ws 800$i"
  done
  if [ -z "$bad_ws" ]; then ok 0.2 "websocket with a made-up token is rejected on every node"
  else bad 0.2 "websocket with a made-up token was accepted (101) on port(s)$bad_ws"; fi

  s=$(call POST $N0/api/auth/register "{\"username\":\"u${RUN}_O\",\"password\":\"$PW\"}")
  O_T=$(field token); O_ID=$(field user.id)
  if [ "$s" = 201 ] && [ "${#O_T}" -ge 16 ]; then ok 1.1 "register returns 201 with user and token"
  else bad 1.1 "register: expected 201 and a token of 16+ chars, got status $s, token length ${#O_T}"; fi

  s=$(call POST $N0/api/auth/register "{\"username\":\"u${RUN}_O\",\"password\":\"$PW\"}")
  expect 1.2 "duplicate username" 409 "$s"

  a=$(call POST $N0/api/auth/register "{\"username\":\"bad name $RUN\",\"password\":\"$PW\"}")
  b=$(call POST $N0/api/auth/register "{\"username\":\"u${RUN}_short\",\"password\":\"short77\"}")
  if [ "$a" = 400 ] && [ "$b" = 400 ]; then ok 1.3 "bad username and short password return 400"
  else bad 1.3 "expected 400 for a username with a space and a 7 char password, got $a and $b"; fi

  a=$(call POST $N0/api/auth/login "{\"username\":\"u${RUN}_O\",\"password\":\"$PW\"}"); t=$(field token)
  b=$(call POST $N0/api/auth/login "{\"username\":\"u${RUN}_O\",\"password\":\"WRONG12345\"}")
  if [ "$a" = 200 ] && [ -n "$t" ] && [ "$b" = 401 ]; then ok 1.4 "login 200, wrong password 401"
  else bad 1.4 "expected login 200 with a token and wrong password 401, got $a and $b"; fi

  a=$(call GET $N0/api/auth/me "" "$O_T"); b=$(call GET $N0/api/auth/me); c=$(call GET $N0/api/auth/me "" "fake-token-$RUN")
  if [ "$a" = 200 ] && [ "$b" = 401 ] && [ "$c" = 401 ]; then ok 1.5 "me 200, no token 401, made-up token 401"
  else bad 1.5 "expected me 200, no token 401, made-up token 401, got $a, $b, $c"; fi

  a=$(call GET $N1/api/auth/me "" "$O_T"); b=$(call GET $N2/api/auth/me "" "$O_T")
  if [ "$a" = 200 ] && [ "$b" = 200 ]; then ok 1.6 "token works on 8001 and 8002"
  else bad 1.6 "token from 8000: me on 8001 got $a, on 8002 got $b (expected 200)"; fi

  call POST $N0/api/auth/register "{\"username\":\"u${RUN}_dn\",\"password\":\"$PW\",\"display_name\":\"Dee Enn\"}" >/dev/null
  dn=$(field user.display_name); f=$(py 'all(k in d["user"] for k in ["id","username","display_name","timezone","avatar_url","status_text","status_emoji"])')
  call GET $N1/api/auth/me "" "$O_T" >/dev/null; g=$(py 'all(k in d["user"] for k in ["id","username","display_name","timezone","avatar_url","status_text","status_emoji"])')
  a=$(call POST $N0/api/auth/login "{\"username\":\"nobody$RUN\",\"password\":\"$PW\"}")
  if [ "$dn" = "Dee Enn" ] && [ "$f$g" = TrueTrue ] && [ "$a" = 401 ]; then ok 1.7 "display_name is kept, users have every UserObj field, unknown username login 401"
  else bad 1.7 "display_name '$dn' (want Dee Enn), every UserObj field on register: '$f', on me: '$g' (want True), login of an unknown username: $a (want 401)"; fi
}

# rule_0_3  -> rule 0.3. It kills node 8001, so it runs first (also in check_roles_all.sh)
rule_0_3() {
  local pid a c back
  pid=$(ss -ltnpH 2>/dev/null | grep -E ':8001 ' | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
  if [ -z "$pid" ]; then bad 0.3 "could not find the process listening on 8001 with ss -ltnp"
  else
    kill -9 "$pid"; sleep 1
    a=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 $N0/api/health); c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 $N2/api/health)
    back=no; for _ in $(seq 1 16); do curl -fs --max-time 1 $N1/api/health >/dev/null 2>&1 && { back=yes; break; }; sleep 0.5; done
    if [ "$a$c" = 200200 ] && [ "$back" = yes ]; then ok 0.3 "after kill -9 of node 8001, 8000 and 8002 keep serving and 8001 comes back"
    else bad 0.3 "after kill -9 of node 8001: 8000 got $a, 8002 got $c (want 200 each), 8001 back within 8s: $back (start.sh must start a dead node again)"; fi
  fi
}



rules_roles_2() {
  local s a b r u t f
  # setup from Roles.1: registered users O (owner), A, M, G, Z, X, Y
  need_users O A M G Z X Y

  WS1="ws-$RUN-a"
  s=$(call POST $N0/api/workspaces "{\"slug\":\"$WS1\",\"name\":\"Check A\"}" "$O_T")
  GEN1=$(field general_channel.id)
  if [ "$s" = 201 ] && [ "$(field workspace.owner_id)" = "$O_ID" ] && [ "$(field workspace.join_mode)" = open ] \
     && [ "$(field general_channel.name)" = general ] && [ -n "$(field workspace.id)" ]; then
    ok 2.1 "create workspace returns 201 with workspace and #general"
  else bad 2.1 "create workspace: expected 201, owner_id=creator, join_mode=open, general_channel.name=general; got status $s"; fi

  s=$(call POST $N0/api/workspaces "{\"slug\":\"$WS1\",\"name\":\"Again\"}" "$O_T")
  expect 2.2 "duplicate slug" 409 "$s"

  s=$(call POST $N0/api/workspaces "{\"slug\":\"Bad_Slug\",\"name\":\"Bad\"}" "$O_T")
  expect 2.3 "invalid slug" 400 "$s"

  call GET $N0/api/workspaces "" "$O_T" >/dev/null; a=$(list_has workspaces slug "$WS1")
  call GET $N0/api/workspaces "" "$X_T" >/dev/null; b=$(list_has workspaces slug "$WS1")
  if [ "$a" = yes ] && [ -z "$b" ]; then ok 2.4 "workspace list shows only the caller's workspaces"
  else bad 2.4 "owner sees it: '${a:-no}', non-member sees it: '${b:-no}' (expected yes and no)"; fi

  r=$(role_of "$WS1" "$O_T" "$O_ID")
  if [ "$r" = owner ]; then ok 2.5 "members list shows the creator as owner"
  else bad 2.5 "members: creator's role is '$r', expected owner"; fi

  call GET $N0/api/workspaces "" "$O_T" >/dev/null
  f=$(py '[sorted(k for k in ["id","slug","name","owner_id","join_mode"] if k in w) for w in d["workspaces"] if w.get("slug")=="'"$WS1"'"]==[["id","join_mode","name","owner_id","slug"]]')
  a=$(call GET $N0/api/workspaces/nope-$RUN "" "$O_T"); b=$(call POST $N0/api/workspaces '{"slug":"a","name":"x"}' "$O_T")
  if [ "$f" = True ] && [ "$a" = 404 ] && [ "$b" = 400 ]; then ok 2.6 "listed workspaces have every field, unknown slug 404, 1-char slug 400"
  else bad 2.6 "listed workspace has id, slug, name, owner_id, join_mode: '$f' (want True), GET unknown slug: $a (want 404), slug 'a': $b (want 400)"; fi

  s=$(call POST $N1/api/channels/${GEN1:-0}/join "{}" "$A_T"); r=$(role_of "$WS1" "$O_T" "$A_ID")
  if [ "$s" = 200 ] && [ "$r" = member ]; then ok 3.1 "non-member joins #general of an open workspace and becomes member"
  else bad 3.1 "join on open workspace: expected 200 and role member, got $s and '$r'"; fi
  for u in M G Z; do t="${u}_T"; call POST $N0/api/channels/${GEN1:-0}/join "{}" "${!t}" >/dev/null; done

  s=$(call POST $N0/api/channels/999999999/join "{}" "$A_T")
  expect 3.2 "join unknown channel" 404 "$s"

  call POST $N0/api/workspaces/$WS1/channels "{\"name\":\"priv-$RUN\",\"is_private\":true}" "$O_T" >/dev/null
  PRIV=$(field channel.id)
  s=$(call POST $N0/api/channels/${PRIV:-0}/join "{}" "$M_T")
  expect 3.3 "join a private channel you are not in" 403 "$s"
}



rules_roles_3() {
  local s a b c mr r PUB
  # setup from Roles.2: workspace WS1 owned by O, with A, M, G, Z as members
  need_ws1

  s=$(call PATCH $N0/api/workspaces/$WS1/members/$A_ID '{"role":"admin"}' "$O_T")
  mr=$(field member.role); r=$(role_of "$WS1" "$O_T" "$A_ID")
  if [ "$s" = 200 ] && [ "$mr" = admin ] && [ "$r" = admin ]; then ok 4.1 "owner promotes a member to admin"; A_ADMIN=1
  else bad 4.1 "promote: expected 200, member.role=admin and admin in the members list; got $s, '$mr', '$r'"; fi

  call PATCH $N0/api/workspaces/$WS1/members/$Z_ID '{"role":"admin"}' "$O_T" >/dev/null
  a=$(call PATCH $N0/api/workspaces/$WS1/members/$G_ID '{"role":"guest"}' "$A_T")
  b=$(call PATCH $N0/api/workspaces/$WS1/members/$Z_ID '{"role":"member"}' "$A_T")
  if [ "$a" = 200 ] && [ "$b" = 403 ]; then ok 4.2 "admin can change a member, not another admin"
  else bad 4.2 "admin sets member to guest: $a (want 200), admin changes another admin: $b (want 403)"; fi

  a=$(call PATCH $N0/api/workspaces/$WS1/members/$O_ID '{"role":"member"}' "$A_T")
  b=$(call PATCH $N0/api/workspaces/$WS1/members/$O_ID '{"role":"admin"}' "$O_T")
  if [ "$a" = 403 ] && [ "$b" = 403 ]; then ok 4.3 "nobody can change the owner's role with PATCH"
  else bad 4.3 "admin changes owner: $a, owner changes self: $b (expected 403 each)"; fi

  s=$(call PATCH $N0/api/workspaces/$WS1/members/$M_ID '{"role":"owner"}' "$O_T")
  expect 4.4 "PATCH role to owner" 400 "$s"

  s=$(call PATCH $N0/api/workspaces/$WS1/members/$G_ID '{"role":"member"}' "$M_T")
  expect 4.5 "member changes someone's role" 403 "$s"

  need_users NM
  a=$(call PATCH $N0/api/workspaces/$WS1/members/$G_ID '{"role":"superuser"}' "$O_T")
  b=$(call PATCH $N0/api/workspaces/$WS1/members/$NM_ID '{"role":"member"}' "$O_T")
  c=$(call PATCH $N0/api/workspaces/$WS1/members/999999999 '{"role":"member"}' "$O_T")
  if [ "$a$b$c" = 400404404 ]; then ok 4.6 "unknown role 400, a user who is not in the workspace 404"
  else bad 4.6 "role 'superuser': $a (want 400), role of a registered user not in the workspace: $b, of user 999999999: $c (want 404 each)"; fi

  a=$(call PATCH $N0/api/workspaces/$WS1/members/$M_ID '{"role":"admin"}' "$A_T"); r=$(role_of "$WS1" "$O_T" "$M_ID")
  call PATCH $N0/api/workspaces/$WS1/members/$M_ID '{"role":"member"}' "$O_T" >/dev/null
  if [ "$a" = 200 ] && [ "$r" = admin ]; then ok 4.7 "admin promotes a member to admin"
  else bad 4.7 "admin promotes a member: $a with role '$r' (want 200, admin)"; fi

  call POST $N0/api/workspaces/$WS1/channels "{\"name\":\"pub-$RUN\"}" "$O_T" >/dev/null; PUB=$(field channel.id)
  a=$(call POST $N0/api/workspaces/$WS1/channels "{\"name\":\"g-$RUN\"}" "$G_T")
  b=$(call POST $N0/api/channels/${PUB:-0}/join "{}" "$G_T")
  if [ "$a" = 403 ] && [ "$b" = 403 ]; then ok 5.1 "guest cannot create or join channels"
  else bad 5.1 "guest create: $a, guest join public: $b (expected 403 each)"; fi

  a=$(call POST $N0/api/workspaces/$WS1/channels "{\"name\":\"m-$RUN\"}" "$M_T")
  b=$(call POST $N0/api/workspaces/$WS1/channels "{\"name\":\"mp-$RUN\",\"is_private\":true}" "$M_T")
  if [ "$a" = 201 ] && [ "$b" = 403 ]; then ok 5.2 "member can create public, not private"
  else bad 5.2 "member public: $a (want 201), member private: $b (want 403)"; fi

  s=$(call POST $N0/api/workspaces/$WS1/channels "{\"name\":\"ap-$RUN\",\"is_private\":true}" "$A_T")
  expect 5.3 "admin creates a private channel" 201 "$s"
}



rules_roles_4() {
  local s a b c d r h jm ra ro n1 n2 o1 o2 C CODE1 LIVE WS2 GEN2
  # setup from Roles.2 and Roles.3: workspace WS1 owned by O, A is an admin, M is a member
  need_ws1; need_users W V P Q E F
  [ "${A_ADMIN:-}" = 1 ] || call PATCH $N0/api/workspaces/$WS1/members/$A_ID '{"role":"admin"}' "$O_T" >/dev/null

  WS2="ws-$RUN-b"
  call POST $N0/api/workspaces "{\"slug\":\"$WS2\",\"name\":\"Check B\"}" "$O_T" >/dev/null; GEN2=$(field general_channel.id)
  a=$(call PATCH $N0/api/workspaces/$WS2 '{"join_mode":"invite_only"}' "$O_T"); jm=$(field workspace.join_mode)
  b=$(call PATCH $N0/api/workspaces/$WS1 '{"name":"By Member"}' "$M_T")
  if [ "$a" = 200 ] && [ "$jm" = invite_only ] && [ "$b" = 403 ]; then ok 6.1 "owner sets invite_only, member cannot PATCH"
  else bad 6.1 "owner PATCH: $a with join_mode '$jm' (want 200, invite_only), member PATCH: $b (want 403)"; fi

  s=$(call POST $N0/api/channels/${GEN2:-0}/join "{}" "$X_T")
  expect 6.2 "non-member joins a channel in an invite_only workspace" 403 "$s"

  s=$(call PATCH $N0/api/workspaces/$WS2 '{"join_mode":"sometimes"}' "$O_T")
  expect 6.3 "invalid join_mode" 400 "$s"

  a=$(call PATCH $N0/api/workspaces/$WS1 '{"name":"By Owner"}' "$O_T"); n1=$(field workspace.name)
  b=$(call PATCH $N1/api/workspaces/$WS1 '{"name":"By Admin"}' "$A_T"); n2=$(field workspace.name)
  if [ "$a$b" = 200200 ] && [ "$n1|$n2" = "By Owner|By Admin" ]; then ok 6.4 "owner and admin rename the workspace"
  else bad 6.4 "owner rename: $a with name '$n1', admin rename: $b with name '$n2' (want 200 and the new name each)"; fi

  a=$(call POST $N0/api/workspaces/$WS2/invitations "{}" "$O_T"); CODE1=$(field invitation.code)
  b=$(call POST $N0/api/workspaces/$WS1/invitations "{}" "$M_T")
  if [ "$a" = 201 ] && [ -n "$CODE1" ] && [ "$b" = 403 ]; then ok 7.1 "owner creates an invitation with a code, member cannot"
  else bad 7.1 "owner create: $a with code '${CODE1}' (want 201), member create: $b (want 403)"; fi

  a=$(call POST $N0/api/invitations/${CODE1:-none}/accept "{}" "$X_T"); r=$(role_of "$WS2" "$O_T" "$X_ID")
  b=$(call POST $N0/api/channels/${GEN2:-0}/join "{}" "$X_T")
  if [ "$a" = 200 ] && [ "$r" = member ] && [ "$b" = 200 ]; then ok 7.2 "accept makes the user a member who can join channels"
  else bad 7.2 "accept: $a (want 200), role '$r' (want member), join after accept: $b (want 200)"; fi

  a=$(call POST $N0/api/invitations/${CODE1:-none}/accept "{}" "$Y_T")
  b=$(call POST $N0/api/invitations/nosuchcode$RUN/accept "{}" "$Y_T")
  if [ "$a" = 404 ] && [ "$b" = 404 ]; then ok 7.3 "used-up and unknown codes return 404"
  else bad 7.3 "second accept of a 1-use code: $a, unknown code: $b (expected 404 each)"; fi

  call POST $N0/api/workspaces/$WS2/invitations "{\"invited_user_id\":$(jv "$Z_ID")}" "$O_T" >/dev/null; C=$(field invitation.code)
  a=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$Y_T"); b=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$Z_T")
  if [ "$a" = 403 ] && [ "$b" = 200 ]; then ok 7.4 "targeted invitation: other user 403, invited user 200"
  else bad 7.4 "targeted invitation: other user got $a (want 403), invited user got $b (want 200)"; fi

  call POST $N0/api/workspaces/$WS2/invitations '{"expires_in_seconds":1}' "$O_T" >/dev/null; C=$(field invitation.code)
  sleep 2
  s=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$Y_T")
  expect 7.5 "expired invitation" 404 "$s"

  call POST $N0/api/workspaces/$WS2/invitations "{}" "$O_T" >/dev/null; LIVE=$(field invitation.code)
  a=$(call GET $N0/api/workspaces/$WS2/invitations "" "$O_T"); h=$(list_has invitations code "$LIVE")
  b=$(call GET $N0/api/workspaces/$WS2/invitations "" "$X_T")
  if [ "$a" = 200 ] && [ "$h" = yes ] && [ "$b" = 403 ]; then ok 7.6 "owner lists invitations, member cannot"
  else bad 7.6 "owner list: $a, includes new code: '${h:-no}' (want 200, yes), member list: $b (want 403)"; fi

  a=$(call DELETE $N0/api/workspaces/$WS2/invitations/${LIVE:-none} "" "$O_T"); d=$(field deleted)
  b=$(call POST $N0/api/invitations/${LIVE:-none}/accept "{}" "$Y_T")
  if [ "$a" = 200 ] && [ "$d" = True ] && [ "$b" = 404 ]; then ok 7.7 "revoke returns deleted:true and the code stops working"
  else bad 7.7 "revoke: $a with deleted='$d' (want 200, true), accept after revoke: $b (want 404)"; fi

  call POST $N0/api/workspaces/$WS2/invitations '{"max_uses":2}' "$O_T" >/dev/null; C=$(field invitation.code)
  a=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$W_T"); b=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$V_T")
  c=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$P_T")
  if [ "$a$b$c" = 200200404 ]; then ok 7.8 "max_uses=2 lets two users in, not a third"
  else bad 7.8 "max_uses=2: first $a, second $b (want 200 each), third $c (want 404)"; fi

  call POST $N0/api/workspaces/$WS2/invitations "{\"invited_username\":\"u${RUN}_Q\"}" "$O_T" >/dev/null; C=$(field invitation.code)
  a=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$Y_T"); b=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$Q_T")
  if [ "$a" = 403 ] && [ "$b" = 200 ]; then ok 7.9 "invited_username: other user 403, invited user 200"
  else bad 7.9 "invited_username invitation: other user got $a (want 403), invited user got $b (want 200)"; fi

  s=$(call POST $N0/api/workspaces/$WS1/invitations "{}" "$A_T")
  expect 7.10 "admin creates an invitation" 201 "$s"

  call POST $N0/api/workspaces/$WS2/invitations "{}" "$O_T" >/dev/null; C=$(field invitation.code)
  call POST $N0/api/invitations/${C:-none}/accept "{}" "$E_T" >/dev/null
  s=$(call GET $N0/api/workspaces/$WS2/invitations "" "$O_T"); h=$(list_has invitations code "$C")
  if [ "$s" = 200 ] && [ -n "$C" ] && [ -z "$h" ]; then ok 7.11 "used-up invitations are not listed"
  else bad 7.11 "list: $s; a code whose only use is taken is still listed: '${h:-no}' (want 200 and no)"; fi

  call POST $N0/api/workspaces/$WS2/invitations "{\"invited_user_id\":$(jv "$F_ID"),\"max_uses\":5}" "$O_T" >/dev/null; C=$(field invitation.code)
  a=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$F_T"); b=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$F_T")
  if [ "$a" = 200 ] && [ "$b" = 404 ]; then ok 7.12 "targeted invitations are single-use, even with max_uses 5"
  else bad 7.12 "targeted invitation with max_uses 5: first accept $a (want 200), second accept $b (want 404)"; fi

  call POST $N0/api/workspaces/$WS2/invitations "{}" "$O_T" >/dev/null; C=$(field invitation.code)
  a=$(call DELETE $N0/api/workspaces/$WS2/invitations/${C:-none} "" "$E_T")
  b=$(call DELETE $N0/api/workspaces/$WS2/invitations/nosuchcode$RUN "" "$O_T")
  c=$(call POST $N0/api/invitations/${C:-none}/accept "{}")
  if [ "$a$b$c" = 403404401 ]; then ok 7.13 "member cannot revoke, unknown code 404, accept needs a token"
  else bad 7.13 "member revokes: $a (want 403), owner revokes an unknown code: $b (want 404), accept with no token: $c (want 401)"; fi

  s=$(call POST $N0/api/workspaces/$WS1/transfer_ownership "{\"new_owner_id\":$(jv "$M_ID")}" "$O_T")
  expect 8.1 "transfer to a member" 400 "$s"

  s=$(call POST $N0/api/workspaces/$WS1/transfer_ownership "{\"new_owner_id\":$(jv "$A_ID")}" "$A_T")
  expect 8.2 "admin calls transfer" 403 "$s"

  s=$(call POST $N0/api/workspaces/$WS1/transfer_ownership "{\"new_owner_id\":$(jv "$A_ID")}" "$O_T")
  ra=$(role_of "$WS1" "$A_T" "$A_ID"); ro=$(role_of "$WS1" "$A_T" "$O_ID")
  if [ "$s" = 200 ] && [ "$ra" = owner ] && [ "$ro" = admin ]; then ok 8.3 "transfer to an admin swaps owner and admin"
  else bad 8.3 "transfer: $s (want 200), new owner role '$ra' (want owner), old owner role '$ro' (want admin)"; fi

  call GET $N0/api/workspaces "" "$A_T" >/dev/null; o1=$(py '[str(w.get("owner_id")) for w in d["workspaces"] if w.get("slug")=="'"$WS1"'"]')
  call GET $N1/api/workspaces/$WS1 "" "$A_T" >/dev/null; o2=$(py 'str(d["workspace"]["owner_id"])')
  a=$(call POST $N0/api/workspaces/$WS1/transfer_ownership "{\"new_owner_id\":$(jv "$Z_ID")}" "$O_T")
  if [ "$o1" = "['$A_ID']" ] && [ "$o2" = "$A_ID" ] && [ "$a" = 403 ]; then ok 8.4 "owner_id follows the transfer, the old owner cannot transfer again"
  else bad 8.4 "after the transfer, owner_id in GET /api/workspaces: $o1, in GET /api/workspaces/{slug}: '$o2' (want $A_ID), old owner transfers again: $a (want 403)"; fi

  a=$(call GET $N0/api/workspaces); b=$(call POST $N0/api/workspaces "{}")
  c=$(call GET $N0/api/workspaces/$WS1/members); d=$(call POST $N0/api/workspaces/$WS1/invitations "{}")
  if [ "$a$b$c$d" = 401401401401 ]; then ok 9.1 "routes return 401 without a token"
  else bad 9.1 "no token: list $a, create $b, members $c, invite $d (expected 401 each)"; fi
}


  boot
  section rule_0_3
  section rules_roles_1
  section rules_roles_2
  section rules_roles_3
  section rules_roles_4
  finish
)

# Without a cluster every channels rule would fail too, and the roles part has already reported it.
if curl -fs --max-time 2 http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
# ===== channels =====
(
  RUN="${BASE}2"

N0=http://127.0.0.1:8000; N1=http://127.0.0.1:8001; N2=http://127.0.0.1:8002
BODY=/tmp/check_channels_body.json
RUN="${RUN:-$(date +%s | tail -c 6)$RANDOM}"   # kept when check_channels_all.sh sources several checks
PW="correct-horse-1"
pass=0; fail=0

ok()  { [ "$FEEDBACK" = verbose ] && echo "PASS $1 $2"; pass=$((pass+1)); }
bad() { [ "$FEEDBACK" = verbose ] && echo "FAIL $1 $2"; fail=$((fail+1)); }
note() { [ "$FEEDBACK" = verbose ] && echo "NOTE $1"; return 0; }
# expect <rule> <description> <expected-status> <actual-status>
expect() { if [ "$4" = "$3" ]; then ok "$1" "$2"; else bad "$1" "$2: expected $3, got $4"; fi; }

# call <METHOD> <URL> [json-body] [token]  -> prints the HTTP status, saves the body in $BODY
call() {
  local args=(-s -o "$BODY" -w '%{http_code}' --max-time 5 -X "$1" "$2")
  [ -n "${3:-}" ] && args+=(-H 'Content-Type: application/json' -d "$3")
  [ -n "${4:-}" ] && args+=(-H "Authorization: Bearer $4")
  curl "${args[@]}" 2>/dev/null || true   # status 000 means no answer within 5s
}
# field <dotted.path>  -> prints that field of the last response body, or nothing
field() {
  python3 -c 'import json,sys
try:
    v=json.load(open(sys.argv[1]))
    for k in sys.argv[2].split("."): v=v[k]
    print(v)
except Exception: pass' "$BODY" "$1"
}
# pick <python expression using d>  -> evaluates it on the last response body d, prints the result
pick() {
  python3 -c 'import json,sys,re
try:
    d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))
except Exception: pass' "$BODY" "$1"
}
# bodies <list-key>  -> the "body" of each item in that list, comma separated
bodies() { pick "\",\".join(m[\"body\"] for m in d[\"$1\"])"; }
# enc <text>  -> URL-encodes text (for cursors)
enc() { python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }
# new_user <NAME>  -> registers a user, sets NAME_T (token) and NAME_ID
new_user() {
  call POST $N0/api/auth/register "{\"username\":\"u${RUN}_$1\",\"password\":\"$PW\"}" >/dev/null
  printf -v "$1_T" '%s' "$(field token)"; printf -v "$1_ID" '%s' "$(field user.id)"
}
# need_base  -> (Spec A routes) users O, B, C, N; workspace WS owned by O; B and C joined #general; N is not in WS
need_base() {
  local u t
  [ -n "${WS:-}" ] && return
  for u in O B C N; do new_user $u; done
  WS="ws-$RUN-c"
  call POST $N0/api/workspaces "{\"slug\":\"$WS\",\"name\":\"Check B\"}" "$O_T" >/dev/null
  WS_ID=$(field workspace.id); GEN=$(field general_channel.id)
  for u in B C; do t="${u}_T"; call POST $N0/api/channels/${GEN:-0}/join "{}" "${!t}" >/dev/null; done
  [ -n "$O_T" ] && [ -n "$WS_ID" ] || note "setup failed (register or create workspace): fix those routes first"
}
# need_ch / need_priv  -> public channel CH and private channel PRIV in WS, made by O (Channels.1 makes them, or here)
need_ch() { [ -n "${CH:-}" ] || { call POST $N0/api/workspaces/$WS/channels "{\"name\":\"chan-$RUN\"}" "$O_T" >/dev/null; CH=$(field channel.id); }; }
need_priv() { [ -n "${PRIV:-}" ] || { call POST $N0/api/workspaces/$WS/channels "{\"name\":\"priv-$RUN\",\"is_private\":true}" "$O_T" >/dev/null; PRIV=$(field channel.id); }; }
# two_pages <url> <list-key> <python expression for one item x>  -> "<page 1 items>|<page 2 items>|<page 2 next_cursor is null>" with limit=1
two_pages() {
  local sep='?' i1 i2 c1 n2
  [[ "$1" == *\?* ]] && sep='&'
  call GET "$1${sep}limit=1" "" "$O_T" >/dev/null; i1=$(pick "[$3 for x in d[\"$2\"]]"); c1=$(field next_cursor)
  call GET "$1${sep}limit=1&cursor=$(enc "$c1")" "" "$O_T" >/dev/null; i2=$(pick "[$3 for x in d[\"$2\"]]"); n2=$(pick 'd["next_cursor"] is None')
  echo "$i1|$i2|$n2"
}
healthy() {
  for u in $N0 $N1 $N2; do curl -fs --max-time 2 "$u/api/health" >/dev/null 2>&1 || return 1; done
}
# boot  -> starts the cluster if it is not answering. Waits at most 15s so a check fits the 30s limit.
boot() {
  if ! healthy; then
    # only start.sh goes to the background (with its own output), so the check still exits cleanly
    (cd /app; setsid nohup /app/start.sh </dev/null >/tmp/start.log 2>&1 &)
    for _ in $(seq 1 15); do healthy && break; sleep 1; done
  fi
  healthy || { bad 0 "cluster is not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)"; finish; exit 1; }
}
# run_py <number-of-rules> <rule-id-if-it-stops-early>  -> runs Python rules from stdin (they import ws_lib.py), counts PASS/FAIL
run_py() {
  local out line r n=0
  out=$(CHECKS="$CHECKS" RUN="$RUN" WS="$WS" O_T="$O_T" B_T="$B_T" timeout 15 python3 - 2>&1)
  # every result goes through ok/bad, so the testing mode applies to the Python rules too
  while IFS= read -r line; do
    case "$line" in
      "PASS "*) r=${line#PASS }; n=$((n+1)); ok "${r%% *}" "${r#* }" ;;
      "FAIL "*) r=${line#FAIL }; n=$((n+1)); bad "${r%% *}" "${r#* }" ;;
    esac
  done <<<"$out"
  while IFS= read -r line; do [ -n "$line" ] && note "$line"; done < <(grep -vE '^(PASS|FAIL) ' <<<"$out" | tail -3)
  [ "$n" -ge "$1" ] || bad "$2" "the WebSocket part stopped early ($n of $1 rules ran)"
}
finish() { echo "RESULT: $pass passed, $fail failed"; [ "$fail" -eq 0 ]; }
# Passing rules print nothing; each failure names its component.
ok()   { pass=$((pass+1)); }
bad()  { [ "$FEEDBACK" = verbose ] && echo "REGRESSION FAIL channels $1 $2"; fail=$((fail+1)); }
note() { [ "$FEEDBACK" = verbose ] && echo "REGRESSION NOTE channels $1"; return 0; }
finish() { echo "$fail" > "$TMP/channels"; }
section() {
  if [ "$SECONDS" -ge 24 ]; then
    bad time "$1 did not run: the suite ran out of time after ${SECONDS}s. The app is too slow; fix that and run it again"
  else "$1"; fi
}


rules_channels_1() {
  local s a b c f t ar cid names n1 n2 LONG BCH T250 LV gone
  # setup from Spec A: users O, B, C, N and workspace WS (B and C are members)
  need_base

  s=$(call POST $N0/api/workspaces/$WS/channels "{\"name\":\"chan-$RUN\"}" "$O_T"); CH=$(field channel.id)
  f=$(pick 'all(k in d["channel"] for k in ["id","workspace_id","name","is_private","is_dm","topic","is_archived"]) and d["channel"]["topic"]=="" and d["channel"]["is_archived"] is False and d["channel"]["is_dm"] is False')
  if [ "$s" = 201 ] && [ "$f" = True ]; then ok 1.1 "create channel returns 201 with every channel field"
  else bad 1.1 "create channel: expected 201 with id, workspace_id, name, is_private, is_dm, topic=\"\", is_archived=false; got $s"; fi

  a=$(call POST $N0/api/workspaces/$WS/channels '{"name":"Bad Name"}' "$O_T")
  b=$(call POST $N0/api/workspaces/$WS/channels "{\"name\":\"chan-$RUN\"}" "$O_T")
  if [ "$a" = 400 ] && [ "$b" = 409 ]; then ok 1.2 "bad name 400, duplicate name 409"
  else bad 1.2 "bad name got $a (want 400), duplicate name got $b (want 409)"; fi

  LONG=$(printf 'x%.0s' $(seq 1 251))
  a=$(call PATCH $N0/api/channels/${CH:-0} '{"topic":"hello"}' "$O_T"); t=$(field channel.topic)
  b=$(call PATCH $N0/api/channels/${CH:-0} "{\"topic\":\"$LONG\"}" "$O_T")
  c=$(call POST $N0/api/workspaces/$WS/channels "{\"name\":\"long-$RUN\",\"topic\":\"$LONG\"}" "$O_T")
  if [ "$a" = 200 ] && [ "$t" = hello ] && [ "$b" = 400 ] && [ "$c" = 400 ]; then ok 1.3 "topic update 200, topic over 250 chars 400"
  else bad 1.3 "PATCH topic: $a with topic '$t' (want 200, hello), 251 chars on PATCH: $b and on create: $c (want 400)"; fi

  s=$(call PATCH $N0/api/channels/${CH:-0} '{"topic":"nope"}' "$C_T")
  expect 1.4 "non-member sets the topic" 403 "$s"

  call POST $N0/api/workspaces/$WS/channels "{\"name\":\"priv-$RUN\",\"is_private\":true}" "$O_T" >/dev/null; PRIV=$(field channel.id)
  s=$(call GET $N0/api/workspaces/$WS "" "$B_T")
  f=$(pick '"workspace" in d and "read_state" in d and isinstance(d["channels"],list)')
  names=$(pick '",".join(c["name"] for c in d["channels"])')
  if [ "$s" = 200 ] && [ "$f" = True ] && [[ ",$names," == *",chan-$RUN,"* ]] && [[ ",$names," != *",priv-$RUN,"* ]]; then
    ok 1.5 "workspace detail lists channels and hides private ones"
  else bad 1.5 "GET /api/workspaces/{slug}: status $s, has workspace/channels/read_state: '$f', channel names: '$names'"; fi

  call POST $N0/api/channels/${CH:-0}/join "{}" "$B_T" >/dev/null
  a=$(call POST $N0/api/channels/${CH:-0}/archive "{}" "$B_T")
  b=$(call POST $N0/api/channels/${CH:-0}/archive "{}" "$O_T"); ar=$(field channel.is_archived)
  if [ "$a" = 403 ] && [ "$b" = 200 ] && [ "$ar" = True ]; then ok 1.6 "only the owner can archive"
  else bad 1.6 "member archive: $a (want 403), owner archive: $b with is_archived '$ar' (want 200, true)"; fi

  a=$(call POST $N0/api/channels/${CH:-0}/messages '{"body":"while archived"}' "$O_T")
  call GET $N0/api/workspaces/$WS "" "$O_T" >/dev/null; n1=$(pick '",".join(c["name"] for c in d["channels"])')
  call GET "$N0/api/workspaces/$WS?include_archived=1" "" "$O_T" >/dev/null; n2=$(pick '",".join(c["name"] for c in d["channels"])')
  if [ "$a" = 423 ] && [[ ",$n1," != *",chan-$RUN,"* ]] && [[ ",$n2," == *",chan-$RUN,"* ]]; then ok 1.7 "archived: post 423, hidden unless include_archived=1"
  else bad 1.7 "post to archived: $a (want 423), listed without flag: $([[ ",$n1," == *",chan-$RUN,"* ]] && echo yes || echo no) (want no), with include_archived=1: $([[ ",$n2," == *",chan-$RUN,"* ]] && echo yes || echo no) (want yes)"; fi

  a=$(call POST $N0/api/channels/${CH:-0}/unarchive "{}" "$O_T"); ar=$(field channel.is_archived)
  b=$(call POST $N0/api/channels/${CH:-0}/messages '{"body":"after unarchive"}' "$O_T")
  if [ "$a" = 200 ] && [ "$ar" = False ] && [ "$b" = 201 ]; then ok 1.8 "unarchive restores posting"
  else bad 1.8 "unarchive: $a with is_archived '$ar' (want 200, false), post after: $b (want 201)"; fi

  a=$(call POST $N0/api/channels/${CH:-0}/leave "{}" "$B_T"); cid=$(field channel.id)
  b=$(call POST $N0/api/channels/${CH:-0}/leave "{}" "$B_T")
  if [ "$a" = 200 ] && [ "$cid" = "$CH" ] && [ "$b" = 200 ]; then ok 1.9 "leave returns 200 {channel}, twice"
  else bad 1.9 "leave: $a (want 200 with channel), leave again: $b (want 200)"; fi

  s=$(call GET $N0/api/channels/${CH:-0}/members "" "$O_T")
  f=$(pick 'all(all(k in u for k in ["id","username","display_name","timezone","avatar_url","status_text","status_emoji"]) for u in d["members"]) and any(str(u["id"])=="'"$O_ID"'" for u in d["members"])')
  if [ "$s" = 200 ] && [ "$f" = True ]; then ok 1.10 "channel members are full user objects"
  else bad 1.10 "GET /api/channels/{id}/members: $s; want {members:[UserObj]} including the owner with all 7 user fields"; fi

  a=$(call POST $N0/api/workspaces/$WS/channels "{\"name\":\"$(printf 'n%.0s' $(seq 1 33))\"}" "$O_T")
  b=$(call POST $N0/api/workspaces/$WS/channels "{\"name\":\"desc-$RUN\",\"description\":\"from description\"}" "$O_T"); t=$(pick 'd["channel"]["topic"]==""')
  if [ "$a" = 400 ] && [ "$b" = 201 ] && [ "$t" = True ]; then ok 1.11 "33-char name 400, description does not fill topic"
  else bad 1.11 "33-char name: $a (want 400), create with description: $b, topic still empty: '$t' (want 201, True)"; fi

  a=$(call POST $N0/api/channels/${CH:-0}/archive "{}" "$O_T")
  b=$(call POST $N0/api/channels/${CH:-0}/unarchive "{}" "$C_T")
  c=$(call POST $N0/api/channels/${CH:-0}/unarchive "{}" "$O_T")
  if [ "$a$b$c" = 200403200 ]; then ok 1.12 "only the owner can unarchive"
  else bad 1.12 "owner archive: $a (want 200), another user unarchive: $b (want 403), owner unarchive: $c (want 200)"; fi

  call POST $N0/api/workspaces/$WS/channels "{\"name\":\"bch-$RUN\"}" "$B_T" >/dev/null; BCH=$(field channel.id)
  s=$(call PATCH $N0/api/channels/${BCH:-0} '{"topic":"set by owner"}' "$O_T")
  expect 1.13 "workspace owner sets the topic of a channel they are not in" 200 "$s"

  T250=$(printf 'y%.0s' $(seq 1 250))
  a=$(call POST $N0/api/workspaces/$WS/channels "{\"name\":\"tp-$RUN\",\"topic\":\"hello\"}" "$O_T"); t=$(field channel.topic)
  b=$(call POST $N0/api/workspaces/$WS/channels "{\"name\":\"t250-$RUN\",\"topic\":\"$T250\"}" "$O_T")
  if [ "$a" = 201 ] && [ "$t" = hello ] && [ "$b" = 201 ]; then ok 1.14 "topic in the create body is kept, 250 chars is allowed"
  else bad 1.14 "create with topic: $a with topic '$t' (want 201, hello), create with a 250-char topic: $b (want 201)"; fi

  call POST $N0/api/workspaces/$WS/channels "{\"name\":\"lv-$RUN\"}" "$O_T" >/dev/null; LV=$(field channel.id)
  call POST $N0/api/channels/${LV:-0}/join "{}" "$C_T" >/dev/null; call POST $N0/api/channels/${LV:-0}/leave "{}" "$C_T" >/dev/null
  s=$(call GET $N1/api/channels/${LV:-0}/members "" "$O_T"); gone=$(pick '"'"$C_ID"'" not in [str(u["id"]) for u in d["members"]]')
  call GET $N0/api/channels/${GEN:-0}/members "" "$O_T" >/dev/null
  f=$(pick '"'"$O_ID"'" in [str(u["id"]) for u in d["members"]] and all(all(k in u for k in ["id","username","display_name","timezone","avatar_url","status_text","status_emoji"]) for u in d["members"])')
  if [ "$s" = 200 ] && [ "$gone$f" = TrueTrue ]; then ok 1.15 "leave removes you from members, #general lists its creator as a full UserObj"
  else bad 1.15 "members after leave: $s, the user is gone: '$gone' (want 200, True); #general members include the creator, all with every UserObj field: '$f' (want True)"; fi
}



rules_channels_2() {
  local s a b c d f i r q u n nb no nc LM pid rc all p1 p2 p3 n3 C1 C2 PG RB WS2_ID GEN2 EC AC AM t
  # setup from Spec A and Channels.1: workspace WS, public channel CH and private channel PRIV (B is not in PRIV)
  need_base; need_ch; need_priv

  s=$(call POST $N0/api/channels/${CH:-0}/messages '{"body":"hello world"}' "$O_T"); M1=$(field message.id)
  f=$(pick 'all(k in d["message"] for k in ["id","channel_id","author_id","author","body","parent_id","reply_count","created_at","edited_at","files","reactions","mentions"]) and d["message"]["parent_id"] is None and d["message"]["reply_count"]==0 and bool(re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|\+00:00)$", d["message"]["created_at"]))')
  if [ "$s" = 201 ] && [ "$f" = True ]; then ok 2.1 "post message returns 201 with every message field"
  else bad 2.1 "post message: $s; want 201 with all 12 message fields, parent_id null, reply_count 0, UTC created_at"; fi

  s=$(call POST $N0/api/channels/${CH:-0}/messages '{"body":""}' "$O_T")
  expect 2.2 "empty body" 400 "$s"

  call POST $N0/api/workspaces/$WS/channels "{\"name\":\"page-$RUN\"}" "$O_T" >/dev/null; PG=$(field channel.id)
  for i in 1 2 3 4 5; do call POST $N0/api/channels/${PG:-0}/messages "{\"body\":\"p$i\"}" "$O_T" >/dev/null; done
  s=$(call GET $N0/api/channels/${PG:-0}/messages "" "$O_T"); all=$(bodies messages); nc=$(pick '"next_cursor" in d and d["next_cursor"] is None')
  if [ "$s" = 200 ] && [ "$all" = "p5,p4,p3,p2,p1" ] && [ "$nc" = True ]; then ok 2.3 "messages are newest first with next_cursor"
  else bad 2.3 "GET messages: $s, order '$all' (want p5,p4,p3,p2,p1), next_cursor null: '$nc'"; fi

  call GET "$N0/api/channels/${PG:-0}/messages?limit=2" "" "$O_T" >/dev/null; p1=$(bodies messages); C1=$(field next_cursor)
  call GET "$N0/api/channels/${PG:-0}/messages?limit=2&cursor=$(enc "$C1")" "" "$O_T" >/dev/null; p2=$(bodies messages); C2=$(field next_cursor)
  call GET "$N0/api/channels/${PG:-0}/messages?limit=2&cursor=$(enc "$C2")" "" "$O_T" >/dev/null; p3=$(bodies messages); n3=$(pick 'd["next_cursor"] is None')
  if [ "$p1|$p2|$p3" = "p5,p4|p3,p2|p1" ] && [ "$n3" = True ]; then ok 2.4 "limit=2 pages through every message once"
  else bad 2.4 "pages were '$p1' | '$p2' | '$p3' (want p5,p4 | p3,p2 | p1), last next_cursor null: '$n3'"; fi

  call GET "$N1/api/channels/${PG:-0}/messages?limit=2&cursor=$(enc "$C1")" "" "$O_T" >/dev/null; q=$(bodies messages)
  if [ "$q" = "p3,p2" ]; then ok 2.5 "a cursor from 8000 works on 8001"
  else bad 2.5 "cursor from 8000 used on 8001 gave '$q' (want p3,p2)"; fi

  call POST $N0/api/channels/${PG:-0}/messages '{"body":"p6"}' "$O_T" >/dev/null
  call GET "$N0/api/channels/${PG:-0}/messages?limit=2&cursor=$(enc "$C1")" "" "$O_T" >/dev/null; q=$(bodies messages)
  if [ "$q" = "p3,p2" ]; then ok 2.6 "a cursor still gives the same page after new messages"
  else bad 2.6 "after posting p6, the old cursor gave '$q' (want p3,p2)"; fi

  a=$(call GET "$N0/api/channels/${PG:-0}/messages?cursor=garbage-$RUN" "" "$O_T")
  b=$(call GET "$N0/api/channels/${PG:-0}/messages?limit=0" "" "$O_T")
  c=$(call GET "$N0/api/channels/${PG:-0}/messages?limit=201" "" "$O_T")
  d=$(call GET "$N0/api/channels/${PG:-0}/messages?limit=200" "" "$O_T")
  if [ "$a$b$c$d" = 400400400200 ]; then ok 2.7 "bad cursor and bad limit return 400"
  else bad 2.7 "made-up cursor: $a, limit=0: $b, limit=201: $c (want 400 each), limit=200: $d (want 200)"; fi

  call POST $N0/api/workspaces/$WS/channels "{\"name\":\"lim-$RUN\"}" "$O_T" >/dev/null; LM=$(field channel.id)
  for i in $(seq 1 51); do call POST $N0/api/channels/${LM:-0}/messages "{\"body\":\"m$i\"}" "$O_T" >/dev/null; done
  s=$(call GET $N0/api/channels/${LM:-0}/messages "" "$O_T"); n=$(pick 'len(d["messages"])'); nc=$(pick 'isinstance(d["next_cursor"],str)')
  if [ "$s" = 200 ] && [ "$n" = 50 ] && [ "$nc" = True ]; then ok 2.8 "limit defaults to 50"
  else bad 2.8 "no limit, 51 messages in the channel: status $s, $n messages, next_cursor is a string: '$nc' (want 200, 50, True)"; fi

  call POST $N0/api/workspaces/$WS/channels "{\"name\":\"eid-$RUN\"}" "$O_T" >/dev/null; EC=$(field channel.id)
  s=$(call POST $N0/api/channels/${EC:-0}/messages '{"body":"no event id"}' "$O_T"); a=$(pick '"event_id" in d["message"]')
  call GET "$N1/api/channels/${EC:-0}/messages?limit=1" "" "$O_T" >/dev/null; b=$(pick '"event_id" in d["messages"][0]')
  if [ "$s" = 201 ] && [ "$a" = False ] && [ "$b" = False ]; then ok 2.9 "REST message objects have no event_id"
  else bad 2.9 "post: $s, event_id in the posted message: '$a', in the listed message: '$b' (want 201, False, False)"; fi

  a=$(call GET $N0/api/channels/${PRIV:-0}/messages "" "$B_T"); b=$(call POST $N1/api/channels/${PRIV:-0}/messages '{"body":"let me in"}' "$B_T")
  if [ "$a$b" = 403403 ]; then ok 2.10 "a user not in a private channel cannot read or post there"
  else bad 2.10 "user not in the private channel: read messages $a, post $b (want 403 each)"; fi

  s=$(call POST $N2/api/channels/${CH:-0}/messages '{"body":"outsider"}' "$N_T")
  expect 2.11 "a user outside the workspace posts to a public channel" 403 "$s"

  s=$(call POST $N0/api/channels/${EC:-0}/messages '{"body":"fields"}' "$O_T")
  f=$(pick 'all(k in d["message"]["author"] for k in ["id","username","display_name","timezone","avatar_url","status_text","status_emoji"]) and d["message"]["edited_at"] is None and d["message"]["files"]==[] and d["message"]["reactions"]==[] and d["message"]["mentions"]==[]')
  a=$(call GET "$N0/api/channels/${EC:-0}/messages?limit=abc" "" "$O_T")
  if [ "$s" = 201 ] && [ "$f" = True ] && [ "$a" = 400 ]; then ok 2.12 "messages have a full author and empty edited_at, files, reactions, mentions; limit=abc 400"
  else bad 2.12 "post: $s, author has every UserObj field and edited_at null, files/reactions/mentions []: '$f' (want 201, True), limit=abc: $a (want 400)"; fi

  s=$(call POST $N0/api/channels/${CH:-0}/messages "{\"body\":\"r1\",\"parent_id\":${M1:-0}}" "$O_T"); pid=$(field message.parent_id)
  call GET "$N0/api/channels/${CH:-0}/messages?limit=200" "" "$O_T" >/dev/null
  rc=$(pick '[m["reply_count"] for m in d["messages"] if str(m["id"])=="'"$M1"'"][0]')
  if [ "$s" = 201 ] && [ "$pid" = "$M1" ] && [ "$rc" = 1 ]; then ok 3.1 "reply has parent_id and bumps reply_count"
  else bad 3.1 "reply: $s with parent_id '$pid' (want 201, $M1), parent reply_count '$rc' (want 1)"; fi

  call POST $N0/api/channels/${CH:-0}/messages "{\"body\":\"r2\",\"parent_id\":${M1:-0}}" "$O_T" >/dev/null
  s=$(call GET $N0/api/messages/${M1:-0}/replies "" "$O_T"); r=$(bodies replies); nc=$(pick '"next_cursor" in d')
  if [ "$s" = 200 ] && [ "$r" = "r1,r2" ] && [ "$nc" = True ]; then ok 3.2 "replies list is oldest first"
  else bad 3.2 "GET replies: $s, order '$r' (want r1,r2), has next_cursor: '$nc'"; fi

  # search results may be messages, or objects holding a "message"
  RB='",".join((x.get("message") or x)["body"] for x in d["results"])'
  call POST $N0/api/channels/${CH:-0}/messages "{\"body\":\"zebra$RUN one\"}" "$O_T" >/dev/null
  call POST $N0/api/channels/${CH:-0}/messages "{\"body\":\"zebra$RUN two\"}" "$O_T" >/dev/null
  s=$(call GET "$N0/api/search?q=zebra$RUN" "" "$O_T"); r=$(pick "$RB"); nc=$(pick '"next_cursor" in d')
  if [ "$s" = 200 ] && [ "$r" = "zebra$RUN two,zebra$RUN one" ] && [ "$nc" = True ]; then ok 3.3 "search finds messages, newest first"
  else bad 3.3 "search: $s, results '$r' (want the 'two' message then 'one'), has next_cursor: '$nc'"; fi

  call POST $N0/api/channels/${PRIV:-0}/messages "{\"body\":\"secret$RUN\"}" "$O_T" >/dev/null
  call GET "$N0/api/search?q=secret$RUN" "" "$B_T" >/dev/null; nb=$(pick 'len(d["results"])')
  call GET "$N0/api/search?q=secret$RUN" "" "$O_T" >/dev/null; no=$(pick 'len(d["results"])')
  if [ "$nb" = 0 ] && [ "$no" = 1 ]; then ok 3.4 "search hides private channels you are not in"
  else bad 3.4 "private message found by a non-member: '$nb' (want 0), by the owner: '$no' (want 1)"; fi

  call POST $N0/api/workspaces "{\"slug\":\"ws-$RUN-d\",\"name\":\"Other\"}" "$O_T" >/dev/null; WS2_ID=$(field workspace.id); GEN2=$(field general_channel.id)
  call POST $N0/api/channels/${GEN2:-0}/messages "{\"body\":\"both$RUN\"}" "$O_T" >/dev/null
  call POST $N0/api/channels/${CH:-0}/messages "{\"body\":\"both$RUN\"}" "$O_T" >/dev/null
  s=$(call GET "$N0/api/search?q=both$RUN&workspace_id=${WS2_ID:-0}" "" "$O_T"); r=$(pick '[str((x.get("message") or x)["channel_id"]) for x in d["results"]]')
  if [ "$s" = 200 ] && [ "$r" = "['$GEN2']" ]; then ok 3.5 "workspace_id limits search to one workspace"
  else bad 3.5 "search with workspace_id: $s, result channel ids $r (want only ['$GEN2'])"; fi

  a=$(call POST $N0/api/messages/${M1:-0}/pin "{}" "$O_T"); f=$(pick 'str(d["pin"]["message_id"])=="'"$M1"'" and "pinned_by" in d["pin"] and "pinned_at" in d["pin"]')
  b=$(call POST $N0/api/messages/${M1:-0}/pin "{}" "$O_T")
  if [ "$a" = 200 ] && [ "$f" = True ] && [ "$b" = 200 ]; then ok 3.6 "pin returns 200 {pin}, pinning again is 200"
  else bad 3.6 "pin: $a with pin object ok '$f' (want 200 and {pin:{message_id,pinned_by,pinned_at}}), pin again: $b (want 200)"; fi

  s=$(call GET $N0/api/channels/${CH:-0}/pins "" "$O_T")
  f=$(pick 'len(d["pins"])==1 and str(d["pins"][0]["message"]["id"])=="'"$M1"'" and "pinned_by" in d["pins"][0] and "next_cursor" in d')
  if [ "$s" = 200 ] && [ "$f" = True ]; then ok 3.7 "pin list shows the pinned message"
  else bad 3.7 "GET pins: $s; want {pins:[{message, pinned_by, pinned_at}], next_cursor} with one pin"; fi

  a=$(call DELETE $N0/api/messages/${M1:-0}/pin "" "$O_T"); u=$(field unpinned)
  call GET $N0/api/channels/${CH:-0}/pins "" "$O_T" >/dev/null; n=$(pick 'len(d["pins"])')
  if [ "$a" = 200 ] && [ "$u" = True ] && [ "$n" = 0 ]; then ok 3.8 "unpin returns unpinned:true and empties the list"
  else bad 3.8 "unpin: $a with unpinned '$u' (want 200, true), pins left: '$n' (want 0)"; fi

  s=$(call GET $N0/api/channels/${PRIV:-0}/pins "" "$B_T")
  expect 3.9 "non-member reads pins of a private channel" 403 "$s"
  local M2

  r=$(two_pages "$N0/api/messages/${M1:-0}/replies" replies 'x["body"]')
  q=$(two_pages "$N0/api/search?q=zebra$RUN" results '(x.get("message") or x)["body"]')
  call POST $N0/api/channels/${CH:-0}/messages '{"body":"second pin"}' "$O_T" >/dev/null; M2=$(field message.id)
  call POST $N0/api/messages/${M1:-0}/pin "{}" "$O_T" >/dev/null; sleep 0.1; call POST $N0/api/messages/${M2:-0}/pin "{}" "$O_T" >/dev/null
  p=$(two_pages "$N0/api/channels/${CH:-0}/pins" pins 'str(x["message"]["id"])')
  if [ "$r" = "['r1']|['r2']|True" ] && [ "$q" = "['zebra$RUN two']|['zebra$RUN one']|True" ] && [ "$p" = "['$M2']|['$M1']|True" ]; then
    ok 3.10 "replies, search and pins page with limit and cursor"
  else bad 3.10 "limit=1 pages (page 1 | page 2 | last next_cursor null): replies $r, search $q, pins $p; want ['r1']|['r2']|True, two then one, ['$M2']|['$M1']|True"; fi

  call POST $N0/api/workspaces/$WS/channels "{\"name\":\"arc-$RUN\"}" "$O_T" >/dev/null; AC=$(field channel.id)
  call POST $N0/api/channels/${AC:-0}/messages '{"body":"before archive"}' "$O_T" >/dev/null; AM=$(field message.id)
  call POST $N0/api/channels/${AC:-0}/archive "{}" "$O_T" >/dev/null
  r=$(call POST $N1/api/channels/${AC:-0}/messages "{\"body\":\"reply\",\"parent_id\":${AM:-0}}" "$O_T")
  t=$(call PATCH $N2/api/channels/${AC:-0} '{"topic":"while archived"}' "$O_T")
  p=$(call POST $N0/api/messages/${AM:-0}/pin "{}" "$O_T")
  call POST $N0/api/channels/${AC:-0}/unarchive "{}" "$O_T" >/dev/null
  u=$(call PATCH $N0/api/channels/${AC:-0} '{"topic":"after unarchive"}' "$O_T"); q=$(call POST $N1/api/messages/${AM:-0}/pin "{}" "$O_T")
  if [ "$r$t$p$u$q" = 423423423200200 ]; then ok 3.11 "archived: reply, topic change and pin are 423; they work after unarchive"
  else bad 3.11 "archived channel: reply $r, topic change $t, pin $p (want 423 each); after unarchive: topic $u, pin $q (want 200 each)"; fi

  s=$(call POST $N0/api/channels/${CH:-0}/messages '{"body":"orphan","parent_id":999999999}' "$O_T")
  if [ "$s" = 404 ] || [ "$s" = 400 ]; then ok 3.12 "a reply to a parent that does not exist is rejected"
  else bad 3.12 "reply with parent_id 999999999: $s (want 404 or 400)"; fi

  for i in 1 2 3; do call POST $N0/api/channels/${EC:-0}/messages "{\"body\":\"kiwi$RUN $i\"}" "$O_T" >/dev/null; done
  call GET "$N0/api/search?q=kiwi$RUN&limit=2" "" "$O_T" >/dev/null; c=$(field next_cursor)
  s=$(call GET "$N2/api/search?q=kiwi$RUN&limit=2&cursor=$(enc "$c")" "" "$O_T"); q=$(pick "$RB")
  if [ "$s" = 200 ] && [ "$q" = "kiwi$RUN 1" ]; then ok 3.13 "a search cursor from 8000 works on 8002"
  else bad 3.13 "search page 2 on 8002 with the cursor from 8000: $s with '$q' (want 200 with 'kiwi$RUN 1')"; fi
}



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


  section rules_channels_1
  section rules_channels_2
  section rules_channels_3
  section rules_channels_4
  finish
)
else
  echo 0 > "$TMP/channels"
fi

# A part that did not write a count crashed. That must count as broken, never as 0.
broken=0
for c in roles channels; do
  n=$(cat "$TMP/$c" 2>/dev/null)
  case "$n" in
    ''|*[!0-9]*) n=1; [ "$FEEDBACK" = verbose ] && echo "REGRESSION FAIL $c 0 this part crashed before it finished" ;;
  esac
  broken=$((broken+n))
done
echo "REGRESSION: $broken broken"
[ "$broken" -eq 0 ]
