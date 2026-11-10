#!/usr/bin/env bash
# Self-check for E.6 (web UI messages, threads and reactions). Exit 0 only if every rule passes.
# Usage: bash check_e_6.sh
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_e_6() {
  run_py 6 6.0 <<'PY'
from e_lib import *

W = world(6)
if not W.error:
    try:
        P = ui(6)
        P.add_init_script(f"if (!sessionStorage.getItem('chk')) {{ sessionStorage.setItem('chk', '1'); localStorage.setItem('huddle.token', {json.dumps(W.O)}); }}")
        P.goto("http://127.0.0.1:8000/")
        P.locator(f'[data-channel-name="{W.CH_NAME}"]').click()
        P.locator(T("message-input")).wait_for(state="visible")
    except Exception as e:
        W.error = f"could not open the channel with huddle.token set: {str(e).splitlines()[0]}"

ROWS = f'{T("message-list")} {T("message")}'
def rows():
    return P.locator(ROWS)
def row(text):
    return rows().filter(has_text=text)
def see(loc, what):
    try: loc.first.wait_for(state="visible")
    except Exception: raise AssertionError(f"{what} is not visible within 2s")
    return loc.first
def send(text):
    P.locator(T("message-input")).fill(text)
    P.locator(T("send-btn")).click()
    see(row(text), f"the row for {text!r}")
    return row(text).first
def reload():
    P.reload()
    P.locator(f'[data-channel-name="{W.CH_NAME}"]').click()
    P.locator(T("message-input")).wait_for(state="visible")
    P.wait_for_timeout(200)
def no_dialogs():
    assert not P.dialogs, f"a {P.dialogs[0]} dialog opened"

def r1():
    r = send("first post")
    see(r.locator(T("message-avatar")), "message-avatar")
    assert W.O_NAME in see(r.locator(T("message-author")), "message-author").inner_text(), "message-author does not show the author"
    t = see(r.locator(T("message-time")), "message-time").inner_text()
    assert re.search(r"\d{1,2}:\d{2}", t) and "T" not in t.replace("PT", "").replace("GMT", "").replace("UTC", ""), f"message-time is {t!r}, want a time such as 3:07 PM"
    assert "first post" in see(r.locator(T("message-body")), "message-body").inner_text(), "message-body does not show the text"
    reload()
    see(row("first post"), "the row after a reload")
def r2():
    n = rows().count()
    for text in ("", "   "):
        P.locator(T("message-input")).fill(text)
        P.locator(T("send-btn")).click()
        P.wait_for_timeout(300)
        assert rows().count() == n, f"sending {text!r} added a row"
    s, b = call("GET", f"/api/channels/{W.CH}/messages?limit=100", None, W.O)
    assert len((b or {}).get("messages", [])) == n, "a blank message was saved"
def r3():
    r = send("before edit")
    P.mouse.move(0, 0)
    assert not r.locator(T("message-edit-btn")).is_visible(), "message-edit-btn is visible with no hover"
    r.hover()
    see(r.locator(T("message-edit-btn")), "message-edit-btn on hover").click()
    i = see(P.locator(T("message-edit-input")), "message-edit-input")
    i.fill("after edit")
    i.press("Enter")
    r2 = row("after edit")
    see(r2, "the edited row")
    assert "edited" in r2.first.inner_text().lower(), "the row does not show 'edited'"
    reload()
    assert "edited" in see(row("after edit"), "the edited row after a reload").inner_text().lower(), "'edited' is gone after a reload"
    no_dialogs()
def r4():
    r = send("to be deleted")
    r.hover()
    see(r.locator(T("message-delete-btn")), "message-delete-btn on hover").click()
    row("to be deleted").first.wait_for(state="detached")
    reload()
    assert row("to be deleted").count() == 0, "the row came back after a reload"
    no_dialogs()
def r5():
    r = send("thread parent")
    r.hover()
    see(r.locator(T("open-thread-btn")), "open-thread-btn on hover").click()
    panel = see(P.locator(T("thread-panel")), "thread-panel")
    a, b = panel.bounding_box(), P.locator(T("message-list")).bounding_box()
    assert a["x"] >= b["x"] + b["width"] - 1, f"thread-panel at x={a['x']} is not right of message-list, which ends at {b['x'] + b['width']}"
    P.locator(T("thread-input")).fill("a reply")
    P.locator(T("thread-send")).click()
    see(panel.get_by_text("a reply"), "the reply in thread-panel")
    P.wait_for_function("s => [...document.querySelectorAll(s)].some(e => /\\b1 repl(y|ies)\\b/i.test(e.innerText))", arg=ROWS)
    P.locator(T("close-thread")).click()
    P.locator(T("thread-panel")).wait_for(state="hidden")
def r6():
    r = send("react here")
    r.hover()
    see(r.locator(T("reaction-button")), "reaction-button on hover").click()
    pick = see(P.locator(T("emoji-picker")), "emoji-picker")
    d = pick.evaluate("e => getComputedStyle(e).display")
    assert d == "grid", f"emoji-picker has display {d}, want grid"
    n = P.locator(T("emoji-option")).count()
    assert n >= 2, f"emoji-picker has {n} emoji-option elements"
    P.locator(T("emoji-option")).first.click()
    chip = see(r.locator(T("reaction-chip")), "reaction-chip")
    assert re.search(r"\b1\b", chip.inner_text()), f"reaction-chip shows {chip.inner_text()!r}"
    no_dialogs()

run_rules(W, [
    ("6.1", "a posted message shows its parts and survives a reload", r1),
    ("6.2", "an empty message is not posted", r2),
    ("6.3", "edit from the hover toolbar shows 'edited' and survives a reload", r3),
    ("6.4", "delete from the hover toolbar survives a reload", r4),
    ("6.5", "threads open on the right and bump the reply count", r5),
    ("6.6", "reactions use an emoji grid and show a chip with a count", r6),
])
PY
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_e_6; finish; }
