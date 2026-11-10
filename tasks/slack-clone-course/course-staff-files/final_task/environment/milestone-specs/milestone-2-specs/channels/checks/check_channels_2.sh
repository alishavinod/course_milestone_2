#!/usr/bin/env bash
# Self-check for Channels.2 (messages, paging, replies, search, pins). Prints PASS/FAIL per rule in /spec/channels/channels-2.md.
# Usage: bash /spec/channels/checks/check_channels_2.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

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

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_channels_2; finish; }
