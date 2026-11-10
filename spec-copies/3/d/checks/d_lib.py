"""Helpers for the D checks: HTTP calls, WebSocket subscribers, killing by port, Redis, rule running."""
import base64, json, os, re, signal, socket, struct, subprocess, threading, time, urllib.error, urllib.request

RUN = os.environ["RUN"]
PW = "correct-horse-1"
PORTS = (8000, 8001, 8002)


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

    def close(self):
        try: self.sock.close()
        except OSError: pass


class Sub(WS):
    """A subscriber that records its message.created frames in the background, with their arrival time."""
    def __init__(self, port, token, channel):
        super().__init__(port, token)
        self.port, self.got, self.alive = port, [], True
        self.send({"type": "subscribe", "channel_id": channel})
        f = next((f for f in self.frames(3) if f.get("type") == "subscribed"), None)
        assert f, f"port {port}: no 'subscribed' frame within 3s"
        self.head = f.get("head_event_id")
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        try:
            while True:
                for f in self.frames(30):
                    if f.get("type") == "message.created": self.got.append((time.time(), f))
        except Exception:
            self.alive = False

    def ids(self):
        return {str((f.get("message") or {}).get("id")) for _, f in list(self.got)}

    def wait(self, ids, deadline):
        """Waits until deadline (a time.time() value) for these message ids. Returns those still missing."""
        ids = {str(i) for i in ids}
        while ids - self.ids() and time.time() < deadline:
            time.sleep(0.02)
        return ids - self.ids()

    def check_dense(self):
        ev = sorted(f.get("event_id") for _, f in list(self.got))
        want_ev = list(range(self.head + 1, self.head + 1 + len(ev)))
        assert ev == want_ev, f"subscriber on {self.port}: event_id values {ev}, want {want_ev}"


def kill_port(port):
    """SIGKILLs whatever owns a listening port. Never selects a process by name."""
    out = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True).stdout
    pids = set(re.findall(r"pid=(\d+)", out))
    assert pids, f"nothing is listening on port {port}"
    for p in pids: os.kill(int(p), signal.SIGKILL)


def redis(cmd, timeout=1):
    """Sends one Redis command. Returns the simple or bulk string reply, or None if Redis does not answer."""
    try:
        s = socket.create_connection(("127.0.0.1", 6379), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(cmd.encode() + b"\r\n")
        buf = b""
        while True:
            buf += s.recv(65536)
            if buf[:1] != b"$":
                if buf.endswith(b"\r\n"): s.close(); return buf[1:-2].decode()
            elif b"\r\n" in buf:
                head, rest = buf.split(b"\r\n", 1)
                if len(rest) >= int(head[1:]): s.close(); return rest[:int(head[1:])].decode()
    except (OSError, ValueError):
        return None


def redis_subscribers():
    """How many HTTP nodes hold a Redis pub/sub subscription in their own process."""
    rows = (redis("CLIENT LIST") or "").splitlines()
    held = set()
    for r in rows:
        if any(int(x) > 0 for x in re.findall(r"\b(?:sub|psub|ssub)=(\d+)", r)):
            port = re.search(r"\baddr=\S+:(\d+)", r)
            if port:
                out = subprocess.run(["ss", "-tnpH", f"sport = :{port.group(1)}"], capture_output=True, text=True).stdout
                held |= set(re.findall(r"pid=(\d+)", out))
    nodes = 0
    for p in PORTS:
        out = subprocess.run(["ss", "-ltnpH", f"sport = :{p}"], capture_output=True, text=True).stdout
        nodes += bool(held & set(re.findall(r"pid=(\d+)", out)))
    return nodes


class World:
    """Users O (workspace owner) and B and C (in channel CH)."""
    error = None


def world(step):
    w = World()
    try:
        for tag in "OBC":
            name = f"u{RUN}_d{step}{tag.lower()}"
            s, b = call("POST", "/api/auth/register", {"username": name, "password": PW})
            assert s == 201, f"setup: register {name} got {s}"
            setattr(w, tag, b["token"])
        w.SLUG = f"d{step}-{RUN}"
        s, b = call("POST", "/api/workspaces", {"slug": w.SLUG, "name": "Check D"}, w.O)
        assert s == 201, f"setup: create workspace got {s}"
        s, b = call("POST", f"/api/workspaces/{w.SLUG}/channels", {"name": f"ch{step}-{RUN}"}, w.O)
        assert s == 201, f"setup: create channel got {s}"
        w.CH = b["channel"]["id"]
        for t in (w.B, w.C):
            s, _ = call("POST", f"/api/channels/{w.CH}/join", {}, t)
            assert s == 200, f"setup: join channel got {s}"
    except Exception as e:
        w.error = str(e) or type(e).__name__
    return w


def write(w, port, body=None):
    """Posts a message as O on a port. Returns its id, and fails the rule if it is not 201."""
    s, b = call("POST", f"/api/channels/{w.CH}/messages", {"body": body or f"m{os.urandom(4).hex()}"}, w.O, port)
    want(s, 201, f"write on {port}")
    return b["message"]["id"]


def listed(w, port, message_id):
    """True if a node lists this message in the channel."""
    s, b = call("GET", f"/api/channels/{w.CH}/messages?limit=100", None, w.B, port)
    want(s, 200, f"read on {port}")
    return str(message_id) in {str(m.get("id")) for m in (b or {}).get("messages", [])}


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
