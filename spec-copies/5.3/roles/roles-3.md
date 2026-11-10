# Roles.3: roles and channel permissions

Do not start this spec until every rule in Roles.1 and Roles.2 passes.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `require_user(request)` | Roles.1 | The caller of every route |
| `get_workspace(slug)` | Roles.2 | Finding the workspace, `404` if unknown |
| `get_role(workspace_id, user_id)` | Roles.2 | Every permission decision below |
| `create_channel(...)`, `channel_to_json(channel)` | Roles.2 | Channel creation |
| `join_channel(channel, user)` | Roles.2 | Channel joining |

## Adds

| Function | What it does |
|---|---|
| `set_role(workspace, actor_id, target_id, new_role)` | Applies the role-change rules below and stores the new role |
| `can_create_channel(role, is_private)` | True only for the roles allowed in section 5 |
| `can_join_channel(role)` | False for `guest` |

## Rules

### 4. Roles

Change a role with `PATCH /api/workspaces/{slug}/members/{user_id}` and body `{role}`.

4.1 **Promote.** The owner sets a member to `admin` and gets `200` with
    `{member: {user_id, role: "admin"}}`. The members list now shows `admin`.

4.2 **Admin changes a member.** An admin can set a member to `guest` (`200`), but cannot change
    another admin (`403`).

4.3 **Owner is protected.** An admin changing the owner's role gets `403`. The owner changing
    their own role also gets `403`.

4.4 **No PATCH to owner.** Setting `role` to `"owner"` returns `400`. Use `transfer_ownership`.

4.5 **Members cannot change roles.** A `member` changing someone's role gets `403`.

4.6 **Bad role, unknown member.** A role that is not `owner`, `admin`, `member` or `guest`
    (like `"superuser"`) returns `400`. Changing the role of a user who is not in the workspace
    (a registered user, or id `999999999`) returns `404`.

4.7 **Admins promote.** An `admin` can set a `member` to `admin` (`200`).

### 5. Channel permissions

Create a channel with `POST /api/workspaces/{slug}/channels` and body `{name, is_private?}`.
It returns `201 {channel}`.

5.1 **Guests.** A `guest` cannot create a channel (`403`) and cannot join a public channel
    (`403`).

5.2 **Members.** A `member` can create a public channel (`201`) but not a private one (`403`).

5.3 **Admins.** An `admin` can create a private channel (`201`).

Every request must answer within 5 seconds.
