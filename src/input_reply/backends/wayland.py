"""Wayland recorder/player through wdotool's portal or evdev backend.

wdotool's recorder captures chords and clicks (not key/button hold state).
Portal capture provides absolute mouse positions; evdev provides deltas.
"""

import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path


class WaylandBackend:
    name = "wayland"

    def __init__(self):
        self.tool = shutil.which("wdotool")
        self.env = os.environ.copy()
        self.input_backend = os.environ.get("INPUT_REPLY_WAYLAND_CAPTURE", "auto")

    def available(self):
        return bool(self.tool and self.env.get("WAYLAND_DISPLAY"))

    def _run(self, *args, input=None, timeout=10, check=True):
        if not self.tool:
            raise RuntimeError("Install wdotool with its recorder feature to use Wayland")
        result = subprocess.run([self.tool, *map(str, args)], input=input, text=True,
                                env=self.env, capture_output=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError((result.stderr or "wdotool command failed").strip().splitlines()[-1])
        return result.stdout.strip()

    def windows(self):
        if not self.available():
            return []
        try:
            ids = self._run("search", "--name", ".", timeout=3).splitlines()
            windows = []
            for ident in dict.fromkeys(ids):
                if ident.isdecimal():
                    try:
                        title = self._run("getwindowname", ident, timeout=3)
                        windows.append({"id": ident, "title": title or "Untitled window"})
                    except RuntimeError:
                        pass
            if windows:
                return windows
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        return [{"id": "0", "title": "Currently focused window"}]

    def active_window(self):
        try:
            ident = self._run("getactivewindow", timeout=3)
            if ident.isdecimal() and int(ident) > 0:
                return {"id": ident, "title": self._run("getwindowname", ident, timeout=3) or "Untitled window"}
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        return {"id": "0", "title": "Currently focused window"}

    def focus(self, ident):
        if str(ident) == "0":
            return self.active_window()
        if not str(ident).isdecimal():
            raise ValueError("Invalid window ID")
        self._run("windowactivate", ident, timeout=5)
        focused = self.active_window()
        if focused["id"] != str(ident):
            raise RuntimeError("Window focus could not be confirmed. Focus it manually during the countdown.")
        return focused

    def focus_recorded(self, data, override=None):
        if override:
            return self.focus(override)
        target = data.get("target_window") or {}
        if target.get("id") and str(target["id"]) != "0":
            try:
                return self.focus(target["id"])
            except (RuntimeError, ValueError):
                pass
        if target.get("title") and target["title"] != "Currently focused window":
            for window in self.windows():
                if window["title"] == target["title"]:
                    return self.focus(window["id"])
        return self.active_window()

    @staticmethod
    def _text_chord(chord):
        parts = chord.lower().split("+")
        if any(part in {"ctrl", "control", "alt", "super", "meta", "cmd"} for part in parts[:-1]):
            return False
        last = parts[-1]
        return len(last) == 1 or last in {"space", "backspace", "minus", "equal", "comma",
                                         "period", "slash", "semicolon", "apostrophe", "grave",
                                         "backslash", "bracketleft", "bracketright"}

    @classmethod
    def convert_trace(cls, trace):
        if not isinstance(trace, list):
            raise ValueError("Invalid wdotool trace")
        events = []
        x = y = 0
        known = False
        for item in trace:
            if not isinstance(item, dict) or not isinstance(item.get("t_ms"), int):
                raise ValueError("Invalid wdotool trace event")
            at = max(0.0, item["t_ms"] / 1000)
            kind = item.get("kind")
            if kind == "key":
                chord = item.get("chord")
                if not isinstance(chord, str) or not chord:
                    raise ValueError("Invalid wdotool key")
                for typ in ("key_down", "key_up"):
                    events.append({"type": typ, "key": chord, "text_key": cls._text_chord(chord), "t": at})
            elif kind == "click":
                button = str(item.get("button"))
                for typ in ("button_down", "button_up"):
                    events.append({"type": typ, "button": button, "x": x, "y": y,
                                   "coordinate_known": known, "t": at})
            elif kind == "move_abs":
                x, y = int(item["x"]), int(item["y"])
                known = True
                events.append({"type": "motion", "x": x, "y": y, "t": at})
            elif kind == "move_delta":
                dx, dy = int(item["dx"]), int(item["dy"])
                if known:
                    x, y = x + dx, y + dy
                events.append({"type": "motion_delta", "dx": dx, "dy": dy, "t": at})
            elif kind == "scroll":
                events.append({"type": "scroll", "dx": int(item["dx"]), "dy": int(item["dy"]), "t": at})
            elif kind != "gap":
                raise ValueError("Unsupported wdotool trace event")
        return events

    def record(self, seconds, output, cancel):
        if not self.available():
            raise RuntimeError("Wayland session or wdotool is unavailable")
        if self.input_backend not in {"auto", "portal", "evdev"}:
            raise ValueError("INPUT_REPLY_WAYLAND_CAPTURE must be auto, portal, or evdev")
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            trace_path = Path(tmp.name)
        try:
            command = [self.tool, "record", "--backend", self.input_backend, "--max-duration",
                       str(max(1, math.ceil(seconds))), "-o", str(trace_path)]
            proc = subprocess.Popen(command, env=self.env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True)
            started = time.monotonic()
            while proc.poll() is None:
                if cancel and cancel.wait(0.05):
                    proc.send_signal(signal.SIGINT)
                    break
                if time.monotonic() - started >= seconds:
                    proc.send_signal(signal.SIGINT)
                    break
                if not cancel:
                    time.sleep(0.05)
            _, stderr = proc.communicate()
            if proc.returncode:
                raise RuntimeError((stderr or "Wayland recording failed").strip().splitlines()[-1])
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            return {"format": "input-reply-v1", "backend": self.name,
                    "duration": round(min(time.monotonic() - started, seconds), 3),
                    "events": self.convert_trace(trace)}
        finally:
            trace_path.unlink(missing_ok=True)

    def open_player(self):
        if not self.available():
            raise RuntimeError("Wayland session or wdotool is unavailable")
        return self

    def close_player(self):
        pass

    def emit(self, event):
        kind = event["type"]
        if kind == "key_down":
            self._run("key", event["key"])
        elif kind == "button_down":
            if event.get("coordinate_known"):
                self._run("mousemove", event["x"], event["y"])
            self._run("click", event["button"])
        elif kind == "motion":
            self._run("mousemove", event["x"], event["y"])
        elif kind == "motion_delta":
            self._run("mousemove", "--relative", event["dx"], event["dy"])
        elif kind == "scroll":
            self._run("scroll", event["dx"], event["dy"])

    def type_text(self, value, cancel=None):
        if not value:
            return
        proc = subprocess.Popen([self.tool, "type", "--file", "-"], env=self.env,
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True)
        proc.stdin.write(value)
        proc.stdin.close()
        while proc.poll() is None:
            if cancel and cancel.wait(0.05):
                proc.terminate()
                proc.wait(timeout=2)
                return
            if not cancel:
                time.sleep(0.05)
        stderr = proc.stderr.read()
        if proc.returncode:
            raise RuntimeError((stderr or "Could not type parameter value").strip().splitlines()[-1])
