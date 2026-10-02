"""High-level desktop actions (click, drag, type, keys, screenshots) on top of the platform backends.

The AI assistant and AI-designed macros use this layer instead of raw recorded events, so
one macro means the same thing on Windows, X11, and Wayland.
"""

from __future__ import annotations

import math
import os
import random
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


def generate_human_path(start: tuple[int, int], end: tuple[int, int], steps: int = 25) -> list[tuple[int, int]]:
    """Generate natural, slightly curved mouse movement points between start and end using a cubic Bezier curve."""
    x0, y0 = start
    x3, y3 = end
    dist = math.hypot(x3 - x0, y3 - y0)
    if dist < 2:
        return [(int(round(x3)), int(round(y3)))]
    deviation = min(dist * 0.18, 65.0)
    side = random.choice([-1, 1])
    cx = (x0 + x3) / 2.0 + side * deviation * random.uniform(0.4, 0.9)
    cy = (y0 + y3) / 2.0 - side * deviation * random.uniform(0.4, 0.9)
    p0 = (float(x0), float(y0))
    p1 = ((x0 + cx) / 2.0, (y0 + cy) / 2.0)
    p2 = ((cx + x3) / 2.0, (cy + y3) / 2.0)
    p3 = (float(x3), float(y3))

    points: list[tuple[int, int]] = []
    actual_steps = max(10, min(steps, int(dist / 14) + 12))
    for i in range(actual_steps + 1):
        t = i / actual_steps
        t_eased = t * t * (3.0 - 2.0 * t)
        x = (1 - t_eased)**3 * p0[0] + 3 * (1 - t_eased)**2 * t_eased * p1[0] + 3 * (1 - t_eased) * t_eased**2 * p2[0] + t_eased**3 * p3[0]
        y = (1 - t_eased)**3 * p0[1] + 3 * (1 - t_eased)**2 * t_eased * p1[1] + 3 * (1 - t_eased) * t_eased**2 * p2[1] + t_eased**3 * p3[1]
        points.append((int(round(x)), int(round(y))))
    return points


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
        if self.name == "wayland":
            try:
                with self.screenshot() as image:
                    return image.size
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                pass
        return None

    def current_cursor(self) -> tuple[int, int]:
        if hasattr(self.backend, "cursor_position"):
            try:
                pos = self.backend.cursor_position()
                if pos is not None:
                    return int(pos[0]), int(pos[1])
            except Exception:
                pass
        return (0, 0)

    # ---- steps -----------------------------------------------------------------
    def perform(self, step, cancel: threading.Event | None = None):
        kind = step["type"]
        if kind == "click":
            self.click(*self._xy(step["x"], step["y"]), step.get("button", "left"), step.get("count", 1))
        elif kind == "human_click":
            self.human_click(*self._xy(step["x"], step["y"]), step.get("button", "left"), cancel=cancel)
        elif kind == "human_move":
            self.human_move(*self._xy(step["x"], step["y"]), duration=step.get("duration"), cancel=cancel)
        elif kind == "drag":
            self.drag([self._xy(x, y) for x, y in step["points"]], step.get("button", "left"))
        elif kind == "type":
            self.backend.type_text(step["text"], cancel)
        elif kind == "human_type":
            self.human_type(step["text"], cancel=cancel)
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

    def human_move(self, x: int, y: int, duration: float | None = None, steps: int = 25, cancel: threading.Event | None = None) -> None:
        """Smoothly move mouse cursor to (x, y) along a natural curved trajectory."""
        start = self.current_cursor()
        target = self._xy(x, y)
        path = generate_human_path(start, target, steps=steps)
        if not path:
            return
        total_time = duration if duration is not None else max(0.12, min(0.45, math.hypot(target[0] - start[0], target[1] - start[1]) / 2500.0 + 0.15))
        delay = total_time / max(1, len(path))
        for px, py in path:
            if cancel and cancel.is_set():
                return
            if self.name == "windows":
                if hasattr(self.backend, "set_cursor_position"):
                    self.backend.set_cursor_position(px, py)
                elif getattr(self.backend, "mouse_controller", None):
                    self.backend.mouse_controller.position = (px, py)
            elif self.name == "x11":
                self.backend.cmd("mousemove", px, py)
            elif self.name == "wayland":
                self.backend._run("mousemove", px, py)
            if cancel:
                cancel.wait(delay)
            else:
                time.sleep(delay)

    def human_click(self, x: int | None = None, y: int | None = None, button: str = "left", cancel: threading.Event | None = None) -> None:
        """Move human-like to coordinates (if given) and click with realistic human button hold timing."""
        if cancel and cancel.is_set():
            return
        if x is not None and y is not None:
            self.human_move(x, y, cancel=cancel)
        time.sleep(random.uniform(0.04, 0.08))
        if self.name == "windows":
            btn = getattr(self.backend.mouse.Button, button, self.backend.mouse.Button.left)
            self.backend.mouse_controller.press(btn)
        elif self.name == "x11":
            self.backend.cmd("mousedown", X11_BUTTON.get(button, "1"))
        elif self.name == "wayland":
            self.backend._run("click", X11_BUTTON.get(button, "1"))
        time.sleep(random.uniform(0.05, 0.09))
        if self.name == "windows":
            btn = getattr(self.backend.mouse.Button, button, self.backend.mouse.Button.left)
            self.backend.mouse_controller.release(btn)
        elif self.name == "x11":
            self.backend.cmd("mouseup", X11_BUTTON.get(button, "1"))
        time.sleep(random.uniform(0.03, 0.06))

    def human_type(self, text: str, min_delay: float = 0.03, max_delay: float = 0.08, cancel: threading.Event | None = None) -> None:
        """Type text with realistic human typing cadence."""
        for char in str(text):
            if cancel and cancel.is_set():
                return
            if hasattr(self.backend, "type_text"):
                self.backend.type_text(char, cancel)
            elif hasattr(self.backend, "keyboard_controller") and self.backend.keyboard_controller:
                self.backend.keyboard_controller.type(char)
            time.sleep(random.uniform(min_delay, max_delay))

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
    def screenshot(self, bbox: tuple[int, int, int, int] | None = None, path: str | Path | None = None):
        """Screen capture as a PIL image, with optional bounding box and saving."""
        try:
            from PIL import Image, ImageGrab
        except ImportError as error:
            raise RuntimeError("Install Pillow to let the assistant see the screen: pip install 'input-reply[ai]'") from error

        if sys.platform == "win32" and hasattr(self.backend, "_ensure_desktop"):
            try:
                self.backend._ensure_desktop()
            except Exception:
                pass

        image = None
        if sys.platform == "win32" or self.name == "x11":
            try:
                display = getattr(self.backend, "display", None) if self.name == "x11" else None
                if bbox is not None:
                    image = ImageGrab.grab(bbox=bbox, xdisplay=display) if display else ImageGrab.grab(bbox=bbox)
                else:
                    image = ImageGrab.grab(xdisplay=display) if display else ImageGrab.grab()
                image = image.convert("RGB")
            except Exception:
                image = None

        if image is None:
            problems = []
            for tool, build in SCREENSHOT_TOOLS:
                if not shutil.which(tool):
                    continue
                with tempfile.TemporaryDirectory() as folder:
                    shot_path = str(Path(folder) / "shot.png")
                    try:
                        subprocess.run(build(shot_path), check=True, capture_output=True, timeout=20, env=self._env())
                        if Path(shot_path).stat().st_size:
                            with Image.open(shot_path) as loaded:
                                image = loaded.convert("RGB")
                                if bbox is not None:
                                    image = image.crop(bbox)
                                break
                    except (OSError, subprocess.SubprocessError) as error:
                        problems.append(f"{tool}: {error}")
            if image is None:
                raise RuntimeError("Could not capture the screen. Install Pillow, grim (Wayland), spectacle, or scrot. " + "; ".join(problems)[:300])

        if path:
            out = Path(path)
            out.parent.mkdir(parents=True, exist_ok=True)
            image.save(str(out))
        return image

    def _env(self):
        return getattr(self.backend, "env", None) if isinstance(getattr(self.backend, "env", None), dict) else None
