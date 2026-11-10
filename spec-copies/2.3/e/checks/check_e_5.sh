#!/usr/bin/env bash
# Self-check for E.5 (web UI accounts, channels and layout). Exit 0 only if every rule passes.
# Usage: bash check_e_5.sh
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_e_5() {
  run_py 8 5.0 <<'PY'
from e_lib import *

W = World()
USER, WS_NAME, SLUG = f"u{RUN}_ui", f"Team {RUN}", f"e5-{RUN}"
try:
    P = ui(5)
    P.goto("http://127.0.0.1:8000/")
    P.locator(T("auth-form")).wait_for(state="visible")
except Exception as e:
    W.error = f"auth-form is not visible at GET /: {str(e).splitlines()[0]}"

def see(name, timeout=2000):
    P.locator(T(name)).first.wait_for(state="visible", timeout=timeout)
    return P.locator(T(name)).first
def hidden(name):
    P.locator(T(name)).first.wait_for(state="hidden")
def form(name, field, value):
    P.locator(f'{T(name)} [name="{field}"]').fill(value)
def token():
    return P.evaluate("localStorage.getItem('huddle.token')")
def entry(name):
    return P.locator(f'{T("channel-list")} [data-channel-name="{name}"]')
def create_channel(name):
    P.locator(T("new-channel-btn")).click()
    see("create-channel-form")
    form("create-channel-form", "name", name)
    P.locator(T("create-channel-submit")).click()

def r1():
    for name in ("logout-btn", "message-input"):
        assert not P.locator(T(name)).is_visible(), f"{name} is visible while logged out"
    P.locator(T("auth-toggle")).click()
    form("auth-form", "username", USER); form("auth-form", "password", "x")
    P.locator(T("auth-submit")).click()
    assert see("auth-error").inner_text().strip(), "auth-error is empty"
    assert not token(), "a failed sign-up stored a token"
def r2():
    form("auth-form", "password", PW)
    P.locator(T("auth-submit")).click()
    hidden("auth-form")
    assert token(), "huddle.token is not in localStorage"
    assert USER in see("current-user").inner_text(), "current-user does not show the username"
def r3():
    see("empty-state-create-workspace")
    form("workspace-create-form", "slug", SLUG); form("workspace-create-form", "name", WS_NAME)
    P.locator(f'{T("workspace-create-form")} [type="submit"]').click()
    hidden("empty-state-create-workspace")
    assert WS_NAME in see("workspace-header").inner_text(), "workspace-header does not show the name"
def r4():
    P.locator(T("logout-btn")).click()
    see("auth-form")
    assert not token(), "logout left huddle.token in localStorage"
    form("auth-form", "username", USER); form("auth-form", "password", PW)
    P.locator(T("auth-submit")).click()
    hidden("auth-form")
    assert WS_NAME in see("workspace-header").inner_text(), "after login, workspace-header does not show the same workspace"
def r5():
    create_channel("alpha")
    entry("alpha").wait_for(state="visible")
    hidden("create-channel-form")
    create_channel("beta")
    entry("beta").wait_for(state="visible")
    hidden("create-channel-form")
    e = entry("alpha")
    assert e.get_attribute("data-channel-id"), "the entry has no data-channel-id"
    assert e.inner_text().strip().startswith("#"), f"the entry text is {e.inner_text()!r}"
    n = P.locator(f'{T("channel-list")} {T("channel-entry")}').count()
    assert n >= 2, f"channel-list has {n} channel-entry elements, want at least 2"
    s, _ = call("PATCH", f"/api/channels/{e.get_attribute('data-channel-id')}", {"topic": "the topic"}, token())
    want(s, 200, "setup: set the topic")
    P.reload()
    see("workspace-header")
    entry("beta").click()
    P.wait_for_function("s => document.querySelector(s).innerText.includes('beta')", arg=T("channel-title"))
    entry("alpha").click()
    P.wait_for_function("s => document.querySelector(s).innerText.includes('alpha')", arg=T("channel-title"))
    assert "the topic" in see("channel-topic").inner_text(), "channel-topic does not show the topic"
def r6():
    before = P.locator(f'{T("channel-list")} [data-channel-name]').count()
    create_channel("alpha")
    assert see("create-channel-error").inner_text().strip(), "create-channel-error is empty"
    assert P.locator(f'{T("channel-list")} [data-channel-name]').count() == before, "a duplicate name added an entry"
    P.locator(T("create-channel-cancel")).click()
def r7():
    entry("beta").click()
    P.wait_for_function("s => document.querySelector(s).innerText.includes('beta')", arg=T("channel-title"))
    assert see("message-list").inner_text().strip(), "message-list is blank in an empty channel"
def r8():
    side = P.evaluate("""s => { let e = document.querySelector(s);
      while (e && e !== document.body) { const c = getComputedStyle(e).backgroundColor;
        if (c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') return c; e = e.parentElement; }
      return null; }""", T("channel-list"))
    assert side and side != "rgb(255, 255, 255)", f"the sidebar background is {side}"
    a, h, m = see("channel-list").bounding_box(), see("workspace-header").bounding_box(), see("message-list").bounding_box()
    assert h["x"] + h["width"] <= m["x"] + 1 and a["x"] + a["width"] <= m["x"] + 1, f"channel-list {a}, workspace-header {h}, message-list {m}: the sidebar must be left of message-list"
    for name in ("thread-panel", "channel-settings-close", "workspace-settings-close"):
        assert not P.locator(T(name)).is_visible(), f"{name} is visible with nothing open"
    i = see("message-input").bounding_box()
    assert i["y"] + i["height"] >= 900 - 120, f"message-input ends at y={i['y'] + i['height']:.0f}, want within 120 pixels of the bottom (900)"

run_rules(W, [
    ("5.1", "an invalid sign-up shows a visible error", r1),
    ("5.2", "a valid sign-up logs in", r2),
    ("5.3", "the empty state creates a workspace", r3),
    ("5.4", "logout then login restores the same workspace", r4),
    ("5.5", "channels are created, listed and switched", r5),
    ("5.6", "a duplicate channel name shows an error", r6),
    ("5.7", "an empty channel shows non-blank text", r7),
    ("5.8", "the layout has a coloured sidebar and no open thread panel", r8),
])
PY
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_e_5; finish; }
