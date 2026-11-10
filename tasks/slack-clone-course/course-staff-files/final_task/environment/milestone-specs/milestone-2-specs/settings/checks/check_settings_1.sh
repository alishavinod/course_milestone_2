#!/usr/bin/env bash
# Self-check for Settings.1 (settings screens and their hooks). Exit 0 only if every rule passes.
# Usage: bash check_settings_1.sh
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_settings_1() {
  run_py 6 1.0 <<'PY'
from settings_lib import *

W = world(7)
if not W.error:
    try:
        P = ui(7)
        P.add_init_script(f"if (!sessionStorage.getItem('chk')) {{ sessionStorage.setItem('chk', '1'); localStorage.setItem('huddle.token', {json.dumps(W.O)}); }}")
        P.goto("http://127.0.0.1:8000/")
        P.locator(f'[data-channel-name="{W.CH_NAME}"]').click()
        P.locator(T("message-input")).wait_for(state="visible")
        NEW = f"u{RUN}_e7n"
        s, b = call("POST", "/api/auth/register", {"username": NEW, "password": PW})
        want(s, 201, "setup: register")
        NT = b["token"]
        ctx = P.context.browser.new_context(viewport={"width": 1280, "height": 900})
        ctx.set_default_timeout(2000)
        N = ctx.new_page()
        N.add_init_script(f"localStorage.setItem('huddle.token', {json.dumps(NT)})")
        N.goto("http://127.0.0.1:8000/")
    except Exception as e:
        W.error = f"could not open the page with huddle.token set: {str(e).splitlines()[0]}"

def see(loc, what):
    try: loc.first.wait_for(state="visible")
    except Exception: raise AssertionError(f"{what} is not visible within 2s")
    return loc.first
def poll(fn, what, secs=2):
    end = time.time() + secs
    while not fn():
        assert time.time() < end, what
        time.sleep(0.05)
def roles():
    s, b = call("GET", f"/api/workspaces/{W.SLUG}/members", None, W.O)
    want(s, 200, "GET members")
    return b["members"]
def row(name):
    return P.locator(T("member-row")).filter(has_text=name)
def join(code):
    see(N.locator(T("join-workspace-form")), "join-workspace-form")
    see(N.locator(f'{T("join-workspace-form")} input[name="code"]'), 'the join field with name="code"').fill(code)
    N.locator(T("join-workspace-submit")).click()

def r1():
    join("nosuchcode")
    e = see(N.locator(T("join-workspace-error")), "join-workspace-error")
    assert e.inner_text().strip(), "join-workspace-error is empty"
def r2():
    s, b = call("POST", f"/api/workspaces/{W.SLUG}/invitations", {}, W.O)
    want(s, 201, "setup: create invitation")
    join(b["invitation"]["code"])
    poll(lambda: "Check E" in N.locator(T("workspace-header")).inner_text(), "workspace-header does not show the workspace name after joining")
def r3():
    s, b = call("GET", f"/api/channels/{W.CH}/members", None, W.O)
    want(s, 200, "GET channel members")
    P.locator(T("channel-settings-btn")).click()
    see(P.locator("#channel-settings-modal"), "channel-settings-modal")
    see(P.locator(T("channel-member-row")), "channel-member-row")
    rows = P.locator(T("channel-member-row"))
    n = len(b["members"])
    assert rows.count() == n, f"{rows.count()} channel-member-row elements, want {n}"
    text = " ".join(rows.all_inner_texts())
    for name in (W.O_NAME, W.B_NAME, W.C_NAME):
        assert name in text, f"no channel-member-row shows {name}"
    P.locator(T("channel-settings-close")).click()
def r4():
    P.locator(T("workspace-settings-btn")).click()
    see(P.locator("#workspace-settings-modal"), "workspace-settings-modal")
    P.locator(T("settings-tab-members")).click()
    see(row(W.B_NAME), f"the member-row for {W.B_NAME}")
    n = len(roles())
    poll(lambda: P.locator(T("member-row")).count() == n, f"{P.locator(T('member-row')).count()} member-row elements, want {n}")
    for name, role in ((W.O_NAME, "owner"), (W.B_NAME, "member")):
        t = see(row(name).locator(T("role-badge")), f"role-badge for {name}").inner_text().lower()
        assert role in t, f"role-badge for {name} shows {t!r}, want {role}"
def r5():
    s, b = call("GET", "/api/auth/me", None, W.B)
    uid = b["user"]["id"]
    see(row(W.B_NAME).locator(T("role-select")), f"role-select for {W.B_NAME}").select_option("admin")
    poll(lambda: any(m["user_id"] == uid and m["role"] == "admin" for m in roles()), "the server still does not show the role as admin")
    poll(lambda: "admin" in row(W.B_NAME).locator(T("role-badge")).first.inner_text().lower(), "role-badge does not show admin")
def r6():
    P.locator(T("settings-tab-invitations")).click()
    sub = P.locator(T("create-invitation-submit"))
    if not sub.is_visible(): P.locator(T("create-invitation-btn")).click()
    see(sub, "create-invitation-submit").click()
    see(P.locator(T("invitation-code")), "invitation-code")
    s, b = call("GET", f"/api/workspaces/{W.SLUG}/invitations", None, W.O)
    want(s, 200, "GET invitations")
    codes = [i["code"] for i in b["invitations"]]
    shown = " ".join(P.locator(T("invitation-code")).all_inner_texts())
    hit = [c for c in codes if c in shown]
    assert hit, f"invitation-code shows {shown!r}, which is not an active code"
    s, _ = call("POST", f"/api/invitations/{hit[0]}/accept", {}, W.D)
    want(s, 200, "accepting the shown code")

run_rules(W, [
    ("1.1", "a failed join shows join-workspace-error", r1),
    ("1.2", "a valid code joins the workspace from the page", r2),
    ("1.3", "channel settings lists channel-member-row for each member", r3),
    ("1.4", "workspace settings lists member-row and role-badge", r4),
    ("1.5", "role-select changes a member's role", r5),
    ("1.6", "a new invitation shows a working invitation-code", r6),
])
PY
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_settings_1; finish; }
