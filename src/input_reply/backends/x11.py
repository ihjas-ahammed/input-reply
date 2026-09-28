"""X11 backend using the original tested recorder and XTest player."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import x11_engine


class X11Backend:
    name = "x11"

    def __init__(self):
        self.display = os.environ.get("INPUT_REPLY_DISPLAY") or os.environ.get("DISPLAY") or ":0"
        self.auth = os.environ.get("INPUT_REPLY_XAUTHORITY") or os.environ.get("XAUTHORITY") or str(Path.home() / ".Xauthority")
        self.x11 = None
        os.environ["DISPLAY"] = self.display
        os.environ["XAUTHORITY"] = self.auth

    def env(self):
        return os.environ | {"DISPLAY": self.display, "XAUTHORITY": self.auth}

    def cmd(self, *parts, check=True):
        result = subprocess.run(["xdotool", *map(str, parts)], env=self.env(), check=check,
                                capture_output=True, text=True, timeout=5)
        return result.stdout.strip()

    def available(self):
        try:
            self.cmd("getdisplaygeometry")
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def keymap(self):
        return x11_engine.keyboard_map()

    def windows(self):
        try:
            ids = self.cmd("search", "--onlyvisible", "--name", ".").splitlines()
        except (OSError, subprocess.SubprocessError):
            return []
        result = []
        for ident in dict.fromkeys(ids):
            if ident.isdecimal():
                try:
                    result.append({"id": ident, "title": self.cmd("getwindowname", ident) or "Untitled window"})
                except (OSError, subprocess.SubprocessError):
                    pass
        return result

    def active_window(self):
        ident = self.cmd("getwindowfocus")
        if not ident.isdecimal() or ident == "0":
            raise RuntimeError("Focus a desktop window before recording")
        return {"id": ident, "title": self.cmd("getwindowname", ident) or "Untitled window"}

    def focus(self, ident):
        if not str(ident).isdecimal() or int(ident) == 0:
            raise ValueError("Invalid window ID")
        try:
            self.cmd("windowactivate", "--sync", ident)
        except (OSError, subprocess.SubprocessError):
            self.cmd("windowfocus", "--sync", ident)
        focused = self.active_window()
        if focused["id"] != str(ident):
            raise RuntimeError("Target window did not gain focus")
        return focused

    def focus_recorded(self, data, override=None):
        if override:
            return self.focus(override)
        target = data.get("target_window") or {}
        ident, title = target.get("id"), target.get("title")
        if ident:
            try:
                if not title or self.cmd("getwindowname", ident) == title:
                    return self.focus(ident)
            except (RuntimeError, OSError, subprocess.SubprocessError):
                pass
        if title:
            for window in self.windows():
                if window["title"] == title:
                    return self.focus(window["id"])
        raise RuntimeError("Recorded window is closed. Choose another open window.")

    def record(self, seconds, output, cancel):
        command = [sys.executable, "-m", "input_reply.backends.x11_engine", "record",
                   "--seconds", str(seconds), "--output", str(output), "--trim-end", "0.2"]
        proc = subprocess.Popen(command, env=self.env(), stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True)
        while proc.poll() is None:
            if cancel and cancel.wait(0.05):
                proc.send_signal(signal.SIGINT)
                break
            if not cancel:
                time.sleep(0.05)
        _, stderr = proc.communicate()
        if proc.returncode:
            raise RuntimeError((stderr or "X11 recording failed").strip().splitlines()[-1])
        return json.loads(Path(output).read_text(encoding="utf-8"))

    def open_player(self):
        self.x11 = x11_engine.X11()
        return self

    def close_player(self):
        if self.x11:
            self.x11.close()
            self.x11 = None

    def emit(self, event):
        if event["type"].startswith("button_"):
            self.x11.emit({"type": "motion", "x": event["x"], "y": event["y"]})
        self.x11.emit(event)

    def type_text(self, value, cancel=None):
        if not value:
            return
        proc = subprocess.Popen(["xdotool", "type", "--clearmodifiers", "--delay", "10", "--", value],
                                env=self.env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        while proc.poll() is None:
            if cancel and cancel.wait(0.05):
                proc.terminate()
                proc.wait(timeout=2)
                return
            if not cancel:
                time.sleep(0.05)
        if proc.returncode:
            raise RuntimeError("Could not type the parameter value")
