# Read this first: work order and rules for this run

The task description you were given is still the grading contract. The files in `/spec` do not
change it. They tell you what to work on now and how to prove it works. Do not edit any file in
`/spec`, including the checks. If a check fails, fix the code, not the check.

## Scope

This run builds the whole app, components A to E. The app must also keep the cluster conditions: health with the right `node_id` on each node, a rejected WebSocket with a bad token, and the other nodes surviving a killed node.

## Files in /spec

### A and B regression suite

| File | What it is |
|---|---|
| `/spec/checks/regression.sh` | One command that reruns every rule of components A and B |

### Spec A (roles and invitations): rules only

| File | What it is |
|---|---|
| `/spec/roles/roles-1.md` | Roles.1: baseline and auth |
| `/spec/roles/roles-2.md` | Roles.2: workspaces and joining channels |
| `/spec/roles/roles-3.md` | Roles.3: roles and channel permissions |
| `/spec/roles/roles-4.md` | Roles.4: join mode, invitations and ownership transfer |

### Spec B (channels and message history): rules only

| File | What it is |
|---|---|
| `/spec/channels/channels-1.md` | Channels.1: channel management |
| `/spec/channels/channels-2.md` | Channels.2: messages, paging, replies, search, pins |
| `/spec/channels/channels-3.md` | Channels.3: mentions and events |
| `/spec/channels/channels-4.md` | Channels.4: read state, auth and unknown ids |

### Spec C (features built on messages)

| Rules | Check | Step |
|---|---|---|
| `/spec/c/c-1.md` | `/spec/c/checks/check_c_1.sh` | C.1: reactions and DMs |
| `/spec/c/c-2.md` | `/spec/c/checks/check_c_2.sh` | C.2: files |
| `/spec/c/c-3.md` | `/spec/c/checks/check_c_3.sh` | C.3: slash commands |
| `/spec/c/c-4.md` | `/spec/c/checks/check_c_4.sh` | C.4: user groups and mentions |
| `/spec/c/c-5.md` | `/spec/c/checks/check_c_5.sh` | C.5: profiles |

`/spec/c/checks/check_c_all.sh` runs all 40 rules of this component. `c_lib.py`, `lib.sh` in the same folder are shared helpers, not checks to run.

### Spec D (cluster robustness)

| Rules | Check | Step |
|---|---|---|
| `/spec/d/d-1.md` | `/spec/d/checks/check_d_1.sh` | D.1: a killed HTTP node |
| `/spec/d/d-2.md` | `/spec/d/checks/check_d_2.sh` | D.2: a killed Redis |
| `/spec/d/d-3.md` | `/spec/d/checks/check_d_3.sh` | D.3: performance |

`/spec/d/checks/check_d_all.sh` runs all 14 rules of this component. `d_lib.py`, `lib.sh` in the same folder are shared helpers, not checks to run.

### Spec E (IRC, web UI and message edits)

| Rules | Check | Step |
|---|---|---|
| `/spec/e/e-1.md` | `/spec/e/checks/check_e_1.sh` | E.1: message edit and delete |
| `/spec/e/e-2.md` | `/spec/e/checks/check_e_2.sh` | E.2: IRC connection |
| `/spec/e/e-3.md` | `/spec/e/checks/check_e_3.sh` | E.3: IRC channels and bridging |
| `/spec/e/e-4.md` | `/spec/e/checks/check_e_4.sh` | E.4: web UI page and pinned hooks |
| `/spec/e/e-5.md` | `/spec/e/checks/check_e_5.sh` | E.5: web UI accounts, channels and layout |
| `/spec/e/e-6.md` | `/spec/e/checks/check_e_6.sh` | E.6: web UI messages, threads and reactions |

`/spec/e/checks/check_e_all.sh` runs all 38 rules of this component. `e_lib.py`, `lib.sh` in the same folder are shared helpers, not checks to run.

### Last step (settings screens)

| Rules | Check | Step |
|---|---|---|
| `/spec/settings/settings-1.md` | `/spec/settings/checks/check_settings_1.sh` | Settings.1: settings screens and their hooks |

`/spec/settings/checks/check_settings_all.sh` runs all 6 rules of this component. `lib.sh`, `settings_lib.py` in the same folder are shared helpers, not checks to run.

## Order of work

1. Read the Spec A files, then the Spec B files, in order, and build each one. Run `bash /spec/checks/regression.sh` as you go, and keep building until it prints `REGRESSION: 0 broken`. It covers all of A and B, so it reports rules you have not built yet.
2. Read `/spec/c/c-1.md` and build it. Run `bash /spec/c/checks/check_c_1.sh` until every rule passes.
3. Read `/spec/c/c-2.md` and build it. Run `bash /spec/c/checks/check_c_2.sh` until every rule passes.
4. Read `/spec/c/c-3.md` and build it. Run `bash /spec/c/checks/check_c_3.sh` until every rule passes.
5. Read `/spec/c/c-4.md` and build it. Run `bash /spec/c/checks/check_c_4.sh` until every rule passes.
6. Read `/spec/c/c-5.md` and build it. Run `bash /spec/c/checks/check_c_5.sh` until every rule passes.
7. Read `/spec/d/d-1.md` and build it. Run `bash /spec/d/checks/check_d_1.sh` until every rule passes.
8. Read `/spec/d/d-2.md` and build it. Run `bash /spec/d/checks/check_d_2.sh` until every rule passes.
9. Read `/spec/d/d-3.md` and build it. Run `bash /spec/d/checks/check_d_3.sh` until every rule passes.
10. Read `/spec/e/e-1.md` and build it. Run `bash /spec/e/checks/check_e_1.sh` until every rule passes.
11. Read `/spec/e/e-2.md` and build it. Run `bash /spec/e/checks/check_e_2.sh` until every rule passes.
12. Read `/spec/e/e-3.md` and build it. Run `bash /spec/e/checks/check_e_3.sh` until every rule passes.
13. Read `/spec/e/e-4.md` and build it. Run `bash /spec/e/checks/check_e_4.sh` until every rule passes.
14. Read `/spec/e/e-5.md` and build it. Run `bash /spec/e/checks/check_e_5.sh` until every rule passes.
15. Read `/spec/e/e-6.md` and build it. Run `bash /spec/e/checks/check_e_6.sh` until every rule passes.
16. Read `/spec/settings/settings-1.md` and build it. Run `bash /spec/settings/checks/check_settings_1.sh` until every rule passes.

After every step, run `bash /spec/checks/regression.sh`. If it reports anything broken, fix that before doing anything else, even if the check for your step passes.

Do not start a new step while the check for the step you are on fails, except as the limits below
allow.

## Limits

You have 50 minutes, and a spending limit that you will reach at about 150 commands. The run stops there with whatever is in `/app`. Each command costs more than the last, because everything you have read is sent again each time. So:

- Read only the part of a file you need, with `grep -n` or a short `sed -n` range. Do not print whole files.
- Read check files only when you need to.
- Fix several failures in one edit, and then run the check once.
- If the same rule still fails after 3 attempts to fix it, move to the next step, and come back to it at the end if you can.

Aim to spend no more than this. When you reach a number, move on to the next item, even if rules still fail:

- A and B: about 50 commands
- Spec C (features built on messages): about 30 commands
- Spec D (cluster robustness): about 15 commands
- Spec E (IRC, web UI and message edits): about 55 commands
- Last step (settings screens): about 10 commands

## How to build it

- Use Python, FastAPI, `uvicorn[standard]` and SQLite in WAL mode with one database file under
  `/app/data`. List every dependency in `/app/requirements.txt`.
- `/app/start.sh` stays in the foreground and keeps the cluster alive. It runs each HTTP node as
  its own process on ports `8000`, `8001` and `8002`, with `node_id` `0`, `1` and `2`. Have it write
  its own PID to `/tmp/start.pid`.
- Write files in pieces of at most about 300 lines. One very large write can fail.
- Keep the web page in its own files under `/app/static/`, not in a Python string.
- After each component passes, copy `/app` to `/tmp/app-<component>`, so a bad edit can be undone.
- Any command stops after 30 seconds, so do not run a server in the foreground. A check starts
  `/app/start.sh` itself if the cluster is not answering.

## How to verify

Run each check as a single command. Each one finishes in under 30 seconds and ends with a score
such as `RESULT: 7 passed, 2 failed`. To run everything for a component:

```bash
bash /spec/checks/regression.sh
bash /spec/c/checks/check_c_all.sh
bash /spec/d/checks/check_d_all.sh
bash /spec/e/checks/check_e_all.sh
bash /spec/settings/checks/check_settings_all.sh
```

After you change code, restart what you changed. Find the process that owns each port and stop it
by PID. `start.sh` then starts it again:

```bash
for p in 8000 8001 8002; do for pid in $(ss -ltnpH "sport = :$p" | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u); do kill -9 "$pid"; done; done
```

Add other ports to that list if your app listens on them. After you change `start.sh` itself, first
run `kill -9 $(cat /tmp/start.pid)`.

Never stop a process by name: no `pkill -f`, `killall`, `pgrep -f`, or parsing `ps`. Those can match
and kill the agent itself.

## Before you submit

You may run `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` when all of these are true, from output you
actually saw:

- the last line of `bash /spec/checks/regression.sh` is `REGRESSION: 0 broken`;
- `bash /spec/c/checks/check_c_all.sh` ends with `RESULT: 40 passed, 0 failed`;
- `bash /spec/d/checks/check_d_all.sh` ends with `RESULT: 14 passed, 0 failed`;
- `bash /spec/e/checks/check_e_all.sh` ends with `RESULT: 38 passed, 0 failed`;
- `bash /spec/settings/checks/check_settings_all.sh` ends with `RESULT: 6 passed, 0 failed`;
- `GET /api/health` returns `200` on ports `8000`, `8001` and `8002`.

If time runs out first, make sure the cluster still starts, then submit.

These rules are necessary, not sufficient. The full task description still applies.
