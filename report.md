# Milestone 2 report

- Name: Alisha Vinod
- UNI: av3311

All results are from my own checks.

## Runs

One row per repeat of each run (22 in total). "Run" is the run in the run plan in README Setup.

- Job folders are under `tasks/slack-clone-course/jobs/`. Results are out of A 51, B 66, C 40, D 14, E 38, Settings 6.
- Check results come from scoring the final `/app` on a fresh boot, as the grader does (fresh container, install `requirements.txt`, run `start.sh`), not from what the agent last saw.
- "Ran checks" counts commands that ran a check or the regression suite. Cost is what mini-swe-agent reports, not what was billed (OpenRouter billed $3.79 in total against about $27.95 reported).

| Run | Component | Repeat | Job folder | Check results | Cost | Ran checks (times) | Submitted with failures? | Problems |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1.1 | C | 1 | `2026-11-06__13-08-27` | C: 40 passed, 0 failed | $0.27 | 18 | No | None |
| 1.1 | C | 2 | `2026-11-06__13-16-32` | C: 40 passed, 0 failed | $0.52 | 29 | No | None |
| 1.2 | D | 1 | `2026-11-06__13-24-00` | D: 14 passed, 0 failed | $0.09 | 6 | No | Tried `FEEDBACK=verbose` on the command line; it stayed score only |
| 1.2 | D | 2 | `2026-11-06__13-26-34` | D: 14 passed, 0 failed | $0.06 | 3 | No | None |
| 1.3 | E | 1 | `2026-11-06__13-28-24` | E: 36 passed, 2 failed | $0.80 | 39 | Not knowingly | Saw 38 of 38 in the run; 3.5 and 3.6 fail on 3 fresh scorings. Cause not found |
| 1.3 | E | 2 | `2026-11-06__13-40-45` | E: 38 passed, 0 failed | $0.68 | 25 | No | None |
| 2.1 | C | 1 | `2026-11-06__13-51-40` | C: 40 passed, 0 failed | $0.24 | 13 | No | None |
| 2.1 | C | 2 | `2026-11-06__13-55-49` | C: 40 passed, 0 failed | $0.21 | 14 | No | None |
| 2.2 | D | 1 | `2026-11-06__14-01-03` | D: 14 passed, 0 failed | $0.34 | 15 | No | 6 commands hit errors or the 30 s limit |
| 2.2 | D | 2 | `2026-11-06__14-10-14` | D: 0 passed, 1 failed (app does not boot) | $0.07 | 1 | Never submitted | `AgentTimeoutError`: hung on one uvicorn command until the 50-minute limit |
| 2.3 | E | 1 | `2026-11-06__15-00-50` | E: 38 passed, 0 failed | $0.33 | 18 | No | None |
| 2.3 | E | 2 | `2026-11-06__15-09-24` | E: 38 passed, 0 failed | $0.43 | 26 | No | None |
| 3 | C, D, E | 1 | `2026-11-06__15-40-00` | C: 40 passed, 0 failed; D: 12 passed, 2 failed; E: 32 passed, 6 failed | $1.53 | 43 | Yes | Last all-E check showed 13 failed. A fake `redis-cli subscribe` process failed D 2.1 and 2.6. IRC fails on a fresh boot |
| 3 | C, D, E | 2 | `2026-11-06__19-27-35` | C: 40 passed, 0 failed; D: 14 passed, 0 failed; E: 38 passed, 0 failed | $1.41 | 39 | No | OpenRouter with no reasoning setting |
| 4 | C, D, E | 1 | `2026-11-06__15-55-32` | C: 40 passed, 0 failed; D: 14 passed, 0 failed; E: 38 passed, 0 failed | $1.82 | 61 | No | None |
| 4 | C, D, E | 2 | `2026-11-06__19-45-24` | C: 40 passed, 0 failed; D: 14 passed, 0 failed; E: 38 passed, 0 failed | $0.73 | 24 | No | OpenRouter with no reasoning setting |
| 5.1 | A to E | 1 | `2026-11-06__16-12-44` | A and B: 18 broken; C: 40 passed, 0 failed; D: 12 passed, 2 failed; E: 25 passed, 13 failed | $3.00 | 57 | Never submitted | `LimitsExceeded` at the $3 cap, on E.6. A was 45 of 51 on one fresh scoring and 50 on another |
| 5.1 | A to E | 2 | `2026-11-06__23-32-32` | A and B: 71 broken; C: 35 passed, 5 failed; D: 11 passed, 3 failed; E: 38 passed, 0 failed | $2.78 | 93 | Yes | Rerun of a failed start (see Notes). Regression suite timed out at command 59 and was never run again |
| 5.2 | A to E | 1 | `2026-11-06__22-40-18` | A and B: 19 broken; C: 40 passed, 0 failed; D: 14 passed, 0 failed; E: 24 passed, 14 failed; Settings: 0 passed, 6 failed | $3.01 | 55 | Never submitted | `LimitsExceeded` at the $3 cap, on E.4 |
| 5.2 | A to E | 2 | `2026-11-06__23-03-21` | A and B: 35 broken; C: 37 passed, 3 failed; D: 3 passed, 11 failed; E: 17 passed, 21 failed; Settings: 6 passed, 0 failed | $2.58 | 87 | Yes | Submitted seeing `REGRESSION: 11 broken`. On a fresh boot a killed node does not return in time, so D and later checks fail (same on 2 scorings) |
| 5.3 | A to E | 1 | `2026-11-07__14-06-41` | A: 51 passed, 0 failed; B: 66 passed, 0 failed; C: 40 passed, 0 failed; D: 8 passed, 6 failed; E: 26 passed, 12 failed; Settings: 0 passed, 6 failed | $3.03 | 61 | Never submitted | Fixed 5.3 README (see Notes). `LimitsExceeded` at the $3 cap, on E.5. Never ran a D check |
| 5.3 | A to E | 2 | `2026-11-07__14-29-24` | A: 13 passed, 38 failed; B: 60 passed, 6 failed; C: 40 passed, 0 failed; D: 5 passed, 2 failed (check stopped early); E: 24 passed, 14 failed; Settings: 0 passed, 6 failed | $3.03 | 101 | Never submitted | Fixed 5.3 README. `LimitsExceeded` at the $3 cap, on E.5. A and B passed in the run but broke later (see 4a). A was 12 on a second scoring |

### Notes on how the runs were done

**Route and reasoning setting.** My OpenAI credit ran low, so I switched to OpenRouter part way through. Comparisons that cross these settings are weaker:

| Runs | Route | Reasoning setting |
| --- | --- | --- |
| 1.1 to 2.3 (both repeats), 3 r1, 4 r1, 5.1 r1 | OpenAI direct | None |
| 3 r2, 4 r2, and both failed starts | OpenRouter | None |
| 5.1 r2, 5.2 r1 and r2, every 5.3 run | OpenRouter | `low` |

With OpenRouter and no reasoning setting, the first reply sometimes invented a whole session and submitted an empty `/app` (about $0.01). This happened in the first 5.1 r2 attempt (`2026-11-06__19-56-05`) and the first old 5.3 attempt (`2026-11-06__20-30-49`). In a separate test of the same first prompt, 10 of 11 replies did this with no setting, against 0 of 9 with `low` and 0 of 6 on OpenAI direct. So I set `low` and reran both, following the README ("Run it again once").

**5.3 README fix.** My first two 5.3 runs (`2026-11-06__21-28-32`, `2026-11-06__22-00-36`) showed a fault in the spec: nothing told the agent that grading installs only `requirements.txt` into a fresh container, and the first run did not boot because of a bad line in it. Because 5.3 is the graded spec, I changed only the 5.3 README. It now says to run `pip install -q -r /app/requirements.txt` after changing that file and before submitting, and to run staged checks one at a time if an `_all` check passes 30 s. No check and no other spec changed, and I refroze the specs. I then ran the fixed 5.3 four times. The first two, chosen before they ran, are the counted rows above. The other two scored A 51 and 50, B 66 and 21, C 40 and 22, D 14 and 2, E 36 and 17. All four boot from a fresh install, but all hit the $3 cap during E.5 or E.6. The old runs are not counted, because their spec differs from the graded one.

**Other job folders.** Only the 22 job folders above are in this repo. The failed starts, old 5.3 runs, fixed 5.3 runs 3 and 4, and practice runs named in these notes were kept locally and left out.

**Six late hooks.** After freezing, I found six pinned hooks no check covered (`join-workspace-error`, `channel-member-row`, `member-row`, `role-badge`, `role-select`, `invitation-code`). Before any counted 5.2 or 5.3 run, I added them as a last "Settings" step in those two runs only. Specs A to E did not change.

**Known weaknesses in my checks.**

- The regression suite and the `_all` checks can pass the 30 s command limit on a large or unhealthy app and then print nothing (14 of about 70 such runs in the whole-app runs). A suite section that does not run counts as only 1 broken, so a low count can mislead.
- Nothing makes the agent rerun A and B checks after later steps except the regression suite (5.1, 5.2) and the 5.3 submit list, which runs that hit the cap never reach.
- My checks do not test `resume.gap` or `blocks`.

## Experiments

### 1) Feedback

> Do you find that adding detailed output to the checks helps the agent?

#### Runs compared and why

1.1, 1.2, 1.3 against 2.1, 2.2, 2.3. Each pair builds the same component alone from the same spec; only the output mode differs (`score_only` or `verbose`). All 12 used the same route and setting.

#### Supporting results

| Component | Mode | Checks passed, r1 and r2 | Cost | Model calls | Ran checks |
| --- | --- | --- | --- | --- | --- |
| C | `score_only` | 40/40, 40/40 | $0.27, $0.52 | 38, 59 | 18, 29 |
| C | `verbose` | 40/40, 40/40 | $0.24, $0.21 | 31, 30 | 13, 14 |
| D | `score_only` | 14/14, 14/14 | $0.09, $0.06 | 18, 14 | 6, 3 |
| D | `verbose` | 14/14, does not boot | $0.34, $0.07 | 51, 14 | 15, 1 |
| E | `score_only` | 36/38, 38/38 | $0.80, $0.68 | 77, 71 | 39, 25 |
| E | `verbose` | 38/38, 38/38 | $0.33, $0.43 | 41, 51 | 18, 26 |

#### Answer

Yes for C and E, mainly in effort rather than score. With detailed output the agent reached the same or a better score for about half the cost on E ($0.38 against $0.74 on average) and on C ($0.23 against $0.40), with fewer calls. The only E failures were in `score_only`.

D does not show the benefit: `score_only` got 14/14 twice very cheaply. I do not blame D's failed boot in `verbose` repeat 2 on the feedback, because the agent hung on one shell command after running the check once.

Almost every run hit the maximum, so these results show detail saves effort, not that it is needed to finish a small component.

#### Consistency across repeats

C and E are consistent: `verbose` was cheaper in all four pairings, though C repeat 1 is close ($0.24 against $0.27). D is not, because of the hung run.

#### Other evidence

Without detailed output the agent opens the check scripts to find out what failed, while it reads the spec about as often in both modes:

| Run | Reads of check files, r1 and r2 | Reads of spec files, r1 and r2 |
| --- | --- | --- |
| 1.1 (C, `score_only`) | 4, 14 | 2, 1 |
| 2.1 (C, `verbose`) | 2, 1 | 3, 3 |
| 1.3 (E, `score_only`) | 15, 14 | 4, 2 |
| 2.3 (E, `verbose`) | 3, 4 | 3, 2 |

So `score_only` does not hide the details from an agent that can read the checks; it only makes them cost more. In 1.2 r1 the agent even tried to turn on `FEEDBACK=verbose` itself.

### 2) Tests as the spec

> Can the checks alone replace the written rules?

#### Runs compared and why

3 against 4. Both build C, D and E together with `verbose` checks; only Run 3 has the written specs. In each repeat both runs used the same route and setting, so I compare them repeat by repeat.

#### Supporting results

| Run | Repeat | C | D | E | Total of 92 | Cost | Model calls | Ran checks | Reads of check files | Reads of spec files |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 3 (specs and checks) | 1 | 40 | 12 | 32 | 84 | $1.53 | 106 | 43 | 8 | 10 |
| 3 (specs and checks) | 2 | 40 | 14 | 38 | 92 | $1.41 | 94 | 39 | 18 | 14 |
| 4 (checks only) | 1 | 40 | 14 | 38 | 92 | $1.82 | 112 | 61 | 8 | 0 |
| 4 (checks only) | 2 | 40 | 14 | 38 | 92 | $0.73 | 70 | 24 | 15 | 0 |

#### Answer

For what my checks measure, yes. Run 4 got 92 of 92 in both repeats. I have no evidence that the specs caused Run 3's one shortfall, so I do not claim checks alone are better.

The limit is that these are the same checks the agent was given, so the result is partly circular. The checks' `FAIL` lines also state expected values, so they carry much of the rules' content. What replaced the written rules was `verbose` checks plus the task description. I would still keep written specs for the graded run, because they cover intent no check tests.

For a measure outside the checks the agent saw, I scored all four apps with my A and B checks, which neither run was given. Both runs were told to build only the basics of A and B, so low scores are expected:

| Run | Repeat | A (of 51) | B (of 66) | Accepts a WebSocket with a made-up token (A 0.2, a milestone 0 boot condition) |
| --- | --- | --- | --- | --- |
| 3 (specs and checks) | 1 | 15 | 28 | Yes |
| 3 (specs and checks) | 2 | 18 | 28 | Yes |
| 4 (checks only) | 1 | 16 | 28 | Yes |
| 4 (checks only) | 2 | 20 | 29 | No |

The checks-only apps are no worse here. But three of the four apps break a boot condition that no C, D or E check covers: anything the checks do not test is left to chance.

#### Consistency across repeats

Run 4 was consistent in score (92, 92) but not in cost ($1.82, $0.73). Run 3 varied (84, 92). With two repeats I cannot say the difference between the runs is real.

#### Other evidence

In Run 4 the agent read the check files 8 and 15 times, using them as the rules; Run 3 read them about as often. Run 3 r1's D failure was a fake `redis-cli subscribe` process that made the Redis step look done; the D.2 check correctly failed it, and the agent submitted anyway.

### 3) Scale

> Do you find that a component passes fewer of its checks when the agent builds it together with
> other components, rather than on its own?

#### Runs compared and why

2.1, 2.2, 2.3 (each component alone, `verbose`) against 3 (C, D and E together, `verbose`): same mode and specs, only the amount of work changes. Run 5 is larger again and listed as further evidence, but it also has A and B to build and a $3 cap that it can reach.

#### Supporting results

| Component | Alone (2.x), r1 and r2 | Together (3), r1 and r2 | Whole app (5.1 r1, r2) | Whole app (5.2 r1, r2) | Whole app (5.3 r1, r2) |
| --- | --- | --- | --- | --- | --- |
| C (of 40) | 40, 40 | 40, 40 | 40, 35 | 40, 37 | 40, 40 |
| D (of 14) | 14, does not boot | 12, 14 | 12, 11 | 14, 3 | 8, 5 (check stopped early) |
| E (of 38) | 38, 38 | 32, 38 | 25, 38 | 24, 17 | 26, 24 |

The last three columns use OpenRouter `low`, except 5.1 r1. The four uncounted 5.3 runs (see Notes) scored E 11 (after I fixed its `requirements.txt` so it would boot), 38, 36 and 17.

#### Answer

Yes, but not always, and mostly for D and E. C stays at 40/40 except in two whole-app runs. E was 38/38 twice alone, fell to 32 in one Run 3 repeat, and was below full in 5 of the 6 counted whole-app repeats. Run 3 r2 and the old 5.3 r2 (215 of 215) show the full app can pass everything, so scale raises the risk rather than setting a limit.

Two things drive the loss. D and E come last, so they are cut off when the budget runs out (5.1 r1, 5.2 r1, both counted 5.3 runs, which spent much of their budget on the browser step E.5). Later changes also break earlier work, or cluster behaviour fails only on a fresh boot (Run 3 r1 IRC, 5.2 r2 D).

#### Consistency across repeats

Not consistent. In Run 3 one repeat lost 8 checks and the other none, and across six 5.3 runs E ranged from 11 to 38. Scale shows up as a higher chance of failure, not a steady drop. Run 3 r1's lower score came on the same route as the alone runs, so the route does not explain it.

#### Other evidence

In Run 3 r1 the agent submitted after its last all-E check showed 13 failed; no 1.x or 2.x run knowingly did this. In 5.1 r2 each C step passed when built, but 5 C rules fail in the final app, so later work broke them or they fail only on a fresh boot.

### 4) Regression

> a) Do you find that building components C, D, and E breaks features from A and B that were
> already working?

> b) Does it make a difference whether the agent has Specs A and B or only their regression
> tests?

> c) Does it make a difference whether the A and B checks are given as a regression suite or as
> plain checks?

#### Runs compared and why

- **4a:** within each of 5.1, 5.2 and 5.3, which A and B rules passed before C started against which pass in the final app.
- **4b:** 5.1 against 5.2. Both have the regression suite; only 5.2 has Specs A and B.
- **4c:** 5.2 against 5.3. Both have Specs A and B; 5.2 checks them with one suite, 5.3 with staged checks.

All of these runs are on OpenRouter `low` except 5.1 r1. The 5.3 README fix was about `requirements.txt` and timeouts, not A or B, so I also use the uncounted 5.3 runs as extra evidence.

#### Supporting results

| Run | Repeat | A and B broken before C | A and B broken, final app | A (of 51) | B (of 66) | C work starts at command | Regression or A/B all-check runs | Reads of Specs A and B |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 5.1 | 1 | 54 | 18 | 45 (50 on another scoring) | 54 | 20 | 17 | 0 (none given) |
| 5.1 | 2 | 71 | 71 | 18 | 23 | 13 | 4 | 0 (none given) |
| 5.2 | 1 | 24 | 19 | 51 | 47 | 57 | 14 | 1 |
| 5.2 | 2 | 29 | 35 (24 by staged checks) | 38 | 55 | 35 | 15 | 1 |
| 5.3 | 1 | 1 | 0 | 51 | 66 | 79 | Staged only; none after C | 8 |
| 5.3 | 2 | 1 | 44 (45 on another scoring) | 13 | 60 | 60 | Staged only; none after command 59 | 1 |

"Before C" is the last full result before C started. For 5.3 it adds up the last result of each staged A and B check; both counted runs moved on with A 1.4 (login) failing. The other four 5.3 runs started C with 0 A and B rules failing and ended with 1, 0, 0 and 46 broken.

Rules that failed in the final app but passed earlier in the run:

| Run | Repeat | Compared from | Rules newly failing in the final app | Fixed in the same period |
| --- | --- | --- | --- | --- |
| 5.1 | 1 | Command 88, after C, 21 broken | 0 | 3 |
| 5.1 | 2 | Before C, 71 broken, never rerun in full | Cannot be measured | Cannot be measured |
| 5.2 | 1 | Command 55, before C, 24 broken | 3 (B 5.2, 5.10, 5.11) | 8 |
| 5.2 | 2 | End of the run, 11 broken (no full list from before C) | 24 (live app against a fresh boot) | 0 |
| 5.3 | 1 | Staged checks before C, 1 failing | 0 | 1 |
| 5.3 | 2 | Staged checks before C, 1 failing | 43 (archive routes return 404; a node killed by A 0.3 never comes back) | Not applicable |
| 5.3 | Fixed run 4 (not counted) | All-checks at command 71: A 50, B 66 | 45 (every message post returns 500 after the E file-attachment change) | 0 |

#### Answer

a) Yes, sometimes badly, though most A and B failures in 5.1 and 5.2 were never working: the agent started C with 24 to 71 rules broken despite the README telling it to reach `0 broken` first. Where A and B were clean first, breakage was 0 rules in 5.1 r1, 3 in 5.2 r1 and 0 in 5.3 r1, but 43 in 5.3 r2 and 45 in uncounted run 4. In both large cases one late change broke a whole area (archiving and node restart, message posting), and the agent never reran an A or B check afterwards. 5.2 r2 is unclear, because I can only compare the live app with a fresh boot. The old 5.3 r2 shows C, D and E can be built with no breakage (215 of 215).

b) Having the specs made little difference, because the agent barely read them: once per repeat in 5.2. Final results overlap (18 and 71 broken in 5.1, 19 and 35 in 5.2). 5.2 did start C later and with fewer broken (24 and 29, against 54 and 71), but 5.1 r2's 71 mostly reflects the agent giving up on the suite after a timeout. In 5.3, where the README presents the specs step by step, the agent read them 8 times in 5 of 6 runs, so how specs are presented seems to matter more than whether they exist.

c) Yes, for building A and B, but not for keeping them. With staged checks every 5.3 run started C with 0 or 1 rules failing; with the single suite (5.2) the agent moved on with 24 and 29. Each staged check is a small target, while the suite reports up to 90 failures at once and can pass the 30 s limit with no output. The final apps are mixed: 0 and 44 broken in the counted 5.3 runs, against 19 and 35 in 5.2. Staged checks run once per step, and nothing makes the agent rerun A and B before the submit list, which capped runs never reach. Staged checks while building plus a short A and B recheck after each later component would probably work best; I did not test that.

#### Consistency across repeats

- **4a:** not consistent. Of the 4 counted runs I can measure, three broke 0 to 3 rules and one broke 43.
- **4b:** not consistent. 5.1 r1 and 5.2 r1 end almost equal (18 and 19); 5.1 r2 is far worse.
- **4c:** consistent before C (all six 5.3 runs at 0 or 1, against 24 and 29 in 5.2); not consistent in the final app (5.3 r1 beats both 5.2 repeats, 5.3 r2 is worse than both).

Across six 5.3 runs on almost the same spec, results range from not booting to 215 of 215, so two repeats cannot size these effects precisely.

#### Other evidence

- **Giving up on the suite (5.1 r2):** at command 59 the suite timed out with no output. The agent noted it had "exceeded the harness's 30-second limit" and never ran it again.
- **Misleading low counts (5.1 r1):** at command 79 the suite reported `5 broken`, but 4 were sections that did not run; the full run soon after showed `29 broken`.
- **Ignored submit rule (5.2 r2):** the agent saw `11 broken` and submitted anyway, calling the rest "deeper channel event/read-state semantics".
- **Budget:** 5.1 r1, 5.2 r1 and all four fixed 5.3 runs hit the $3 cap with E unfinished. In the fixed 5.3 runs the browser step E.5 was run 16 to 27 times in three of the four runs.
- **Fresh boot matters:** 5.2 r2 passed D in the run but gets 3 of 14 on a fresh boot, and the old 5.3 r1's bad `requirements.txt` showed only on a fresh install.
