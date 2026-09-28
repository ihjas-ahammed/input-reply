"""Authenticated local/network web API for Input Reply."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import core
from .backends import select_backend


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


def seconds(value, maximum=300):
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Time must be a number") from error
    if not 0 <= number <= maximum or number != number:
        raise ValueError(f"Time must be between 0 and {maximum} seconds")
    return number


def wait_countdown(value, cancel=None):
    until = time.monotonic() + value
    while time.monotonic() < until:
        delay = min(0.1, until - time.monotonic())
        if cancel:
            if cancel.wait(delay):
                return False
        else:
            time.sleep(delay)
    return not (cancel and cancel.is_set())


def mapping_for(data, backend):
    if data.get("format") == core.LEGACY_FORMAT:
        if backend.name != "x11":
            raise RuntimeError("This older X11 recording can only replay on X11")
        return backend.keymap()
    return None


def check_backend(data, backend):
    saved = data.get("backend") or ("x11" if data.get("format") == core.LEGACY_FORMAT else None)
    if saved and saved != backend.name:
        raise RuntimeError(f"Recording uses {saved}; this desktop uses {backend.name}")


def record_once(backend, name, duration, countdown, window_id=None, cancel=None):
    if not wait_countdown(countdown, cancel):
        return None
    target = backend.focus(window_id) if window_id is not None else backend.active_window()
    path = core.safe_path(name)
    data = backend.record(duration, path, cancel)
    data["target_window"] = target
    data["recorded_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    data["backend"] = backend.name
    core.write_recording(name, data)
    return data


def replay_once(backend, name, values, countdown, window_id=None, preserve=False, cancel=None):
    data = core.read_recording(name)
    check_backend(data, backend)
    mapping = mapping_for(data, backend)
    events = core.substitute(data, values, mapping)
    if mapping:
        events = [event | {"key": mapping.get(event["code"], "")}
                  if event["type"].startswith("key_") else event for event in events]
    if not wait_countdown(countdown, cancel):
        return None, False
    target = backend.focus_recorded(data, window_id)
    backend.open_player()
    try:
        success = core.play(events, backend, cancel, preserve)
    finally:
        backend.close_player()
    return target, success


class MacroState:
    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.Lock()
        self.cancel = None
        self.job = {"phase": "idle", "busy": False, "message": "Ready"}

    def snapshot(self):
        with self.lock:
            result = dict(self.job)
        result["desktop_available"] = self.backend.available()
        result["display"] = self.backend.name
        return result

    def _set(self, **values):
        with self.lock:
            self.job.update(values)

    def begin(self, kind, name, duration=0, countdown=3, window_id=None, values=None, preserve=False):
        with self.lock:
            if self.job["busy"]:
                raise RuntimeError("Another macro is already running")
            self.cancel = threading.Event()
            self.job = {"phase": "countdown", "busy": True, "kind": kind, "name": name,
                        "message": f"Starting in {countdown:g} seconds", "started_at": time.time()}
        threading.Thread(target=self._run, args=(kind, name, duration, countdown, window_id, values or {}, preserve),
                         daemon=True).start()

    def _run(self, kind, name, duration, countdown, window_id, values, preserve):
        try:
            if kind == "record":
                self._set(phase="recording", message=f"Recording {name}. Press F12 or Stop to finish.")
                data = record_once(self.backend, name, duration, countdown, window_id, self.cancel)
                if data is None:
                    self._set(phase="cancelled", busy=False, message="Cancelled")
                else:
                    self._set(phase="done", busy=False, message=f"Saved {name}")
            else:
                self._set(phase="replaying", message=f"Replaying {name}")
                target, completed = replay_once(self.backend, name, values, countdown,
                                                window_id, preserve, self.cancel)
                if completed:
                    self._set(phase="done", busy=False, message=f"Replayed {name} in {target['title']}")
                else:
                    self._set(phase="cancelled", busy=False, message="Cancelled")
        except Exception as error:
            self._set(phase="error", busy=False, message=str(error))

    def stop(self):
        with self.lock:
            if not self.job["busy"]:
                return False
            self.cancel.set()
            self.job["message"] = "Stopping…"
            return True


class Handler(BaseHTTPRequestHandler):
    server_version = "InputReply"

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

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path in {"/", "/index.html"}:
            self.reply(200, files("input_reply").joinpath("web.html").read_bytes(), "text/html; charset=utf-8")
            return
        if not self.authorized():
            return
        try:
            if parsed.path == "/api/status":
                self.reply(200, self.server.state.snapshot())
            elif parsed.path == "/api/recordings":
                self.reply(200, {"recordings": core.catalog()})
            elif parsed.path == "/api/windows":
                self.reply(200, {"windows": self.server.backend.windows() if self.server.backend.available() else []})
            elif parsed.path == "/api/inspect":
                name = parse_qs(parsed.query).get("name", [""])[0]
                data = core.read_recording(name)
                self.reply(200, core.inspect(name, mapping_for(data, self.server.backend)))
            else:
                self.reply(404, {"error": "Not found"})
        except (ValueError, RuntimeError, FileNotFoundError, OSError) as error:
            self.reply(400, {"error": str(error)})

    def do_POST(self):
        if not self.authorized():
            return
        try:
            body = self.body()
            state, backend = self.server.state, self.server.backend
            if self.path == "/api/record":
                if not backend.available():
                    raise RuntimeError("Interactive desktop is unavailable")
                name = core.new_name(body.get("name", "macro"))
                duration = seconds(body.get("seconds", 30))
                if duration <= 0:
                    raise ValueError("Recording duration must be positive")
                countdown = seconds(body.get("countdown", 3), 30)
                window_id = body.get("window_id")
                if not str(window_id).isdecimal():
                    raise ValueError("Choose a target desktop window")
                state.begin("record", name, duration, countdown, window_id)
                self.reply(202, {"name": name, "status": "starting"})
            elif self.path == "/api/replay":
                if not backend.available():
                    raise RuntimeError("Interactive desktop is unavailable")
                name = body.get("name")
                data = core.read_recording(name)
                check_backend(data, backend)
                mapping = mapping_for(data, backend)
                core.substitute(data, body.get("params", {}), mapping)
                countdown = seconds(body.get("countdown", 3), 30)
                window_id = body.get("window_id") or None
                if window_id is not None and not str(window_id).isdecimal():
                    raise ValueError("Invalid desktop window")
                state.begin("replay", name, countdown=countdown, window_id=window_id,
                            values=body.get("params", {}), preserve=bool(body.get("preserve_key_holds")))
                self.reply(202, {"name": name, "status": "starting"})
            elif self.path == "/api/stop":
                self.reply(200, {"stopping": state.stop()})
            elif self.path == "/api/delete":
                if state.snapshot()["busy"]:
                    raise RuntimeError("Stop the current macro before deleting")
                path = core.safe_path(body.get("name"))
                path.unlink()
                self.reply(200, {"deleted": path.name})
            elif self.path == "/api/parameter/add":
                if state.snapshot()["busy"]:
                    raise RuntimeError("Stop the current macro before editing parameters")
                name = body.get("name")
                data = core.read_recording(name)
                self.reply(200, core.add_parameter(name, int(body.get("block")), body.get("parameter"),
                                                   mapping_for(data, backend)))
            elif self.path == "/api/parameter/remove":
                if state.snapshot()["busy"]:
                    raise RuntimeError("Stop the current macro before editing parameters")
                name = body.get("name")
                data = core.read_recording(name)
                self.reply(200, core.remove_parameter(name, body.get("parameter"), mapping_for(data, backend)))
            else:
                self.reply(404, {"error": "Not found"})
        except (ValueError, RuntimeError, TypeError, FileNotFoundError, OSError) as error:
            self.reply(400, {"error": str(error)})


def serve(host="127.0.0.1", port=8765, backend=None):
    backend = backend or select_backend()
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.backend = backend
    server.state = MacroState(backend)
    server.access_token = access_token()
    print(f"Input Reply listening on {host}:{port} ({backend.name})", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        server.state.stop()
    finally:
        server.server_close()
