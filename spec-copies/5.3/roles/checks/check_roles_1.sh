#!/usr/bin/env bash
# Self-check for Roles.1 (baseline and auth). Prints PASS/FAIL per rule in /spec/roles/roles-1.md.
# Usage: bash /spec/roles/checks/check_roles_1.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

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

[ "${CHECK_ALL:-}" = 1 ] || { boot; rule_0_3; rules_roles_1; finish; }
