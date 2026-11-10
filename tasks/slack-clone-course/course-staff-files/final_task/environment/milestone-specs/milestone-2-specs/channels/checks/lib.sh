#!/usr/bin/env bash
# Shared helpers for the staged channels checks. Sourced by check_channels_*.sh, not run directly.
FEEDBACK=verbose

N0=http://127.0.0.1:8000; N1=http://127.0.0.1:8001; N2=http://127.0.0.1:8002
BODY=/tmp/check_channels_body.json
CHECKS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
