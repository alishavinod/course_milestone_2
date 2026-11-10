#!/usr/bin/env bash
# Self-check for Channels.1 (channel management). Prints PASS/FAIL per rule in /spec/channels/channels-1.md.
# Usage: bash /spec/channels/checks/check_channels_1.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

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

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_channels_1; finish; }
