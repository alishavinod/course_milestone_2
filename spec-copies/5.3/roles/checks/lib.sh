#!/usr/bin/env bash
# Shared helpers for the staged roles checks. Sourced by check_roles_*.sh, not run directly.
FEEDBACK=verbose

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
