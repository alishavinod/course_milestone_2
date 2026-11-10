"""Helpers for the WebSocket parts of the staged channels checks. Imported by check_channels_3.sh and _4.sh."""
import base64, json, os, socket, struct, time, urllib.request, urllib.error

E = os.environ
RUN, SLUG, O, B = E["RUN"], E["WS"], E["O_T"], E["B_T"]


def call(method, path, body=None, token=None, port=8000):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    if data is not None: req.add_header("Content-Type", "application/json")
    if token: req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r: return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e: return e.code, None
    except Exception: return 0, None


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
            if not chunk: raise AssertionError(f"port {port}: connection closed during the handshake")
            buf += chunk
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        status = head.split(b" ")[1].decode()
        assert status == "101", f"port {port}: handshake answered {status}, want 101"

    def send(self, obj):
        data, mask = json.dumps(obj).encode(), os.urandom(4)
        n = len(data)
        hdr = bytes([0x81, 0x80 | n]) if n < 126 else bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk: raise AssertionError("server closed the WebSocket")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def frames(self, timeout=3):
        """Yields JSON frames until `timeout` seconds pass with nothing new."""
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
            elif b1 & 0x0F == 8: raise AssertionError("server closed the WebSocket")

    def first(self, pred, timeout=3):
        return next((f for f in self.frames(timeout) if pred(f)), None)

    def events(self, cid, count, timeout=3):
        got = []
        for f in self.frames(timeout):
            if str(f.get("channel_id")) == str(cid) and isinstance(f.get("event_id"), int):
                got.append((f["event_id"], f.get("type")))
                if len(got) == count: break
        return got


def is_sub(frame):
    return frame.get("type") == "subscribed"


def rule(rid, desc, fn):
    try:
        fn(); print(f"PASS {rid} {desc}")
    except AssertionError as e:
        print(f"FAIL {rid} {desc}: {e}")
    except KeyError:
        print(f"FAIL {rid} {desc}: skipped, an earlier rule in this section failed")
    except Exception as e:
        print(f"FAIL {rid} {desc}: {type(e).__name__}: {e}")
