#!/usr/bin/env bash
# Self-check for Roles.2 (workspaces and joining). Prints PASS/FAIL per rule in /spec/roles/roles-2.md.
# Usage: bash /spec/roles/checks/check_roles_2.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

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

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_roles_2; finish; }
