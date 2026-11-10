#!/usr/bin/env bash
# Self-check for Roles.3 (roles and channel permissions). Prints PASS/FAIL per rule in /spec/roles/roles-3.md.
# Usage: bash /spec/roles/checks/check_roles_3.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

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

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_roles_3; finish; }
