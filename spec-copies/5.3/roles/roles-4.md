# Roles.4: join mode, invitations and ownership transfer

Do not start this spec until every rule in Roles.1 to Roles.3 passes.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `require_user(request)` | Roles.1 | The caller of every route; `401` without a token |
| `get_workspace(slug)`, `workspace_to_json(workspace)` | Roles.2 | Workspace lookup and responses |
| `get_role(workspace_id, user_id)` | Roles.2 | Admin/owner-only checks |
| `add_member(workspace_id, user_id, role)` | Roles.2 | Accepting an invitation adds a `member` |
| `join_channel(channel, user)` | Roles.2 | Must now refuse non-members of an `invite_only` workspace |
| `set_role(...)` | Roles.3 | Roles after a transfer |

## Adds

| Function | What it does |
|---|---|
| `update_workspace(workspace, fields)` | Applies `{name?, join_mode?}` |
| `create_invitation(workspace, creator_id, expires_in_seconds, max_uses, invited_user_id)` | Stores an invitation with a new random `code` |
| `accept_invitation(code, user)` | Checks the rules in section 7, uses up one use in a single SQL statement, adds the member |
| `list_invitations(workspace)` | Active invitations only (not used up, expired or revoked) |
| `revoke_invitation(workspace, code)` | Marks the invitation revoked |
| `transfer_ownership(workspace, actor_id, new_owner_id)` | Applies section 8, including the new `owner_id` |

## Rules

### 6. Join mode

`PATCH /api/workspaces/{slug}` takes `{name?, join_mode?}` and returns `200 {workspace}`.

6.1 **Change join mode.** The owner sets `join_mode` to `"invite_only"` and gets `200`. A
    `member` trying the same PATCH gets `403`.

6.2 **Invite-only.** In an `"invite_only"` workspace, a non-member joining a channel gets `403`.

6.3 **Bad join mode.** `join_mode` of `"sometimes"` returns `400`.

6.4 **Rename.** `PATCH /api/workspaces/{slug}` with `{name}` by the owner or an admin returns
    `200 {workspace}` with the new name.

### 7. Invitations

7.1 **Create.** `POST /api/workspaces/{slug}/invitations` by the owner returns `201` with
    `{invitation}` that has a `code`. A `member` gets `403`.

7.2 **Accept.** `POST /api/invitations/{code}/accept` returns `200`. The user becomes a
    `member` and can now join channels in the invite-only workspace.

7.3 **Used up or unknown.** `max_uses` defaults to `1`, so a second user accepting the same
    code gets `404`. An unknown code returns `404`.

7.4 **Targeted.** For an invitation with `{invited_user_id}`, any other user gets `403`. The
    invited user gets `200`.

7.5 **Expired.** An invitation with `{expires_in_seconds: 1}`, accepted 2 seconds later,
    returns `404`.

7.6 **List.** `GET /api/workspaces/{slug}/invitations` by the owner returns `200` with
    `{invitations: [...]}` including active codes. A `member` gets `403`.

7.7 **Revoke.** `DELETE /api/workspaces/{slug}/invitations/{code}` by the owner returns `200`
    with `{"deleted": true}`. Accepting that code afterwards returns `404`.

7.8 **More uses.** With `{max_uses: 2}`, two different users accept with `200` and a third
    gets `404`.

7.9 **Targeted by username.** For an invitation with `{invited_username}`, any other user gets
    `403`. The invited user gets `200`.

7.10 **Admins invite too.** An `admin` creating an invitation gets `201`.

7.11 **Only active invitations are listed.** Once every use of a code is taken, it no longer
     appears in `GET /api/workspaces/{slug}/invitations` (nor do revoked or expired codes).

7.12 **Targeted invitations are single-use.** An invitation with `invited_user_id` (or
     `invited_username`) can be accepted once, even if `max_uses` is larger: the invited user
     gets `200`, and accepting it again returns `404`.

7.13 **Revoke and accept rules.** A `member` revoking an invitation gets `403`. Revoking a code
     that does not exist returns `404`. Accepting with no token returns `401`.

### 8. Ownership transfer

`POST /api/workspaces/{slug}/transfer_ownership` with body `{new_owner_id}`. Owner only.

8.1 **Must be an admin.** Transferring to a `member` returns `400`.

8.2 **Owner only.** An `admin` calling it gets `403`.

8.3 **Transfer.** Transferring to an `admin` returns `200`. The target becomes `owner` and the
    old owner becomes `admin`.

8.4 **`owner_id` follows the owner.** After a transfer, `owner_id` is the new owner in
    `GET /api/workspaces` and `GET /api/workspaces/{slug}`. The old owner is now an `admin`,
    so they get `403` if they call `transfer_ownership` again.

### 9. Auth everywhere

9.1 **No token.** `GET /api/workspaces`, `POST /api/workspaces`,
    `GET /api/workspaces/{slug}/members` and `POST /api/workspaces/{slug}/invitations` return
    `401` with no token.

Every request must answer within 5 seconds.
