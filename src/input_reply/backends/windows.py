"""Windows global input capture and replay through pynput and Win32 focus APIs."""

import ctypes
import threading
import time


def _pynput():
    try:
        from pynput import keyboard, mouse
    except ImportError as error:
        raise RuntimeError("Install Input Reply with the Windows dependency: pip install input-reply") from error
    return keyboard, mouse


class WindowsBackend:
    name = "windows"

    def __init__(self):
        self.keyboard = None
        self.mouse = None
        self.keyboard_controller = None
        self.mouse_controller = None
        self.user32 = ctypes.windll.user32
        self.user32.GetForegroundWindow.restype = ctypes.c_void_p
        self.user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        self.user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        self.user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        self.user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        self.user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.user32.ShowWindowAsync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.user32.IsWindow.argtypes = [ctypes.c_void_p]
        self.user32.IsIconic.argtypes = [ctypes.c_void_p]

    def available(self):
        try:
            _pynput()
            return bool(self.user32.GetSystemMetrics(0) and self.user32.GetSystemMetrics(1))
        except (RuntimeError, OSError):
            return False

    def _title(self, ident):
        length = self.user32.GetWindowTextLengthW(ctypes.c_void_p(int(ident)))
        buf = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(ctypes.c_void_p(int(ident)), buf, length + 1)
        return buf.value[:180] or "Untitled window"

    def active_window(self):
        ident = self.user32.GetForegroundWindow()
        if not ident:
            raise RuntimeError("Focus a desktop window before recording")
        return {"id": str(ident), "title": self._title(ident)}

    def windows(self):
        result = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        @callback_type
        def collect(hwnd, unused):
            if self.user32.IsWindowVisible(hwnd):
                title = self._title(hwnd)
                if title != "Untitled window":
                    result.append({"id": str(hwnd), "title": title})
            return True

        self.user32.EnumWindows.argtypes = [callback_type, ctypes.c_void_p]
        self.user32.EnumWindows(collect, None)
        return result

    def focus(self, ident, timeout=15.0, cancel=None):
        if not str(ident).isdecimal() or int(ident) <= 0:
            raise ValueError("Invalid window ID")
        hwnd = ctypes.c_void_p(int(ident))
        if not self.user32.IsWindow(hwnd):
            raise RuntimeError("The selected window has closed. Refresh the window list and choose it again.")
        if self.user32.GetForegroundWindow() == int(ident):
            return self.active_window()
        if self.user32.IsIconic(hwnd):
            self.user32.ShowWindowAsync(hwnd, 9)
        self.user32.SetForegroundWindow(hwnd)
        # Activation is asynchronous and Windows may refuse it. Allow the user
        # to activate the target without capturing that click or sending input
        # elsewhere. Do not repeatedly steal focus while waiting.
        deadline = time.monotonic() + timeout
        while True:
            if self.user32.GetForegroundWindow() == int(ident):
                return self.active_window()
            if not self.user32.IsWindow(hwnd):
                raise RuntimeError("The selected window has closed. Refresh the window list and choose it again.")
            if cancel is not None and cancel.is_set():
                return None
            if time.monotonic() >= deadline:
                raise RuntimeError("The target window was not activated. Retry and click the selected window during the countdown or the focus wait.")
            if cancel is not None:
                cancel.wait(0.1)
            else:
                time.sleep(0.1)

    def focus_recorded(self, data, override=None, cancel=None):
        if override:
            return self.focus(override, cancel=cancel)
        target = data.get("target_window") or {}
        ident, title = target.get("id"), target.get("title")
        if ident:
            try:
                if self.user32.IsWindow(ctypes.c_void_p(int(ident))) and (not title or self._title(ident) == title):
                    return self.focus(ident, cancel=cancel)
            except (ValueError, OSError):
                pass
        if title:
            for window in self.windows():
                if window["title"] == title:
                    return self.focus(window["id"], cancel=cancel)
        raise RuntimeError("Recorded window is closed. Choose another open window.")

    @staticmethod
    def _key_event(key, kind, at):
        if getattr(key, "name", None):
            name = key.name
        else:
            name = getattr(key, "char", None) or "unknown"
        event = {"type": kind, "key": str(name), "t": round(at, 4)}
        vk = getattr(key, "vk", None)
        if isinstance(vk, int):
            event["vk"] = vk
        return event

    def record(self, seconds, output, cancel):
        keyboard, mouse = _pynput()
        events = []
        lock = threading.Lock()
        done = threading.Event()
        start = time.monotonic()
        last_motion = 0.0

        def append(event):
            with lock:
                events.append(event)

        def on_press(key):
            if key == keyboard.Key.f12:
                done.set()
                return False
            append(self._key_event(key, "key_down", time.monotonic() - start))

        def on_release(key):
            if key != keyboard.Key.f12:
                append(self._key_event(key, "key_up", time.monotonic() - start))

        def on_move(x, y):
            nonlocal last_motion
            at = time.monotonic() - start
            if at - last_motion >= 0.02:
                last_motion = at
                append({"type": "motion", "x": int(x), "y": int(y), "t": round(at, 4)})

        def on_click(x, y, button, pressed):
            append({"type": "button_down" if pressed else "button_up", "button": button.name,
                    "x": int(x), "y": int(y), "t": round(time.monotonic() - start, 4)})

        with keyboard.Listener(on_press=on_press, on_release=on_release) as kl, \
             mouse.Listener(on_move=on_move, on_click=on_click) as ml:
            while not done.is_set() and time.monotonic() - start < seconds:
                if cancel and cancel.wait(0.05):
                    break
                if not cancel:
                    time.sleep(0.05)
            kl.stop()
            ml.stop()
        with lock:
            ordered = sorted(events, key=lambda e: e["t"])
        return {"format": "input-reply-v1", "backend": self.name,
                "duration": round(min(time.monotonic() - start, seconds), 3), "events": ordered}

    def open_player(self):
        self.keyboard, self.mouse = _pynput()
        self.keyboard_controller = self.keyboard.Controller()
        self.mouse_controller = self.mouse.Controller()
        return self

    def close_player(self):
        self.keyboard_controller = None
        self.mouse_controller = None

    def _key(self, event):
        name = event["key"]
        if hasattr(self.keyboard.Key, name):
            return getattr(self.keyboard.Key, name)
        if isinstance(event.get("vk"), int):
            return self.keyboard.KeyCode.from_vk(event["vk"])
        return self.keyboard.KeyCode.from_char(name)

    def emit(self, event):
        kind = event["type"]
        if kind == "motion":
            self.mouse_controller.position = (event["x"], event["y"])
        elif kind.startswith("button_"):
            self.mouse_controller.position = (event["x"], event["y"])
            button = getattr(self.mouse.Button, event["button"])
            if kind == "button_down":
                self.mouse_controller.press(button)
            else:
                self.mouse_controller.release(button)
        else:
            key = self._key(event)
            if kind == "key_down":
                self.keyboard_controller.press(key)
            else:
                self.keyboard_controller.release(key)

    def type_text(self, value, cancel=None):
        for char in value:
            if cancel and cancel.is_set():
                return
            self.keyboard_controller.type(char)
