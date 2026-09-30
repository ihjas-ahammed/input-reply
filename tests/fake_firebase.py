"""In-memory stand-in for Firebase Auth and Realtime Database (REST + server-sent events)."""

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def resolve_server_values(value):
    if isinstance(value, dict):
        if value == {".sv": "timestamp"}:
            return int(time.time() * 1000)
        return {k: resolve_server_values(v) for k, v in value.items()}
    return value


class Tree:
    def __init__(self):
        self.root = {}
        self.lock = threading.Lock()
        self.watchers = []   # (watched path, queue)
        self.counter = 0

    @staticmethod
    def parts(path):
        return [p for p in path.strip("/").split("/") if p]

    def get(self, path):
        node = self.root
        for part in self.parts(path):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def _set(self, path, value):
        parts = self.parts(path)
        if not parts:
            self.root = value if isinstance(value, dict) else {}
            return
        node = self.root
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = node[part] = {}
            node = child
        if value is None:
            node.pop(parts[-1], None)
        else:
            node[parts[-1]] = value

    def write(self, path, value):
        with self.lock:
            self._set(path, resolve_server_values(value))
            self._notify(path)

    def patch(self, path, changes):
        with self.lock:
            for key, value in changes.items():
                target = path.rstrip("/") + "/" + key
                self._set(target, resolve_server_values(value))
                self._notify(target)

    def push(self, path, value):
        with self.lock:
            self.counter += 1
            key = f"-N{self.counter:08d}"
            target = path.rstrip("/") + "/" + key
            self._set(target, resolve_server_values(value))
            self._notify(target)
            return key

    def _notify(self, path):
        for watched, channel in self.watchers:
            watched = "/" + "/".join(self.parts(watched))
            changed = "/" + "/".join(self.parts(path))
            if changed == watched or changed.startswith(watched + "/"):
                relative = changed[len(watched):] or "/"
                channel.put({"path": relative, "data": self.get(path)})

    def watch(self, path):
        channel = queue.Queue()
        with self.lock:
            self.watchers.append((path, channel))
            initial = {"path": "/", "data": self.get(path)}
        return channel, initial

    def unwatch(self, channel):
        with self.lock:
            self.watchers = [w for w in self.watchers if w[1] is not channel]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_POST(self):
        parsed = urlsplit(self.path)
        raw = self.read_body()
        server = self.server
        if parsed.path.endswith(("accounts:signUp", "accounts:signInWithPassword")):
            body = json.loads(raw)
            email = body["email"]
            if parsed.path.endswith("signUp"):
                if email in server.users:
                    return self.send_json(400, {"error": {"message": "EMAIL_EXISTS"}})
                server.users[email] = (body["password"], f"uid{len(server.users) + 1}")
            elif email not in server.users or server.users[email][0] != body["password"]:
                return self.send_json(400, {"error": {"message": "INVALID_LOGIN_CREDENTIALS"}})
            uid = server.users[email][1]
            return self.send_json(200, {"localId": uid, "email": email, "idToken": f"tok-{uid}",
                                        "refreshToken": f"refresh-{uid}", "expiresIn": "3600"})
        if parsed.path.endswith("/token"):
            refresh = parse_qs(raw.decode())["refresh_token"][0]
            if not refresh.startswith("refresh-"):
                return self.send_json(400, {"error": {"message": "INVALID_REFRESH_TOKEN"}})
            uid = refresh[len("refresh-"):]
            return self.send_json(200, {"user_id": uid, "id_token": f"tok-{uid}",
                                        "refresh_token": refresh, "expires_in": "3600"})
        path = self.database_path(parsed)
        if path is None:
            return
        self.send_json(200, {"name": server.tree.push(path, json.loads(raw))})

    def database_path(self, parsed):
        token = parse_qs(parsed.query).get("auth", [""])[0]
        if not token.startswith("tok-"):
            self.send_json(401, {"error": "Permission denied"})
            return None
        path = parsed.path[:-len(".json")] if parsed.path.endswith(".json") else parsed.path
        uid = token[len("tok-"):]
        parts = Tree.parts(path)
        # Mirror the rules: only the owner can touch inputReply/users/<uid>.
        if len(parts) >= 3 and parts[:2] == ["inputReply", "users"] and parts[2] != uid:
            self.send_json(401, {"error": "Permission denied"})
            return None
        return path

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = self.database_path(parsed)
        if path is None:
            return
        if "text/event-stream" not in self.headers.get("Accept", ""):
            return self.send_json(200, self.server.tree.get(path))
        channel, initial = self.server.tree.watch(path)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            self.emit("put", initial)
            while not self.server.closing.is_set():
                try:
                    self.emit("put", channel.get(timeout=0.2))
                except queue.Empty:
                    self.wfile.write(b"event: keep-alive\ndata: null\n\n")
                    self.wfile.flush()
        except OSError:
            pass
        finally:
            self.server.tree.unwatch(channel)

    def emit(self, event, payload):
        self.wfile.write(f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode())
        self.wfile.flush()

    def do_PUT(self):
        path = self.database_path(urlsplit(self.path))
        if path is not None:
            value = json.loads(self.read_body())
            self.server.tree.write(path, value)
            self.send_json(200, value)

    def do_PATCH(self):
        path = self.database_path(urlsplit(self.path))
        if path is not None:
            value = json.loads(self.read_body())
            self.server.tree.patch(path, value)
            self.send_json(200, value)

    def do_DELETE(self):
        path = self.database_path(urlsplit(self.path))
        if path is not None:
            self.server.tree.write(path, None)
            self.send_json(200, None)


class FakeFirebase:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.tree = Tree()
        self.httpd.users = {}
        self.httpd.closing = threading.Event()
        self.tree = self.httpd.tree
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def config(self):
        return {"apiKey": "test-key", "databaseURL": self.url,
                "identityUrl": self.url + "/v1", "secureTokenUrl": self.url + "/v1"}

    def close(self):
        self.httpd.closing.set()
        self.httpd.shutdown()
        self.httpd.server_close()
