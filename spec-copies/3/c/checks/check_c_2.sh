#!/usr/bin/env bash
# Self-check for C.2 (files). Exit 0 only if every rule passes.
# Usage: bash check_c_2.sh
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_c_2() {
  run_py 7 2.0 <<'PY'
from c_lib import *

W = world(2)
DATA = bytes(range(256)) * 4 + b"end"
TS = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|\+00:00)$"

def r1():
    s, b = upload(DATA, W.B, "pic.png", "image/png")
    want(s, 201, "upload")
    f = (b or {}).get("file") or {}
    missing = [k for k in ("id", "uploader_id", "filename", "content_type", "size", "created_at") if k not in f]
    assert not missing, f"file is missing {missing}"
    W.F = f["id"]
    assert same(f["uploader_id"], W.B_ID), f"uploader_id {f['uploader_id']}, want {W.B_ID}"
    assert (f["filename"], f["content_type"], f["size"]) == ("pic.png", "image/png", len(DATA)), \
        f"filename, content_type, size are {(f['filename'], f['content_type'], f['size'])}, want {('pic.png', 'image/png', len(DATA))}"
    assert re.match(TS, str(f["created_at"])), f"created_at {f['created_at']!r} is not a UTC timestamp"
def r2():
    s, b = call("GET", f"/api/files/{W.F}", None, W.B)
    assert s == 200 and same(((b or {}).get("file") or {}).get("id"), W.F), f"GET file: status {s}, body {str(b)[:100]}"
    want(call("GET", "/api/files/999999999", None, W.B)[0], 404, "unknown file")
def r3():
    s, h, data = raw_get(f"/api/files/{W.F}/download", W.B)
    want(s, 200, "download")
    assert data == DATA, f"download returned {len(data)} bytes that do not match the {len(DATA)} uploaded"
    ct, cd = h.get("Content-Type", ""), h.get("Content-Disposition", "")
    assert ct.startswith("image/png"), f"Content-Type is {ct!r}, want image/png"
    assert "pic.png" in cd, f"Content-Disposition is {cd!r}, want it to contain the filename"
def r4():
    limit = 10 * 1024 * 1024
    want(upload(b"x" * limit, W.B, "big.bin", "application/octet-stream")[0], 201, "a file of exactly 10 MiB")
    want(upload(b"x" * (limit + 1), W.B, "big2.bin", "application/octet-stream")[0], 413, "a file one byte over 10 MiB")
def r5():
    a = upload(b"x", W.B, field="other")[0]
    c = upload(b"x", None)[0]
    assert (a, c) == (400, 401), f"no file part {a} (want 400), no token {c} (want 401)"
def r6():
    s, b = upload(b"attach me", W.B)
    want(s, 201, "upload")
    W.A = b["file"]["id"]
    s, b = post(W, "with a file", W.B, file_ids=[W.A])
    want(s, 201, "post with file_ids")
    fs = b["message"].get("files") or []
    assert len(fs) == 1 and same(fs[0].get("id"), W.A) and fs[0].get("filename"), f"message files are {fs}, want the FileObj"
def r7():
    theirs = upload(b"not yours", W.C)[1]["file"]["id"]
    a = post(W, "x", W.B, file_ids=[theirs])[0]
    c = post(W, "x", W.B, file_ids=[W.A])[0]
    assert (a, c) == (400, 400), f"another user's file {a}, already attached {c} (want 400 each)"

run_rules(W, [
    ("2.1", "upload returns 201 with every FileObj field", r1),
    ("2.2", "GET file returns 200, unknown 404", r2),
    ("2.3", "download returns the raw bytes and the headers", r3),
    ("2.4", "exactly 10 MiB is 201, one byte more is 413", r4),
    ("2.5", "no file part is 400, no token is 401", r5),
    ("2.6", "file_ids attaches files to the message", r6),
    ("2.7", "another user's or an already attached file returns 400", r7),
])
PY
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_c_2; finish; }
