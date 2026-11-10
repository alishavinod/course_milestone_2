# Channels.1: channel management

Spec B builds on Spec A (register, workspaces, joining). Do not start it until
every rule in Spec A passes, and keep them passing.

## Tips

- Use FastAPI and `uvicorn[standard]`: Channels.3 needs real WebSockets, which Python's
  built-in `http.server` cannot do.

## Objects

- `ChannelObj`: `{id, workspace_id, name, is_private, is_dm, topic, is_archived}`. `topic`
  starts as `""`.
- `UserObj`: `{id, username, display_name, timezone, avatar_url, status_text, status_emoji}`.

## Uses

Call these functions from Spec A. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `require_user(request)` | Roles.1 | The caller of every route |
| `user_to_json(user)` | Roles.1 | Channel member lists (must have every `UserObj` field) |
| `get_workspace(slug)`, `workspace_to_json(workspace)`, `get_role(...)` | Roles.2 | Workspace lookup and the owner check |
| `create_channel(...)`, `channel_to_json(channel)`, `join_channel(...)` | Roles.2 | Creating and joining channels |

## Adds

| Function | What it does |
|---|---|
| `get_channel(channel_id)` | Returns the channel, or answers `404` |
| `set_topic(channel, user, topic)` | Applies rules 1.3 and 1.4 |
| `archive_channel(channel, user, archived)` | Archive or unarchive; workspace owner only |
| `leave_channel(channel, user)` | Removes the user; does nothing if not a member |
| `workspace_detail(workspace, user, include_archived)` | The body of `GET /api/workspaces/{slug}` |

Until Channels.4, `read_state` in workspace detail can be an empty list `[]`.

## Rules

### 1. Channels

1.1 **Create.** `POST /api/workspaces/{slug}/channels` with `{name}` returns `201 {channel}`
    with every `ChannelObj` field. `topic` is `""`, `is_archived` and `is_dm` are `false`.

1.2 **Bad or duplicate name.** The name must match `^[a-z0-9-]{1,32}$`: `Bad Name` returns
    `400`. A name already used in the workspace returns `409`.

1.3 **Topic.** `PATCH /api/channels/{id}` with `{topic}` returns `200 {channel}` with the new
    topic. A topic over 250 characters returns `400` (on PATCH and on create).

1.4 **Who can set the topic.** Only a channel member or the workspace owner. Anyone else gets
    `403`.

1.5 **Workspace detail.** `GET /api/workspaces/{slug}` returns `200 {workspace, channels,
    read_state}`. `channels` leaves out private channels the caller is not in.

1.6 **Archive.** `POST /api/channels/{id}/archive` is workspace-owner only: the owner gets
    `200 {channel}` with `is_archived: true`, anyone else gets `403`.

1.7 **Archived channels.** Posting a message to an archived channel returns `423`. Workspace
    detail leaves archived channels out, unless called with `?include_archived=1`.

1.8 **Unarchive.** `POST /api/channels/{id}/unarchive` (owner only) returns `200 {channel}`
    with `is_archived: false`, and posting works again (`201`).

1.9 **Leave.** `POST /api/channels/{id}/leave` returns `200 {channel}`. Leaving again also
    returns `200`.

1.10 **Channel members.** `GET /api/channels/{id}/members` returns `200 {members: [UserObj,
     ...]}`.

1.11 **More channel input.** A 33-character name returns `400`. A `description` field in the
     create body is ignored: it does not fill `topic`, which stays `""`.

1.12 **Unarchive is owner-only.** Anyone but the workspace owner gets `403` on
     `POST /api/channels/{id}/unarchive`.

1.13 **The owner can set any topic.** The workspace owner can set the topic of a channel they
     are not a member of (`200`). Other non-members still get `403` (rule 1.4).

1.14 **Topic on create.** `topic` in the create body is kept (`"hello"` gives topic `"hello"`).
     A topic of exactly 250 characters is allowed (`201`).

1.15 **Members after leave.** After a user leaves a channel, they are not in
     `GET /api/channels/{id}/members`. The workspace creator is a member of `#general`, and
     every member there is a full `UserObj`.

Rules 1.7 and 1.8 post messages with `POST /api/channels/{id}/messages` and `{body}`. A simple
version that stores the message and returns `201` is enough here; Channels.2 completes it.

Every request must answer within 5 seconds.
