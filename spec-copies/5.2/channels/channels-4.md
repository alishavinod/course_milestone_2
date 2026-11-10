# Channels.4: read state, auth and unknown ids

Do not start this spec until every rule in Channels.1 to Channels.3 passes.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `require_user(request)` | Roles.1 | The caller |
| `get_channel(channel_id)` | Channels.1 | `404` for unknown channels |
| `workspace_detail(...)` | Channels.1 | Now fills `read_state` |
| `emit_event(...)`, the `events` table | Channels.3 | Which messages come after `last_read_event_id` |
| `resolve_mentions(...)` | Channels.3 | Which of those mention the reader |

## Adds

| Function | What it does |
|---|---|
| `get_read_state(user_id, channel_id)` | Works out `{channel_id, last_read_event_id, unread_count, mention_count}` |
| `mark_read(user_id, channel_id, event_id)` | Stores the larger of the old and new `last_read_event_id` |
| `get_message(message_id)` | Returns the message, or answers `404`. Use it in pin, unpin and replies |

## Rules

### 6. Read state

6.1 **Get.** `GET /api/channels/{id}/read` returns `200 {read_state: {channel_id,
    last_read_event_id, unread_count, mention_count}}`. `last_read_event_id` starts at `0`.

6.2 **Counts.** `unread_count` is the number of top-level messages by other users with an
    `event_id` above `last_read_event_id`. `mention_count` is how many of those mention you.
    Thread replies do not count.

6.3 **Mark read.** `POST /api/channels/{id}/read` with `{last_read_event_id}` returns the
    updated `{read_state}`, with counts worked out from the new `last_read_event_id`. Marking
    an older message read leaves the newer ones unread. Marking the latest event read makes
    both counts `0`, and `GET` shows the same.

6.4 **Never backwards.** Posting a smaller `last_read_event_id` does not move it back.

6.5 **In workspace detail.** `read_state` in `GET /api/workspaces/{slug}` is a list with one
    `read_state` object per channel the caller is in.

6.6 **Counts follow the event order.** Events that are not messages (a member joining, a topic
    change) can come before, between and after messages, and never count. After marking any
    message's `event_id` read, `unread_count` is the number of later top-level messages by
    others and `mention_count` is how many of those mention you. Store the `event_id` each
    message got and compare against that; do not count or offset events.

### 7. Auth and unknown ids

7.1 **No token.** `GET` and `POST /api/channels/{id}/messages`, `GET /api/search`,
    `GET /api/channels/{id}/pins`, `GET /api/channels/{id}/read` and `PATCH /api/channels/{id}`
    return `401` with no token.

7.2 **Unknown ids.** For a channel or message id that does not exist, `GET` and
    `POST /api/channels/{id}/messages`, `POST /api/channels/{id}/archive`,
    `POST /api/messages/{id}/pin` and `GET /api/messages/{id}/replies` return `404`.

Every request must answer within 5 seconds.
