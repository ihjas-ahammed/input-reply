"""Accounts and remote control through Firebase.

Data model, all under ``inputReply/users/<uid>`` (see firebase/database.rules.json)::

    devices/<device>            presence, job status and a catalog of recordings (never typed text)
    commands/<device>/<id>      {action, args, createdAt, status, result|error}

Any client signed in as the same user can push a command; the desktop agent
running on ``<device>`` streams that node, executes commands, and writes back
the outcome. Only the owner of a uid can read or write its subtree.
"""

from __future__ import annotations

import json
import platform
import queue
import secrets
import socket
import threading
import time

from . import __version__, core, settings
from .actions import CHANGES_CATALOG, dispatch, mapping_for
from .firebase import ROOT, CloudError, Database, Session, load_config

COMMAND_TTL = 120          # seconds a queued command stays valid; stale ones are never run
HEARTBEAT = 30             # seconds between presence updates
KEEP_FINISHED = 20         # finished commands kept per device
TERMINAL = {"done", "error", "expired"}
SERVER_TIME = {".sv": "timestamp"}
MAX_ARGS_BYTES = 65536


def device_id() -> str:
    path = core.data_dir() / "device-id"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = secrets.token_hex(8)
    path.write_text(value + "\n", encoding="utf-8")
    return value


def device_name() -> str:
    return socket.gethostname() or "This computer"


def user_path(uid, *parts) -> str:
    return "/".join([ROOT, "users", uid, *parts])


class Agent:
    """Runs on the desktop: publishes presence and executes commands for one signed-in user."""

    def __init__(self, database: Database, uid: str, backend, state, on_revoked=None):
        self.db, self.uid, self.backend, self.state = database, uid, backend, state
        self.device = device_id()
        self.on_revoked = on_revoked
        self.stop_event = threading.Event()
        self.jobs: queue.Queue = queue.Queue()
        self.seen: dict[str, None] = {}
        self.connected = False
        self.error: str | None = None
        self.catalog_dirty = True
        self.finished = 0
        self.threads: list[threading.Thread] = []

    # ---- paths -----------------------------------------------------------------
    def _device(self, *parts):
        return user_path(self.uid, "devices", self.device, *parts)

    def _commands(self, *parts):
        return user_path(self.uid, "commands", self.device, *parts)

    # ---- lifecycle -------------------------------------------------------------
    def start(self):
        for target in (self._stream, self._worker, self._beat):
            thread = threading.Thread(target=target, daemon=True, name=f"cloud-{target.__name__}")
            thread.start()
            self.threads.append(thread)

    def stop(self, mark_offline=True):
        self.stop_event.set()
        if mark_offline and self.connected:
            try:
                self.db.patch(self._device(), {"online": False, "lastSeen": SERVER_TIME})
            except (CloudError, ValueError):
                pass
        self.connected = False

    def _failed(self, error):
        if getattr(error, "code", None) == "SESSION_REVOKED":
            self.stop(mark_offline=False)
            if self.on_revoked:
                self.on_revoked()
            return
        self.connected = False
        self.error = str(error)

    # ---- presence and status ---------------------------------------------------
    def _presence(self):
        return {"name": device_name(), "platform": platform.system(), "backend": self.backend.name,
                "version": __version__, "online": True, "lastSeen": SERVER_TIME,
                "remoteEnabled": bool(settings.load()["remote_enabled"]),
                "desktopAvailable": self.state.desktop.available()}

    def _job(self):
        job = self.state.snapshot()
        return {k: job.get(k) for k in ("phase", "busy", "kind", "name", "message") if job.get(k) is not None}

    def _catalog(self):
        rows = []
        for row in core.catalog():
            try:
                info = core.inspect(row["name"], mapping_for(core.read_recording(row["name"]), self.backend))
            except (ValueError, RuntimeError, OSError):
                info = {"blocks": [], "parameters": []}
            rows.append(row | {"blocks": info["blocks"], "parameters": info["parameters"]})
        return rows

    def _beat(self):
        version, last_beat = -1, 0.0
        while not self.stop_event.is_set():
            try:
                now = time.monotonic()
                job_version = self.state.version
                if now - last_beat >= HEARTBEAT or last_beat == 0.0:
                    self.db.patch(self._device(), self._presence())
                    self.connected, self.error, last_beat = True, None, now
                    version = -1  # force a job/catalog refresh after (re)connecting
                if self.connected and job_version != version:
                    self.db.put(self._device("job"), self._job())
                    version = job_version
                if self.connected and self.catalog_dirty:
                    self.catalog_dirty = False
                    self.db.put(self._device("recordings"), self._catalog())
            except (CloudError, ValueError) as error:
                self.catalog_dirty = True
                self._failed(error)
                last_beat = 0.0 if not self.stop_event.is_set() else last_beat
                self.stop_event.wait(5)
            self.stop_event.wait(1)

    # ---- commands --------------------------------------------------------------
    def _stream(self):
        try:
            self.db.stream(self._commands(), self._on_event, self.stop_event)
        except CloudError as error:
            self._failed(error)

    def _on_event(self, event, path, data):
        parts = [p for p in path.split("/") if p]
        if not parts and isinstance(data, dict):
            for command_id, command in data.items():
                self._consider(command_id, command)
            self._prune(data)
        elif len(parts) == 1:
            self._consider(parts[0], data)

    def _consider(self, command_id, command):
        if not isinstance(command, dict) or command.get("status", "pending") != "pending":
            return
        if command_id in self.seen:
            return
        self.seen[command_id] = None
        while len(self.seen) > 500:
            self.seen.pop(next(iter(self.seen)))
        self.jobs.put((command_id, command))

    def _prune(self, commands):
        finished = sorted((c for c in commands.items()
                           if isinstance(c[1], dict) and c[1].get("status") in TERMINAL),
                          key=lambda item: item[1].get("createdAt") or 0)
        stale = finished[:-KEEP_FINISHED] if len(finished) > KEEP_FINISHED else []
        if stale:
            try:
                self.db.patch(self._commands(), {command_id: None for command_id, _ in stale})
            except (CloudError, ValueError):
                pass

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                command_id, command = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._execute(command_id, command)
            except (CloudError, ValueError) as error:
                self._failed(error)

    def _finish(self, command_id, **fields):
        self.db.patch(self._commands(command_id), fields | {"finishedAt": SERVER_TIME})

    def _execute(self, command_id, command):
        action, args, created = command.get("action"), command.get("args") or {}, command.get("createdAt")
        if not isinstance(created, (int, float)):
            return self._finish(command_id, status="error", error="Command has no server timestamp")
        if self.db.server_time() - created / 1000 > COMMAND_TTL:
            return self._finish(command_id, status="expired", error="Command was not delivered in time")
        if not settings.load()["remote_enabled"]:
            return self._finish(command_id, status="error", error="Remote control is turned off on this computer")
        if not isinstance(action, str) or not isinstance(args, dict) or len(json.dumps(args)) > MAX_ARGS_BYTES:
            return self._finish(command_id, status="error", error="Invalid command")
        # REST PATCH supports multi-path keys: atomically mark running and delete params.
        # Replacement text is only needed to start the job; do not leave it in the database.
        self.db.patch(self._commands(command_id), {"status": "running", "startedAt": SERVER_TIME, "args/params": None})
        try:
            result = dispatch(action, args, self.backend, self.state)
            self._finish(command_id, status="done", result=result)
        except (ValueError, RuntimeError, TypeError, OSError, KeyError) as error:
            self._finish(command_id, status="error", error=str(error))
        if action in CHANGES_CATALOG:
            self.catalog_dirty = True
        self.finished += 1
        if self.finished % 25 == 0:
            self._prune(self.db.get(self._commands()) or {})


class CloudService:
    """Owns the sign-in session and the agent; used by the local API, tray, and CLI."""

    def __init__(self, backend, state, config=None):
        self.backend, self.state = backend, state
        self.lock = threading.Lock()
        self.agent: Agent | None = None
        self.config_error = None
        try:
            self.config = config or load_config()
            self.session = Session(self.config)
        except (CloudError, ValueError, OSError) as error:
            self.config, self.session, self.config_error = None, None, str(error)

    def _require(self):
        if not self.session:
            raise CloudError(f"Cloud sign-in is not configured: {self.config_error}")
        return self.session

    def _start_agent(self):
        self._stop_agent()
        core.set_account(self.session.uid)
        self.agent = Agent(Database(self.session), self.session.uid, self.backend, self.state,
                           on_revoked=self._revoked)
        self.agent.start()

    def _stop_agent(self, mark_offline=True):
        if self.agent:
            self.agent.stop(mark_offline)
            self.agent = None

    def _revoked(self):
        with self.lock:
            self.agent = None
            self.session.sign_out()
            core.set_account(None)

    def start_saved(self) -> bool:
        """Resume the saved session without needing the network (boot may be offline)."""
        if not self.session:
            return False
        with self.lock:
            if self.session.restore():
                self._start_agent()
                return True
        return False

    def sign_in(self, email, password, create=False):
        session = self._require()
        with self.lock:
            session.sign_in(email, password, create)
            core.adopt_legacy_recordings(session.uid)
            self._start_agent()
        return self.status()

    def sign_out(self):
        with self.lock:
            self._stop_agent()
            if self.session:
                self.session.sign_out()
            core.set_account(None)
        return self.status()

    def reset_password(self, email):
        self._require().reset_password(email)

    def set_remote(self, enabled: bool):
        settings.update(remote_enabled=bool(enabled))
        agent = self.agent
        if agent and agent.connected:
            try:
                agent.db.patch(agent._device(), {"remoteEnabled": bool(enabled)})
            except (CloudError, ValueError):
                pass  # the next heartbeat publishes it
        return self.status()

    def shutdown(self):
        with self.lock:
            self._stop_agent()

    def status(self) -> dict:
        session, agent = self.session, self.agent
        return {"configured": bool(session), "config_error": self.config_error,
                "signed_in": bool(session and session.signed_in), "email": session and session.email,
                "uid": session and session.uid, "device_id": device_id(), "device_name": device_name(),
                "remote_enabled": bool(settings.load()["remote_enabled"]),
                "connected": bool(agent and agent.connected), "error": agent.error if agent else None}


class RemoteClient:
    """Drive another signed-in computer from the command line (uses the same protocol as any client)."""

    def __init__(self):
        config = load_config()
        self.session = Session(config)
        if not self.session.restore():
            raise CloudError("Not signed in. Run: input-reply login")
        self.db = Database(self.session)
        self.uid = self.session.uid

    def devices(self) -> dict:
        found = self.db.get(user_path(self.uid, "devices")) or {}
        return {key: value for key, value in found.items() if isinstance(value, dict)}

    def resolve(self, wanted=None) -> str:
        found = self.devices()
        if wanted:
            matches = [k for k, v in found.items() if wanted in (k, v.get("name"))]
            if len(matches) != 1:
                raise CloudError(f"No single device matches {wanted!r}. Run: input-reply remote devices")
            return matches[0]
        online = [k for k, v in found.items() if v.get("online")]
        if len(online) != 1:
            raise CloudError("Choose a computer with --device (run: input-reply remote devices)")
        return online[0]

    def send(self, device, action, args=None, wait=30.0) -> dict:
        path = user_path(self.uid, "commands", device)
        command_id = self.db.push(path, {"action": action, "args": args or {}, "createdAt": SERVER_TIME,
                                         "status": "pending", "source": "cli"})
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            command = self.db.get(f"{path}/{command_id}") or {}
            if command.get("status") in TERMINAL:
                return command
            time.sleep(0.5)
        raise CloudError("The computer did not answer in time. Is Input Reply running there?")
