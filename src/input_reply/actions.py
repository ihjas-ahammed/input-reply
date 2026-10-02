"""Everything the app can do, behind one dispatcher.

The local web API and the Firebase remote agent both call ``dispatch`` so a
command behaves the same no matter which client triggered it.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from . import core, macros
from .actuator import Actuator
from .backends.windows import WindowsBackend


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


def record_once(backend, name, duration, countdown, window_id=None, cancel=None, on_started=None, on_focusing=None):
    if not wait_countdown(countdown, cancel):
        return None
    if window_id is not None and on_focusing:
        on_focusing()
    if window_id is not None and isinstance(backend, WindowsBackend):
        target = backend.focus(window_id, cancel=cancel)
    else:
        target = backend.focus(window_id) if window_id is not None else backend.active_window()
    if target is None or (cancel and cancel.is_set()):
        return None
    if on_started:
        on_started()
    path = core.safe_path(name)
    data = backend.record(duration, path, cancel)
    data["target_window"] = target
    data["recorded_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    data["backend"] = backend.name
    core.write_recording(name, data)
    return data


def replay_once(backend, name, values, countdown, window_id=None, preserve=False, cancel=None, on_started=None, on_focusing=None):
    data = core.read_recording(name)
    if data.get("format") == core.FORMAT_AGENT:
        macros.resolve(data, values)
        if not wait_countdown(countdown, cancel):
            return None, False
        if on_started:
            on_started()
        return {"id": "0", "title": "AI macro"}, macros.play(data, values, Actuator(backend), cancel)
    check_backend(data, backend)
    mapping = mapping_for(data, backend)
    events = core.substitute(data, values, mapping)
    if mapping:
        events = [event | {"key": mapping.get(event["code"], "")}
                  if event["type"].startswith("key_") else event for event in events]
    if not wait_countdown(countdown, cancel):
        return None, False
    if on_focusing:
        on_focusing()
    if isinstance(backend, WindowsBackend):
        target = backend.focus_recorded(data, window_id, cancel=cancel)
    else:
        target = backend.focus_recorded(data, window_id)
    if target is None or (cancel and cancel.is_set()):
        return None, False
    if on_started:
        on_started()
    backend.open_player()
    try:
        success = core.play(events, backend, cancel, preserve)
    finally:
        backend.close_player()
    return target, success


class Desktop:
    """Backend probes are subprocess calls; keep recent answers instead of repeating them."""

    def __init__(self, backend, available_ttl=5.0, windows_ttl=2.0):
        self.backend = backend
        self.available_ttl, self.windows_ttl = available_ttl, windows_ttl
        self._lock = threading.Lock()
        self._available = (0.0, False)
        self._windows = (0.0, [])

    def available(self):
        now = time.monotonic()
        with self._lock:
            stamp, value = self._available
        if now - stamp < self.available_ttl:
            return value
        value = bool(self.backend.available())
        with self._lock:
            self._available = (now, value)
        return value

    def windows(self, force=False):
        now = time.monotonic()
        with self._lock:
            stamp, value = self._windows
        if not force and now - stamp < self.windows_ttl:
            return list(value)
        value = self.backend.windows() if self.available() else []
        with self._lock:
            self._windows = (now, value)
        return list(value)

    def invalidate(self):
        with self._lock:
            self._available, self._windows = (0.0, False), (0.0, [])


class MacroState:
    def __init__(self, backend):
        self.backend = backend
        self.desktop = Desktop(backend)
        self.lock = threading.Lock()
        self.cancel = None
        self.on_stop = None
        self.job = {"phase": "idle", "busy": False, "message": "Ready"}
        self.version = 0

    def snapshot(self):
        with self.lock:
            result = dict(self.job)
        result["desktop_available"] = self.desktop.available()
        result["display"] = self.backend.name
        return result

    def busy(self):
        with self.lock:
            return bool(self.job["busy"])

    def _set(self, **values):
        with self.lock:
            self.job.update(values)
            self.version += 1

    def begin(self, kind, name, duration=0, countdown=3, window_id=None, values=None, preserve=False):
        with self.lock:
            if self.job["busy"]:
                raise RuntimeError("Another macro is already running")
            self.cancel = threading.Event()
            self.on_stop = None
            self.job = {"phase": "countdown", "busy": True, "kind": kind, "name": name,
                        "message": f"Starting in {countdown:g} seconds", "started_at": time.time()}
            self.version += 1
        threading.Thread(target=self._run, args=(kind, name, duration, countdown, window_id, values or {}, preserve),
                         daemon=True).start()

    def _run(self, kind, name, duration, countdown, window_id, values, preserve):
        try:
            if kind == "record":
                data = record_once(self.backend, name, duration, countdown, window_id, self.cancel,
                                   on_focusing=lambda: self._set(phase="focusing",
                                       message="Activating target window. If it stays in the background, click it now (up to 15 seconds)."),
                                   on_started=lambda: self._set(phase="recording",
                                       message=f"Recording {name}. Press F12 or Stop to finish."))
                if data is None:
                    self._set(phase="cancelled", busy=False, message="Cancelled")
                else:
                    self._set(phase="done", busy=False, message=f"Saved {name}")
            else:
                target, completed = replay_once(self.backend, name, values, countdown,
                                                window_id, preserve, self.cancel,
                                                on_focusing=lambda: self._set(phase="focusing",
                                                    message="Activating target window. If it stays in the background, click it now (up to 15 seconds)."),
                                                on_started=lambda: self._set(phase="replaying", message=f"Replaying {name}"))
                if completed:
                    self._set(phase="done", busy=False, message=f"Replayed {name} in {target['title']}")
                else:
                    self._set(phase="cancelled", busy=False, message="Cancelled")
        except Exception as error:
            self._set(phase="error", busy=False, message=str(error))
        finally:
            self.desktop.invalidate()

    def claim(self, kind, name, message, on_stop=None):
        """Reserve the desktop for a long-running task that manages its own thread (the AI assistant)."""
        with self.lock:
            if self.job["busy"]:
                raise RuntimeError("Another macro is already running")
            self.cancel = threading.Event()
            self.on_stop = on_stop
            self.job = {"phase": "replaying", "busy": True, "kind": kind, "name": name,
                        "message": message, "started_at": time.time()}
            self.version += 1
            return self.cancel

    def update(self, message):
        self._set(message=message)

    def release(self, phase, message):
        self._set(phase=phase, busy=False, message=message)
        self.desktop.invalidate()

    def stop(self):
        with self.lock:
            busy = bool(self.job["busy"])
            on_stop = self.on_stop
            if not busy and not on_stop:
                return False
            self.cancel.set()
            if busy:
                self.job["message"] = "Stopping…"
                self.version += 1
        if on_stop:
            on_stop()
        return True


def _window_id(value, required):
    if value in (None, ""):
        if required:
            raise ValueError("Choose a target desktop window")
        return None
    if not str(value).isdecimal():
        raise ValueError("Invalid desktop window")
    return value


def _require_desktop(state):
    if not state.desktop.available():
        raise RuntimeError("Interactive desktop is unavailable")


def _idle(state, action):
    if state.busy():
        raise RuntimeError(f"Stop the current macro before {action}")


def _status(args, backend, state):
    return state.snapshot()


def _recordings(args, backend, state):
    return {"recordings": core.catalog()}


def _windows(args, backend, state):
    return {"windows": state.desktop.windows()}


def _inspect(args, backend, state):
    name = args.get("name", "")
    return core.inspect(name, mapping_for(core.read_recording(name), backend))


def _record(args, backend, state):
    _require_desktop(state)
    name = core.new_name(args.get("name", "macro"))
    duration = seconds(args.get("seconds", 30))
    if duration <= 0:
        raise ValueError("Recording duration must be positive")
    countdown = seconds(args.get("countdown", 3), 30)
    state.begin("record", name, duration, countdown, _window_id(args.get("window_id"), True))
    return {"name": name, "status": "starting"}


def _replay(args, backend, state):
    _require_desktop(state)
    name = args.get("name")
    data = core.read_recording(name)
    values = args.get("params", {})
    if data.get("format") == core.FORMAT_AGENT:
        macros.resolve(data, values)
    else:
        check_backend(data, backend)
        core.substitute(data, values, mapping_for(data, backend))
    countdown = seconds(args.get("countdown", 3), 30)
    state.begin("replay", name, countdown=countdown, window_id=_window_id(args.get("window_id"), False),
                values=values, preserve=bool(args.get("preserve_key_holds")))
    return {"name": name, "status": "starting"}


def _stop(args, backend, state):
    return {"stopping": state.stop()}


def _delete(args, backend, state):
    _idle(state, "deleting")
    return {"deleted": core.delete_recording(args.get("name"))}


def _parameter_add(args, backend, state):
    _idle(state, "editing parameters")
    name = args.get("name")
    mapping = mapping_for(core.read_recording(name), backend)
    return core.add_parameter(name, int(args.get("block")), args.get("parameter"), mapping)


def _parameter_remove(args, backend, state):
    _idle(state, "editing parameters")
    name = args.get("name")
    mapping = mapping_for(core.read_recording(name), backend)
    return core.remove_parameter(name, args.get("parameter"), mapping)


ACTIONS = {"status": _status, "recordings": _recordings, "windows": _windows, "inspect": _inspect,
           "record": _record, "replay": _replay, "stop": _stop, "delete": _delete,
           "parameter_add": _parameter_add, "parameter_remove": _parameter_remove}
# Actions that only start a job; the HTTP API answers these with 202.
STARTS_JOB = {"record", "replay"}
# Actions that change the recording list or its parameters, so a remote catalog goes stale.
CHANGES_CATALOG = {"record", "delete", "parameter_add", "parameter_remove"}


def dispatch(action, args, backend, state):
    handler = ACTIONS.get(action)
    if handler is None:
        raise ValueError(f"Unknown action: {action}")
    if not isinstance(args, dict):
        raise ValueError("Action arguments must be an object")
    return handler(args, backend, state)
