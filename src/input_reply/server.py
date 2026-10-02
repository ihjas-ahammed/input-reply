"""Authenticated local/network web API for Input Reply."""

from __future__ import annotations

import hmac
import json
import os
import secrets
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

import re

from . import ai, autostart, core, settings
from .actions import (STARTS_JOB, MacroState, check_backend, dispatch, mapping_for,  # noqa: F401 (re-exported)
                      record_once, replay_once, seconds, wait_countdown)
from .backends import select_backend
from .firebase import CloudError

LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

GET_ACTIONS = {"/api/status": "status", "/api/recordings": "recordings", "/api/windows": "windows"}
POST_ACTIONS = {"/api/record": "record", "/api/replay": "replay", "/api/stop": "stop",
                "/api/delete": "delete", "/api/parameter/add": "parameter_add",
                "/api/parameter/remove": "parameter_remove",
                "/api/script": "script"}


def token_path():
    return core.data_dir() / "access-token"


def access_token():
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(descriptor, "w", encoding="utf-8") as out:
        out.write(token + "\n")
    return token


@lru_cache(maxsize=1)
def page() -> bytes:
    return files("input_reply").joinpath("web.html").read_bytes()


class Handler(BaseHTTPRequestHandler):
    server_version = "InputReply"
    protocol_version = "HTTP/1.1"   # keep-alive: the page polls, so avoid a new connection each time
    timeout = 30

    def log_message(self, format, *args):
        # Avoid logging access codes, titles, and supplied parameter values.
        pass

    def reply(self, status, data, content_type="application/json; charset=utf-8"):
        body = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self'")
        if status >= 400:
            # An unread request body would corrupt the next request on this connection.
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied, self.server.access_token):
            self.reply(401, {"error": "Enter the access code shown by input-reply token"})
            return False
        return True

    def body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid request size") from error
        if not 0 <= length <= 65536:
            raise ValueError("Request is too large")
        value = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("Invalid request")
        return value

    @property
    def assistant(self):
        assistant = getattr(self.server, "assistant", None)
        if assistant is None:
            raise RuntimeError("The AI assistant is not available in this server")
        return assistant

    @property
    def cloud(self):
        service = getattr(self.server, "cloud", None)
        if service is None:
            raise RuntimeError("Cloud sign-in is not available in this server")
        return service

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path in {"/", "/index.html"}:
            self.reply(200, page(), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/health":
            self.reply(200, {"ok": True, "app": "input-reply"})
            return
        if not self.authorized():
            return
        try:
            state, backend = self.server.state, self.server.backend
            if parsed.path == "/api/desktop-process":
                if self.client_address[0] not in LOOPBACK:
                    self.reply(403, {"error": "Desktop activation is local only"})
                    return
                self.reply(200, {"pid": os.getpid()})
            elif parsed.path in GET_ACTIONS:
                self.reply(200, dispatch(GET_ACTIONS[parsed.path], {}, backend, state))
            elif parsed.path == "/api/inspect":
                name = parse_qs(parsed.query).get("name", [""])[0]
                self.reply(200, dispatch("inspect", {"name": name}, backend, state))
            elif parsed.path == "/api/script":
                name = parse_qs(parsed.query).get("name", [""])[0]
                self.reply(200, dispatch("script", {"name": name, "op": "get"}, backend, state))
            elif parsed.path == "/api/cloud":
                service = getattr(self.server, "cloud", None)
                self.reply(200, service.status() if service else
                           {"configured": False, "signed_in": False, "disabled": True})
            elif parsed.path == "/api/settings":
                self.reply(200, self.app_settings())
            elif parsed.path == "/api/ai":
                after = parse_qs(parsed.query).get("after", ["0"])[0]
                self.reply(200, self.assistant.status(int(after) if after.isdecimal() else 0))
            else:
                self.reply(404, {"error": "Not found"})
        except (ValueError, RuntimeError, FileNotFoundError, OSError) as error:
            self.reply(400, {"error": str(error)})

    def app_settings(self):
        return {"autostart": autostart.is_installed(), "remote_enabled": settings.load()["remote_enabled"]}

    def do_POST(self):
        if not self.authorized():
            return
        try:
            body = self.body()
            state, backend = self.server.state, self.server.backend
            if self.path in POST_ACTIONS:
                action = POST_ACTIONS[self.path]
                result = dispatch(action, body, backend, state)
                self.reply(202 if action in STARTS_JOB else 200, result)
            elif self.path.startswith("/api/cloud/"):
                self.cloud_action(self.path.rsplit("/", 1)[1], body)
            elif self.path == "/api/settings":
                self.change_settings(body)
            elif self.path.startswith("/api/ai/"):
                self.ai_action(self.path.rsplit("/", 1)[1], body)
            else:
                self.reply(404, {"error": "Not found"})
        except CloudError as error:
            self.reply(error.status if error.status in (401, 403) else 400, {"error": str(error)})
        except (ValueError, RuntimeError, TypeError, FileNotFoundError, OSError) as error:
            self.reply(400, {"error": str(error)})

    def cloud_action(self, action, body):
        cloud = self.cloud
        credentials = {"login", "signup", "reset"}
        if action in credentials and self.client_address[0] not in LOOPBACK:
            # The server itself is plain HTTP; never accept a password over the network.
            raise RuntimeError("Sign in from the computer running Input Reply, or from the remote web app")
        if action in {"login", "signup"}:
            self.reply(200, cloud.sign_in(body.get("email"), body.get("password"), action == "signup"))
        elif action == "reset":
            cloud.reset_password(body.get("email"))
            self.reply(200, {"sent": True})
        elif action == "logout":
            self.reply(200, cloud.sign_out())
        elif action == "remote":
            self.reply(200, cloud.set_remote(bool(body.get("enabled"))))
        else:
            self.reply(404, {"error": "Not found"})

    def ai_action(self, action, body):
        assistant = self.assistant
        if action == "ask":
            self.reply(202, assistant.ask(body.get("text")))
        elif action == "clear":
            assistant.clear()
            self.reply(200, assistant.status())
        elif action == "settings":
            changes = {}
            if "enabled" in body:
                changes["ai_enabled"] = bool(body["enabled"])
            if "voice" in body:
                changes["ai_voice"] = bool(body["voice"])
            if "model" in body:
                if not isinstance(body["model"], str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,80}", body["model"]):
                    raise ValueError("Invalid model name")
                changes["ai_model"] = body["model"]
            if "api_key" in body:
                if self.client_address[0] not in LOOPBACK:
                    raise RuntimeError("Set the API key from the computer running Input Reply")
                ai.set_api_key(body["api_key"] or None)
                assistant.close()
            if changes:
                settings.update(**changes)
            self.reply(200, assistant.status())
        else:
            self.reply(404, {"error": "Not found"})

    def change_settings(self, body):
        if "remote_enabled" in body:
            self.cloud.set_remote(bool(body["remote_enabled"]))
        if "autostart" in body:
            host, port = self.server.server_address[:2]
            if body["autostart"]:
                autostart.install(host, port)
                settings.update(autostart_configured=True)
            else:
                autostart.remove()
                settings.update(autostart_configured=True)  # the user chose; do not re-enable silently
        self.reply(200, self.app_settings())


def make_server(host="127.0.0.1", port=8765, backend=None, cloud=None, auto_port=True):
    backend = backend or select_backend()
    actual_port = int(port)
    try:
        server = ThreadingHTTPServer((host, actual_port), Handler)
    except OSError:
        if not auto_port:
            raise
        from .lockfile import find_available_port
        alt_port = find_available_port(start_port=actual_port + 1, host=host)
        print(f"Port {actual_port} is occupied by another application. Using alternate port {alt_port} instead.", flush=True)
        actual_port = alt_port
        server = ThreadingHTTPServer((host, actual_port), Handler)

    from .lockfile import write_service_lock
    write_service_lock(actual_port, host, os.getpid())

    server.actual_port = actual_port
    server.daemon_threads = True
    server.backend = backend
    server.state = MacroState(backend)
    server.cloud = cloud
    server.assistant = ai.Assistant(backend, server.state)
    server.access_token = access_token()
    return server


def serve(host="127.0.0.1", port=8765, backend=None):
    from .lockfile import clear_service_lock
    server = make_server(host, port, backend)
    actual_port = getattr(server, "actual_port", port)
    print(f"Input Reply listening on {host}:{actual_port} ({server.backend.name})", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        server.state.stop()
    finally:
        clear_service_lock(only_if_pid=os.getpid())
        server.server_close()
