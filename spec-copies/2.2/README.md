# Read this first: work order and rules for this run

The task description you were given is still the grading contract. The files in `/spec` do not
change it. They tell you what to work on now and how to prove it works. Do not edit any file in
`/spec`, including the checks. If a check fails, fix the code, not the check.

## Scope

This run builds Spec D (cluster robustness). Before that, build only the basics it needs, as the task description defines them: register and login, workspaces, channels, messages, and the WebSocket (`subscribe`, `resume`, `message.created` with a dense `event_id`). Do not build the rest of roles, invitations, search, pins or read state. IRC is not part of this run.

## Files in /spec

### Spec D (cluster robustness)

| Rules | Check | Step |
|---|---|---|
| `/spec/d/d-1.md` | `/spec/d/checks/check_d_1.sh` | D.1: a killed HTTP node |
| `/spec/d/d-2.md` | `/spec/d/checks/check_d_2.sh` | D.2: a killed Redis |
| `/spec/d/d-3.md` | `/spec/d/checks/check_d_3.sh` | D.3: performance |

`/spec/d/checks/check_d_all.sh` runs all 14 rules of this component. `d_lib.py`, `lib.sh` in the same folder are shared helpers, not checks to run.

## Order of work

1. Read `/spec/d/d-1.md` and build it. Run `bash /spec/d/checks/check_d_1.sh` until every rule passes.
2. Read `/spec/d/d-2.md` and build it. Run `bash /spec/d/checks/check_d_2.sh` until every rule passes.
3. Read `/spec/d/d-3.md` and build it. Run `bash /spec/d/checks/check_d_3.sh` until every rule passes.

Do not start a new step while the check for the step you are on fails, except as the limits below
allow.

## Limits

You have 50 minutes, and a spending limit that you will reach at about 150 commands. The run stops there with whatever is in `/app`. Each command costs more than the last, because everything you have read is sent again each time. So:

- Read only the part of a file you need, with `grep -n` or a short `sed -n` range. Do not print whole files.
- Read check files only when you need to.
- Fix several failures in one edit, and then run the check once.
- If the same rule still fails after 3 attempts to fix it, move to the next step, and come back to it at the end if you can.

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
bash /spec/d/checks/check_d_all.sh
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

- `bash /spec/d/checks/check_d_all.sh` ends with `RESULT: 14 passed, 0 failed`;
- `GET /api/health` returns `200` on ports `8000`, `8001` and `8002`.

If time runs out first, make sure the cluster still starts, then submit.

These rules are necessary, not sufficient. The full task description still applies.
