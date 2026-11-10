#!/usr/bin/env bash
# Self-check for E.4 (web UI page and pinned hooks). Exit 0 only if every rule passes.
# Usage: bash check_e_4.sh
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_e_4() {
  run_py 5 4.0 <<'PY'
from e_lib import *
from html.parser import HTMLParser

W = World()
TESTIDS = """auth-form auth-submit auth-toggle workspace-create-form empty-state-create-workspace
join-workspace-form join-workspace-submit channel-list dms-list new-channel-btn current-user logout-btn
workspace-header workspace-settings-btn workspace-settings-close workspace-name-input workspace-join-mode
workspace-general-submit channel-title channel-topic channel-settings-btn channel-settings-title
channel-settings-close channel-members-list channel-add-member-form channel-add-member-input
channel-add-member-submit channel-topic-input channel-topic-submit archive-channel-btn unarchive-channel-btn
create-channel-form create-channel-private create-channel-submit create-channel-cancel message-list
message-input send-btn thread-panel thread-input thread-send close-thread settings-tab-members
settings-tab-invitations settings-tab-general settings-pane-members settings-pane-invitations
settings-pane-general create-invitation-btn create-invitation-form create-invitation-submit
invitations-list""".split()
IDS = "auth-modal create-channel-modal channel-settings-modal workspace-settings-modal create-invitation-form".split()
ROLES = {
    "primary": "auth-submit send-btn thread-send create-channel-submit join-workspace-submit workspace-general-submit create-invitation-submit channel-topic-submit channel-add-member-submit".split(),
    "danger": "archive-channel-btn logout-btn".split(),
    "secondary": "new-channel-btn create-invitation-btn unarchive-channel-btn create-channel-cancel close-thread channel-settings-close workspace-settings-close channel-settings-btn workspace-settings-btn".split(),
}
COLOURS = {"primary": "rgb(0, 122, 90)", "danger": "rgb(224, 30, 90)", "secondary": "rgb(18, 100, 163)"}

class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.testids, self.ids = {}, set()
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a: self.ids.add(a["id"])
        if "data-testid" in a: self.testids[a["data-testid"]] = a

def served(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode(errors="replace")
    except urllib.error.HTTPError as e: return e.code, "", ""
    except Exception: return 0, "", ""

def short(names):
    return f"{len(names)} missing, such as {', '.join(names[:5])}"
def r1():
    for p in PORTS:
        s, ct, _ = served(p)
        assert s == 200 and "html" in ct, f"GET / on {p}: status {s}, Content-Type {ct!r}"
def parsed():
    h = Page(); h.feed(served(8000)[2]); return h
def r2():
    missing = [t for t in TESTIDS if t not in parsed().testids]
    assert not missing, short(missing)
def r3():
    missing = [i for i in IDS if i not in parsed().ids]
    assert not missing, short(missing)
def r4():
    h = parsed()
    wrong = [t for role, ts in ROLES.items() for t in ts if h.testids.get(t, {}).get("data-button-role") != role]
    assert not wrong, f"wrong or no data-button-role: {len(wrong)}, such as {', '.join(wrong[:5])}"
def r5():
    page = ui(4)
    page.goto("http://127.0.0.1:8000/")
    page.mouse.move(0, 0)
    wrong = []
    for role, ts in ROLES.items():
        for t in ts:
            got = page.evaluate("""s => { const e = document.querySelector(s); if (!e) return null;
              const c = getComputedStyle(e); return [c.backgroundColor, c.color, c.backgroundImage]; }""", T(t))
            if got != [COLOURS[role], "rgb(255, 255, 255)", "none"]: wrong.append(f"{t} is {got}")
    assert not wrong, f"{len(wrong)} wrong, such as {wrong[0]}"

run_rules(W, [
    ("4.1", "GET / returns HTML on all three nodes", r1),
    ("4.2", "the served HTML has every pinned data-testid", r2),
    ("4.3", "the served HTML has every pinned id", r3),
    ("4.4", "the pinned buttons have the right data-button-role", r4),
    ("4.5", "the pinned buttons have the exact colours", r5),
])
PY
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_e_4; finish; }
