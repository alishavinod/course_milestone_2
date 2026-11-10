#!/usr/bin/env bash
# Shared helpers for the E checks. Sourced by check_e_*.sh, not run directly.
FEEDBACK=score_only

N0=http://127.0.0.1:8000; N1=http://127.0.0.1:8001; N2=http://127.0.0.1:8002
CHECKS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN="${RUN:-$(date +%s | tail -c 6)$RANDOM}"   # kept when check_e_all.sh sources several checks
pass=0; fail=0

ok()  { [ "$FEEDBACK" = verbose ] && echo "PASS $1 $2"; pass=$((pass+1)); }
bad() { [ "$FEEDBACK" = verbose ] && echo "FAIL $1 $2"; fail=$((fail+1)); }
note() { [ "$FEEDBACK" = verbose ] && echo "NOTE $1"; return 0; }
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
# run_py <number-of-rules> <rule-id-if-it-stops-early>  -> runs Python rules from stdin, counts PASS/FAIL
run_py() {
  local out line r n=0 left=$((28-SECONDS))
  # keep the whole check inside the 30 second command limit
  if [ "$left" -lt 5 ]; then cat >/dev/null; bad "$2" "not run, out of time: run this step's check on its own"; return; fi
  out=$(CHECKS="$CHECKS" RUN="$RUN" PYTHONPATH="$CHECKS" PLAYWRIGHT_BROWSERS_PATH=/ms-playwright timeout "$left" /opt/test-venv/bin/python -u - 2>&1)
  # every result goes through ok/bad, so the testing mode applies to the Python rules too
  while IFS= read -r line; do
    case "$line" in
      "PASS "*) r=${line#PASS }; n=$((n+1)); ok "${r%% *}" "${r#* }" ;;
      "FAIL "*) r=${line#FAIL }; n=$((n+1)); bad "${r%% *}" "${r#* }" ;;
    esac
  done <<<"$out"
  while IFS= read -r line; do [ -n "$line" ] && note "$line"; done < <(grep -vE '^(PASS|FAIL) ' <<<"$out" | tail -3)
  [ "$n" -ge "$1" ] || bad "$2" "this check stopped early ($n of $1 rules ran)"
}
finish() { echo "RESULT: $pass passed, $fail failed"; [ "$fail" -eq 0 ]; }
