"""Helpers for the C checks: HTTP calls, a tiny WebSocket client, test users and rule running."""
import base64, json, os, re, socket, struct, time, urllib.error, urllib.parse, urllib.request

RUN = os.environ["RUN"]
PW = "correct-horse-1"


def call(method, path, body=None, token=None, port=8000, timeout=5):
    """Returns (status, parsed JSON or None). Status 0 means no answer."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    if data is not None: req.add_header("Content-Type", "application/json")
    if token: req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.status, _json(r.read())
    except urllib.error.HTTPError as e: return e.code, _json(e.read())
    except Exception: return 0, None


def _json(raw):
    try: return json.loads(raw or b"null")
    except ValueError: return None


def raw_get(path, token):
    """Returns (status, headers, bytes) for a download."""
    req = urllib.request.Request(f"http://127.0.0.1:8000{path}")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r: return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e: return e.code, e.headers, b""
    except Exception: return 0, {}, b""


def upload(data, token, filename="note.txt", ctype="text/plain", field="file"):
    """POST /api/files as multipart/form-data. Returns (status, parsed JSON or None)."""
    b = "chk" + os.urandom(8).hex()
    payload = (f'--{b}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
               f"Content-Type: {ctype}\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request("http://127.0.0.1:8000/api/files", data=payload, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={b}")
    if token: req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r: return r.status, _json(r.read())
    except urllib.error.HTTPError as e: return e.code, _json(e.read())
    except Exception: return 0, None


def same(a, b):
    return str(a) == str(b)


def idset(items):
    return sorted(str(x) for x in (items or []))


def want(status, expected, what):
    assert status == expected, f"{what}: expected {expected}, got {status}"


class WS:
    """Tiny WebSocket client: connect, send JSON, receive JSON frames."""
    def __init__(self, port, token):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /api/ws?token={token} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            assert chunk, f"port {port}: WebSocket closed during the handshake"
            buf += chunk
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        status = head.split(b" ")[1].decode()
        assert status == "101", f"port {port}: WebSocket handshake answered {status}, want 101"

    def send(self, obj):
        data, mask = json.dumps(obj).encode(), os.urandom(4)
        self.sock.sendall(bytes([0x81, 0x80 | len(data)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            assert chunk, "the server closed the WebSocket"
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def frames(self, timeout=3):
        """Yields JSON frames until `timeout` seconds pass."""
        end = time.time() + timeout
        while time.time() < end:
            self.sock.settimeout(max(0.05, end - time.time()))
            try:
                b1, b2 = self._read(2)
            except socket.timeout:
                return
            n = b2 & 0x7F
            if n == 126: n = struct.unpack(">H", self._read(2))[0]
            elif n == 127: n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if b1 & 0x0F == 1: yield json.loads(data)
            elif b1 & 0x0F == 8: return

    def first(self, pred, timeout=3):
        return next((f for f in self.frames(timeout) if pred(f)), None)

    def close(self):
        try:
            self.sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))
            self.sock.close()
        except OSError:
            pass


def subscribed(port, token, channel):
    """Opens a WebSocket subscribed to a channel. Returns (ws, head_event_id)."""
    w = WS(port, token)
    w.send({"type": "subscribe", "channel_id": channel})
    f = w.first(lambda f: f.get("type") == "subscribed")
    assert f, "no 'subscribed' frame within 3s"
    return w, f.get("head_event_id")


class World:
    """Users O (workspace owner), B and C (in workspace and channel CH), and N (in neither)."""
    error = None


def new_user(tag):
    name = f"u{RUN}_{tag}"
    s, b = call("POST", "/api/auth/register", {"username": name, "password": PW})
    assert s == 201, f"setup: register {name} got {s}"
    return b["token"], b["user"]["id"], name


def world(step):
    w = World()
    try:
        for tag in "OBCN":
            t, i, n = new_user(f"{step}{tag.lower()}")
            setattr(w, tag, t); setattr(w, tag + "_ID", i); setattr(w, tag + "_NAME", n)
        w.SLUG = f"c{step}-{RUN}"
        s, b = call("POST", "/api/workspaces", {"slug": w.SLUG, "name": "Check C"}, w.O)
        assert s == 201, f"setup: create workspace got {s}"
        w.CH = channel(w, f"ch{step}-{RUN}")
        for t in (w.B, w.C):
            s, _ = call("POST", f"/api/channels/{w.CH}/join", {}, t)
            assert s == 200, f"setup: join channel got {s}"
    except Exception as e:
        w.error = str(e) or type(e).__name__
    return w


def channel(w, name):
    s, b = call("POST", f"/api/workspaces/{w.SLUG}/channels", {"name": name}, w.O)
    assert s == 201, f"setup: create channel {name} got {s}"
    return b["channel"]["id"]


def post(w, body, token=None, channel_id=None, **extra):
    """Posts a message. Returns (status, parsed JSON)."""
    return call("POST", f"/api/channels/{channel_id or w.CH}/messages", {"body": body, **extra}, token or w.O)


def message(w, body, token=None, channel_id=None):
    s, b = post(w, body, token, channel_id)
    assert s == 201, f"setup: post message got {s}"
    return b["message"]["id"]


def run_rules(w, rules):
    """Prints one PASS or FAIL line per rule, so the check always reports every rule."""
    for rid, desc, fn in rules:
        if w.error:
            print(f"FAIL {rid} {desc}: {w.error}")
            continue
        try:
            fn()
            print(f"PASS {rid} {desc}")
        except AssertionError as e:
            print(f"FAIL {rid} {desc}: {e}")
        except Exception as e:
            print(f"FAIL {rid} {desc}: {type(e).__name__}: {e}")
