# Roles.1: baseline and auth

Your service is launched with `/app/start.sh`. The three HTTP nodes listen on
`127.0.0.1:8000`, `:8001`, and `:8002`.

## Tips

- Use FastAPI and `uvicorn[standard]` (list both in `/app/requirements.txt`).
- Run each node as its own process. `start.sh` must keep running and start a node again if it
  dies (rule 0.3). Write it in bash (`#!/usr/bin/env bash`), not `sh`.
- Keep all data in one SQLite file (for example `/app/data/app.db`) so all three nodes see the
  same users, tokens and workspaces.
- Never return `422`. Bad input is `400`. Check the token first: no valid token is `401`.

## Uses

Nothing. This is the first spec.

## Adds

Define these as separate Python functions with these names. Later specs call them.

| Function | What it does |
|---|---|
| `get_db()` | Opens the shared SQLite database |
| `hash_password(password)` | Returns the stored form of a password. Never store the plain password |
| `create_user(username, password, display_name=None)` | Stores a new user and returns it. Signals a conflict if the username exists |
| `issue_token(user_id)` | Creates, stores and returns a new bearer token of 16 or more characters |
| `user_to_json(user)` | Returns the `user` object sent in responses. Never includes the password |
| `require_user(request)` | Reads `Authorization: Bearer <token>` and returns the user, or answers `401` |

## Rules

### 0. Baseline (milestone 0)

0.1 **Health.** `GET /api/health` on ports `8000`, `8001`, `8002` returns `200` with
    `{"status": "ok", "node_id": N}` where `N` is `0`, `1`, `2`.

0.2 **Bad WebSocket token.** A WebSocket connection to `/api/ws?token=<made-up>` is rejected
    on every port, `8000`, `8001` and `8002` (the server does not answer `101`).

0.3 **One node dies, the others keep serving.** When the process on port `8001` is killed
    with `kill -9`, ports `8000` and `8002` keep answering `GET /api/health` with `200`, and
    `start.sh` starts node `8001` again within 8 seconds.

### 1. Auth

1.1 **Register.** `POST /api/auth/register` with `{username, password}` returns `201` with
    `{user, token}`. `token` is at least 16 characters.

1.2 **Duplicate username.** Registering the same username again returns `409`.

1.3 **Bad input.** A username with a space returns `400`. A 7-character password returns `400`.

1.4 **Login.** `POST /api/auth/login` returns `200` with `{user, token}`. A wrong password
    returns `401`.

1.5 **Me.** `GET /api/auth/me` with a valid token returns `200` with `{user}`. No token, or a
    made-up token, returns `401`.

1.6 **Every node.** A token from port `8000` works for `GET /api/auth/me` on `8001` and `8002`.

1.7 **User fields and unknown users.** `display_name` sent to register is kept. The `user` from
    register and from `GET /api/auth/me` has every `UserObj` field: `id, username,
    display_name, timezone, avatar_url, status_text, status_emoji`. Logging in as a username
    that does not exist returns `401`.

Every request must answer within 5 seconds.
