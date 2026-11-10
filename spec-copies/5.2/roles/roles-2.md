# Roles.2: workspaces and joining channels

Do not start this spec until every rule in Roles.1 passes.

## Uses

Call these functions from Roles.1. Do not write a second version of any of them.

| Function | Used here for |
|---|---|
| `get_db()` | Every read and write |
| `require_user(request)` | The caller of every route below |
| `user_to_json(user)` | User objects in responses |

## Adds

| Function | What it does |
|---|---|
| `get_workspace(slug)` | Returns the workspace, or answers `404` |
| `get_role(workspace_id, user_id)` | Returns `owner`, `admin`, `member`, `guest`, or `None` if not a member |
| `add_member(workspace_id, user_id, role)` | Adds a workspace member (does nothing if already a member) |
| `workspace_to_json(workspace)` | The `workspace` object: `{id, slug, name, owner_id, join_mode}` |
| `create_channel(workspace_id, name, is_private, creator_id)` | Stores a channel; the creator is its first member |
| `channel_to_json(channel)` | The `channel` object |
| `join_channel(channel, user)` | Adds the user to the channel (and to an open workspace as `member`) |

Roles are per workspace: `owner`, `admin`, `member`, `guest`.

## Rules

### 2. Workspaces

2.1 **Create.** `POST /api/workspaces` with `{slug, name}` returns `201` with
    `{workspace, general_channel}`. `workspace` has `id, slug, name, owner_id, join_mode`.
    `owner_id` is the creator, `join_mode` is `"open"`, and `general_channel.name` is
    `"general"`. The creator's role is `owner`.

2.2 **Duplicate slug.** The same slug again returns `409`.

2.3 **Bad slug.** The slug must match `^[a-z0-9-]{2,32}$`. `Bad_Slug` returns `400`.

2.4 **List.** `GET /api/workspaces` returns `200` with `{workspaces: [...]}`, only the ones the
    caller is a member of.

2.5 **Members.** `GET /api/workspaces/{slug}/members` returns `200` with
    `{members: [{user_id, role}, ...]}`. The creator has role `owner`.

2.6 **Workspace fields and unknown slugs.** Each workspace in `GET /api/workspaces` has `id,
    slug, name, owner_id, join_mode`. `GET /api/workspaces/{slug}` for a slug that does not
    exist returns `404`. A 1-character slug (`a`) returns `400`.

### 3. Joining channels

3.1 **Open workspace.** When `join_mode` is `"open"`, a user who is not a member can call
    `POST /api/channels/{id}/join` on a public channel and gets `200`. They become a workspace
    `member`.

3.2 **Unknown channel.** Joining channel `999999999` returns `404`.

3.3 **Private channel.** Joining a private channel you are not in returns `403`.

Every request must answer within 5 seconds.
