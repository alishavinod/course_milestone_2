# Settings.1: settings screens and their hooks

Do this step last, after every other step in this run. It adds no new routes. It connects the
settings and join screens that are already in the page to routes you have already built.

## Uses

| Function | Used here for |
|---|---|
| `POST /api/invitations/{code}/accept` | Joining a workspace from the page |
| `GET /api/channels/{id}/members` | The channel settings member list |
| `GET /api/workspaces/{slug}/members`, `PATCH /api/workspaces/{slug}/members/{user_id}` | The workspace members tab |
| `GET /api/users/{id}` | The username on each member row, if your member list does not include it |
| `POST` and `GET /api/workspaces/{slug}/invitations` | The invitations tab |

## Adds

These `data-testid` values are fixed by the task description. They may be created when the screen
renders:

| Element | Fixed name |
|---|---|
| The error after a failed join | `join-workspace-error` |
| One member in channel settings | `channel-member-row` |
| One member in workspace settings | `member-row` |
| The role shown in a `member-row` | `role-badge` |
| The control that changes a role, a `<select>` in a `member-row` | `role-select` |
| One invitation's code | `invitation-code` |

The join form's text field has `name="code"`. `role-select` has options whose values are `admin`,
`member` and `guest`.

## Rules

1.1 **Failed join.** A user with no workspace sees `join-workspace-form`. Submitting an unknown code
    shows a non-empty `join-workspace-error`.

1.2 **Join.** Submitting a valid code from that form joins the workspace, and `workspace-header`
    shows its name.

1.3 **Channel members.** `channel-settings-btn` opens `channel-settings-modal`. It has one
    `channel-member-row` for each channel member, showing their username.

1.4 **Workspace members.** `workspace-settings-btn` opens `workspace-settings-modal`, and
    `settings-tab-members` shows one `member-row` for each workspace member, with their username and
    a `role-badge` showing their role.

1.5 **Change a role.** As the owner, choosing `admin` in another member's `role-select` changes their
    role on the server, and their `role-badge` then shows `admin`.

1.6 **Invitations.** In `settings-tab-invitations`, `create-invitation-submit` creates an invitation.
    Its code then appears in an `invitation-code`, and that code can be accepted. If
    `create-invitation-submit` is not visible, `create-invitation-btn` must show it.

Every request must answer within 5 seconds.
