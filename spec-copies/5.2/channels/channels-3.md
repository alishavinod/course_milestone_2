# Channels.3: mentions and events

Do not start this spec until every rule in Channels.1 and Channels.2 passes.

## Tips

- **Events.** Store every event in one `events` table: `(channel_id, event_id, type, payload)`.
  `event_id` is `1 + the highest event_id for that channel`, written in the same transaction
  as the change (`BEGIN IMMEDIATE`). Each node checks the table every ~100 ms and sends new
  rows to its WebSocket subscribers. Then all three nodes see the same numbered events.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `require_user(request)` | Roles.1 | The WebSocket `token` is checked the same way |
| `get_role(workspace_id, user_id)` | Roles.2 | Only workspace members can be mentioned |
| `join_channel(...)` | Roles.2 | Now also adds a `member.joined` event |
| `set_topic(...)`, `archive_channel(...)`, `leave_channel(...)` | Channels.1 | Now also add events |
| `post_message(...)`, `message_to_json(message)` | Channels.2 | Now fill `mentions` and add a message event |

## Adds

| Function | What it does |
|---|---|
| `resolve_mentions(body, channel, author_id)` | The user ids for `mentions` (section 4). Called by `post_message` |
| `emit_event(channel_id, type, payload)` | Stores the next numbered event for the channel |
| `channel_head(channel_id)` | The channel's latest `event_id` (`0` if none) |
| `ws_endpoint` at `/api/ws` | Checks the token, handles `subscribe` and `resume`, sends new events |

## Rules

### 4. Mentions

`mentions` lists the user ids of `@username` in the body.

4.1 **Basic.** `"hi @bob"` gives `mentions: [<bob's id>]`.

4.2 **No repeats, no self.** `"@bob @bob @me"` (written by me) gives just `[<bob's id>]`.

4.3 **Not inside words.** `"me@bob.com"` and `"abc@bob"` mention nobody.

4.4 **Workspace only.** `@username` of a user who is not in the workspace is left out.

4.5 **@channel.** `@channel` mentions every channel member except the author.

4.6 **Channel membership is not needed.** `@username` of a workspace member who is not in the
    channel is still mentioned. Only users outside the workspace are left out (rule 4.4).

4.7 **Punctuation ends a mention.** `"hi @bob, and @carol."` mentions both Bob and Carol.

### 5. Events (WebSocket)

Connect to `ws://127.0.0.1:<port>/api/ws?token=<token>`. Frames are JSON, one per message.

5.1 **Connect.** A valid token connects on `8000` and `8001`. (A bad token is still rejected.)

5.2 **Subscribe.** Sending `{"type": "subscribe", "channel_id": <id>}` gets back
    `{"type": "subscribed", "channel_id": <id>, "head_event_id": <int>}`, where
    `head_event_id` is the channel's latest `event_id` (`0` if none). Every node gives the same
    number.
    Subscribing never sends older events again: after `subscribed`, the first event is the
    next new one. Only `resume` replays history.

5.3 **New message event.** Posting a message sends subscribers
    `{"type": "message.created", "event_id": <head + 1>, "channel_id": <id>, "message": ...}`.

5.4 **Every node.** A subscriber on `8001` gets the same event, with the same `event_id`, for a
    message posted on `8000`.

5.5 **Dense event ids.** These actions each add exactly one event, numbered one after another
    with no gaps: a message (`message.created`), a reply (`message.reply`), a topic change
    (`channel.updated`), a user joining (`member.joined`), a user leaving (`member.left`).
    Archive and unarchive add `channel.archived` and `channel.unarchived`.

5.6 **Resume.** Sending `{"type": "resume", "channel_id": <id>, "since_event_id": <n>}` (on any
    node) sends every event after `n` in order, then
    `{"type": "resumed", "channel_id": <id>, "head_event_id": <latest>}`.

5.7 **Archive events.** Archiving and then unarchiving a channel add `channel.archived` and
    `channel.unarchived`, with the next two event ids.

5.8 **Concurrent writes.** 30 messages posted at the same time across ports `8000`, `8001`
    and `8002` each add exactly one event: the event ids are the next 30 numbers, with no
    gaps and no duplicates, and the channel lists all 30 messages. Give out the next
    `event_id` inside one database transaction (`BEGIN IMMEDIATE`).

5.9 **Frames carry the message.** A `message.created` frame has the full message object under
    `"message"` (with its `id` and `body`), on every node, next to `type`, `event_id` and
    `channel_id`.

5.10 **No events from private channels you are not in.** A user who is not in a private
     channel and sends `subscribe` for it gets no `message.created` frames for that channel.

5.11 **Reply frames.** A thread reply sends `message.reply` with the reply under `"message"`
     (its `body` and `parent_id`).

Every request must answer within 5 seconds.
