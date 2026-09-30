"""High-level desktop actions (click, drag, type, keys, screenshots) on top of the platform backends.

The AI assistant and AI-designed macros use this layer instead of raw recorded events, so
one macro means the same thing on Windows, X11, and Wayland.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from contextlib import contextmanager
from pathlib import Path

X11_BUTTON = {"left": "1", "middle": "2", "right": "3"}
WINDOWS_KEYS = {"return": "enter", "esc": "esc", "escape": "esc", "super": "cmd", "win": "cmd", "meta": "cmd",
                "control": "ctrl", "pageup": "page_up", "pagedown": "page_down", "prior": "page_up",
                "next": "page_down", "backspace": "backspace", "delete": "delete", "del": "delete"}
SCREENSHOT_TOOLS = (
    ("grim", lambda path: ["grim", path]),
    ("spectacle", lambda path: ["spectacle", "-b", "-n", "-f", "-o", path]),
    ("gnome-screenshot", lambda path: ["gnome-screenshot", "-f", path]),
    ("import", lambda path: ["import", "-window", "root", path]),
    ("maim", lambda path: ["maim", path]),
    ("scrot", lambda path: ["scrot", "-o", path]),
)


class Actuator:
    def __init__(self, backend):
        self.backend = backend
        self.name = backend.name
        self.scale = (1.0, 1.0)
        self.opened = False
        self.depth = 0
        self.lock = threading.RLock()

    # ---- lifecycle -------------------------------------------------------------
    @contextmanager
    def session(self):
        """Input controllers are opened on first use and released when the last user leaves."""
        with self.lock:
            if self.depth == 0 or not self.opened:
                if self.name == "windows":
                    self.backend.open_player()
                self.opened = True
            self.depth += 1
        try:
            yield self
        finally:
            with self.lock:
                self.depth -= 1
                if self.depth == 0:
                    if self.name == "windows":
                        self.backend.close_player()
                    self.opened = False

    def set_scale(self, recorded, actual):
        """Scale recorded coordinates when the screen size differs from when the macro was made."""
        if recorded and actual and tuple(recorded) != tuple(actual):
            self.scale = (actual[0] / recorded[0], actual[1] / recorded[1])
        else:
            self.scale = (1.0, 1.0)

    def _xy(self, x, y):
        return round(x * self.scale[0]), round(y * self.scale[1])

    def screen_size(self):
        try:
            if self.name == "x11":
                width, height = self.backend.cmd("getdisplaygeometry").split()
                return int(width), int(height)
            if self.name == "windows":
                return self.backend.user32.GetSystemMetrics(0), self.backend.user32.GetSystemMetrics(1)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return None

    # ---- steps -----------------------------------------------------------------
    def perform(self, step, cancel: threading.Event | None = None):
        kind = step["type"]
        if kind == "click":
            self.click(*self._xy(step["x"], step["y"]), step.get("button", "left"), step.get("count", 1))
        elif kind == "drag":
            self.drag([self._xy(x, y) for x, y in step["points"]], step.get("button", "left"))
        elif kind == "type":
            self.backend.type_text(step["text"], cancel)
        elif kind == "key":
            self.press(step["keys"])
        elif kind == "scroll":
            self.scroll(step["dx"], step["dy"])
        elif kind == "wait":
            if cancel:
                cancel.wait(step["seconds"])
            else:
                time.sleep(step["seconds"])
        elif kind == "open_url":
            self.open_url(step["url"])
        elif kind == "launch":
            self.launch(step["app"])
        elif kind == "focus":
            self.focus(step["title"])
        else:
            raise ValueError(f"Unknown step type: {kind}")

    def click(self, x, y, button="left", count=1):
        if self.name == "x11":
            self.backend.cmd("mousemove", x, y)
            self.backend.cmd("click", "--repeat", count, "--delay", 80, X11_BUTTON[button])
        elif self.name == "wayland":
            self.backend._run("mousemove", x, y)
            for _ in range(count):
                self.backend._run("click", X11_BUTTON[button])
        else:
            mouse = self.backend.mouse_controller
            mouse.position = (x, y)
            mouse.click(getattr(self.backend.mouse.Button, button), count)

    def drag(self, points, button="left"):
        if self.name == "x11":
            self.backend.cmd("mousemove", *points[0])
            self.backend.cmd("mousedown", X11_BUTTON[button])
            try:
                for x, y in points[1:]:
                    self.backend.cmd("mousemove", x, y)
                    time.sleep(0.01)
            finally:
                self.backend.cmd("mouseup", X11_BUTTON[button])
        elif self.name == "wayland":
            raise RuntimeError("Dragging is not supported on Wayland by wdotool")
        else:
            mouse, pressed = self.backend.mouse_controller, getattr(self.backend.mouse.Button, button)
            mouse.position = points[0]
            mouse.press(pressed)
            try:
                for point in points[1:]:
                    mouse.position = point
                    time.sleep(0.01)
            finally:
                mouse.release(pressed)

    def press(self, keys):
        if self.name == "x11":
            self.backend.cmd("key", "--clearmodifiers", keys)
        elif self.name == "wayland":
            self.backend._run("key", keys)
        else:
            self._windows_chord(keys)

    def _windows_chord(self, keys):
        keyboard, controller = self.backend.keyboard, self.backend.keyboard_controller
        pressed = []
        try:
            for part in keys.split("+"):
                name = WINDOWS_KEYS.get(part.lower(), part.lower())
                key = getattr(keyboard.Key, name) if hasattr(keyboard.Key, name) else keyboard.KeyCode.from_char(part)
                controller.press(key)
                pressed.append(key)
        finally:
            for key in reversed(pressed):
                controller.release(key)

    def scroll(self, dx, dy):
        if self.name == "x11":
            for amount, positive, negative in ((dy, "5", "4"), (dx, "7", "6")):
                if amount:
                    self.backend.cmd("click", "--repeat", abs(amount), positive if amount > 0 else negative)
        elif self.name == "wayland":
            self.backend._run("scroll", dx, dy)
        else:
            self.backend.mouse_controller.scroll(dx, -dy)

    def open_url(self, url):
        # A BROWSER command containing %s makes webbrowser.open() wait until the browser quits, so never wait long.
        outcome: list[bool] = []
        opener = threading.Thread(target=lambda: outcome.append(webbrowser.open(url)), daemon=True)
        opener.start()
        opener.join(5)
        if outcome and not outcome[0]:
            raise RuntimeError("No web browser could be opened")

    def launch(self, app):
        path = shutil.which(app)
        if path:
            command = [path]
        elif shutil.which("gtk-launch"):
            command = ["gtk-launch", app]
        else:
            raise RuntimeError(f"Cannot find an application named {app}")
        options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name != "nt":
            options["start_new_session"] = True
        subprocess.Popen(command, **options)

    def focus(self, title):
        wanted = title.lower()
        for window in self.backend.windows():
            if wanted in window["title"].lower():
                self.backend.focus(window["id"])
                return window
        raise RuntimeError(f"No open window has {title!r} in its title")

    # ---- screenshots -----------------------------------------------------------
    def screenshot(self):
        """Full-screen capture as a PIL image."""
        try:
            from PIL import Image, ImageGrab
        except ImportError as error:
            raise RuntimeError("Install Pillow to let the assistant see the screen: pip install 'input-reply[ai]'") from error
        problems = []
        if sys.platform == "win32" or self.name == "x11":
            try:
                display = getattr(self.backend, "display", None) if self.name == "x11" else None
                return (ImageGrab.grab(xdisplay=display) if display else ImageGrab.grab()).convert("RGB")
            except Exception as error:  # Pillow raises OSError/ValueError depending on platform
                problems.append(f"ImageGrab: {error}")
        for tool, build in SCREENSHOT_TOOLS:
            if not shutil.which(tool):
                continue
            with tempfile.TemporaryDirectory() as folder:
                path = str(Path(folder) / "shot.png")
                try:
                    subprocess.run(build(path), check=True, capture_output=True, timeout=20, env=self._env())
                    if Path(path).stat().st_size:
                        return Image.open(path).convert("RGB")
                except (OSError, subprocess.SubprocessError) as error:
                    problems.append(f"{tool}: {error}")
        raise RuntimeError("Could not capture the screen. Install grim (Wayland), spectacle, gnome-screenshot, "
                           "ImageMagick, or scrot. " + "; ".join(problems)[:300])

    def _env(self):
        return getattr(self.backend, "env", None) if isinstance(getattr(self.backend, "env", None), dict) else None
