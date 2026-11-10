# Channels.2: messages, paging, replies, search, pins

Do not start this spec until every rule in Channels.1 passes.

## Tips

- **Cursors.** A cursor can be a base64 string holding the last message id of the page. The
  next page is "messages with a smaller id, newest first". A cursor that does not decode is
  `400`.
- Timestamps look like `2026-05-16T08:07:34.123Z` (UTC).

## Objects

- `MessageObj`: `{id, channel_id, author_id, author, body, parent_id, reply_count, created_at,
  edited_at, files, reactions, mentions}`. `author` is a `UserObj`. `files` and `reactions`
  are `[]` for now. `mentions` is a list of user ids (`[]` until Channels.3).

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `require_user(request)`, `user_to_json(user)` | Roles.1 | The caller; the message `author` |
| `get_workspace(slug)` | Roles.2 | `workspace_id` filter in search |
| `get_channel(channel_id)` | Channels.1 | `404`, archived (`423`) and private-channel checks |

## Adds

| Function | What it does |
|---|---|
| `post_message(channel, author, body, parent_id=None)` | Stores a message (or reply) and returns it |
| `message_to_json(message)` | The `message` object with every `MessageObj` field |
| `encode_cursor(message_id)`, `decode_cursor(cursor)` | Opaque cursors; a bad cursor answers `400` |
| `read_limit(request)` | `limit` from the query: default `50`, `1` to `200`, otherwise `400` |
| `list_messages(channel, limit, cursor)` | One page of top-level messages, newest first, plus `next_cursor` |
| `list_replies(parent, limit, cursor)` | Replies, oldest first |
| `search_messages(user, text, workspace_id, limit, cursor)` | Messages the user can see, newest first |
| `pin_message(message, user)`, `unpin_message(message)`, `list_pins(channel, user)` | Pins |

## Rules

### 2. Messages and paging

2.1 **Post.** `POST /api/channels/{id}/messages` with `{body}` returns `201 {message}` with
    every `MessageObj` field. `parent_id` is `null`, `reply_count` is `0`, `created_at` is a
    UTC timestamp.

2.2 **Empty body.** `{body: ""}` returns `400`.

2.3 **Newest first.** `GET /api/channels/{id}/messages` returns `200 {messages, next_cursor}`
    with the newest message first.

2.4 **Paging.** With `?limit=2`, each page has 2 messages and a `next_cursor` string. Passing
    `&cursor=<next_cursor>` returns the next older messages. The last page has
    `next_cursor: null`. No message is skipped or repeated.

2.5 **Cursors work on every node.** A cursor from port `8000` gives the same page on `8001`.

2.6 **Cursors do not move.** New messages posted after a cursor was issued do not change the
    page that cursor returns.

2.7 **Bad paging input.** A made-up cursor returns `400`. `limit=0` and `limit=201` return
    `400`. `limit=200` is allowed. The default limit is `50`.

2.8 **Default limit.** Without `limit`, a channel with 51 messages returns the newest `50`
    and a `next_cursor` string.

2.9 **No `event_id` in REST messages.** Message objects returned by REST routes (post,
    list, replies) do not have an `event_id` field. Only WebSocket event frames carry
    `event_id`.

2.10 **Private channels are private.** A user who is not in a private channel gets `403` on
     `GET` and `POST /api/channels/{id}/messages` for it.

2.11 **Outsiders cannot post.** A user who is not in the channel's workspace gets `403` when
     posting to one of its public channels.

2.12 **Full message fields.** `author` has every `UserObj` field. A new message has
     `edited_at: null`, `files: []`, `reactions: []` and `mentions: []`. `limit=abc` returns
     `400`.

### 3. Replies, search, pins

3.1 **Reply.** Posting `{body, parent_id}` returns `201` with that `parent_id`. The parent's
    `reply_count` goes up by 1.

3.2 **Replies list.** `GET /api/messages/{parent_id}/replies` returns `200 {replies,
    next_cursor}`, oldest first.

3.3 **Search.** `GET /api/search?q=<text>` returns `200 {results, next_cursor}`: messages
    containing the text, newest first.

3.4 **Search only what you can see.** Messages in private channels the caller is not in are
    not returned.

3.5 **Search one workspace.** `&workspace_id=<id>` only returns messages from that workspace.

3.6 **Pin.** `POST /api/messages/{id}/pin` returns `200 {pin: {message_id, pinned_by,
    pinned_at}}`. Pinning again also returns `200`.

3.7 **Pin list.** `GET /api/channels/{id}/pins` returns `200 {pins: [{message, pinned_by,
    pinned_at}], next_cursor}`, newest first.

3.8 **Unpin.** `DELETE /api/messages/{id}/pin` returns `200 {"unpinned": true}`, and the pin
    list is empty again.

3.9 **Private pins.** A user who is not in a private channel gets `403` on its pin list.

3.10 **Paging everywhere.** Replies, search and pins page the same way as messages: with
     `?limit=1` and two items, the first page has one item and a `next_cursor` string, and
     `&cursor=<next_cursor>` gives the other item with `next_cursor: null`.

3.11 **Every write to an archived channel is `423`.** Not only posting (rule 1.7): in an
     archived channel, a thread reply, a topic change (`PATCH /api/channels/{id}`) and
     pinning a message also return `423`, on every node. After unarchive they work again.

3.12 **Replies need a real parent.** A reply with a `parent_id` that does not exist returns
     `404` (or `400`), never `201`.

3.13 **Search cursors work on every node.** A search `next_cursor` from port `8000` gives the
     next page on `8002`.

Every request must answer within 5 seconds.
