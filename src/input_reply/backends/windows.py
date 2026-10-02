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
        self.kernel32 = ctypes.windll.kernel32
        self.user32.GetForegroundWindow.restype = ctypes.c_void_p
        self.user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        self.user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        self.user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        self.user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        self.user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.user32.ShowWindowAsync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.user32.IsWindow.argtypes = [ctypes.c_void_p]
        self.user32.IsIconic.argtypes = [ctypes.c_void_p]

    def _ensure_desktop(self):
        try:
            if hasattr(self.user32, "OpenInputDesktop") and hasattr(self.user32, "SetThreadDesktop"):
                hdesk = self.user32.OpenInputDesktop(0, False, 0x01FF)
                if hdesk:
                    self.user32.SetThreadDesktop(hdesk)
                    if hasattr(self.user32, "CloseDesktop"):
                        self.user32.CloseDesktop(hdesk)
        except Exception:
            pass

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
        self._ensure_desktop()
        ident = self.user32.GetForegroundWindow()
        if not ident:
            raise RuntimeError("Focus a desktop window before recording")
        return {"id": str(ident), "title": self._title(ident)}

    def windows(self):
        self._ensure_desktop()
        result = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        @callback_type
        def collect(hwnd, unused):
            if self.user32.IsWindowVisible(hwnd):
                title = self._title(hwnd)
                if title != "Untitled window" and title not in {"Program Manager", "Windows Input Experience"}:
                    result.append({"id": str(hwnd), "title": title})
            return True

        hdesk = None
        try:
            if hasattr(self.user32, "OpenInputDesktop"):
                hdesk = self.user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_ENUMERATE
            if hdesk and hasattr(self.user32, "EnumDesktopWindows"):
                self.user32.EnumDesktopWindows(hdesk, collect, 0)
        except Exception:
            pass
        finally:
            if hdesk and hasattr(self.user32, "CloseDesktop"):
                try:
                    self.user32.CloseDesktop(hdesk)
                except Exception:
                    pass

        if not result and hasattr(self.user32, "EnumWindows"):
            try:
                self.user32.EnumWindows.argtypes = [callback_type, ctypes.c_void_p]
                self.user32.EnumWindows(collect, None)
            except Exception:
                pass
        return result

    def focus(self, ident, timeout=15.0, cancel=None):
        if not str(ident).isdecimal() or int(ident) <= 0:
            raise ValueError("Invalid window ID")
        self._ensure_desktop()
        hwnd = ctypes.c_void_p(int(ident))
        if not self.user32.IsWindow(hwnd):
            self._ensure_desktop()
            if not self.user32.IsWindow(hwnd):
                raise RuntimeError("The selected window has closed. Refresh the window list and choose it again.")
        fg = self.user32.GetForegroundWindow()
        if fg == int(ident):
            return self.active_window()
        if self.user32.IsIconic(hwnd):
            self.user32.ShowWindowAsync(hwnd, 9)
        else:
            self.user32.ShowWindowAsync(hwnd, 5)

        kernel32 = getattr(self, "kernel32", None) or ctypes.windll.kernel32
        fore_thread = None
        if fg and hasattr(self.user32, "GetWindowThreadProcessId"):
            fore_thread = self.user32.GetWindowThreadProcessId(fg, None)
        curr_thread = kernel32.GetCurrentThreadId() if hasattr(kernel32, "GetCurrentThreadId") else None
        attached = False
        if fore_thread and curr_thread and fore_thread != curr_thread and hasattr(self.user32, "AttachThreadInput"):
            try:
                attached = bool(self.user32.AttachThreadInput(curr_thread, fore_thread, True))
            except Exception:
                attached = False
        try:
            if hasattr(self.user32, "BringWindowToTop"):
                self.user32.BringWindowToTop(hwnd)
            self.user32.SetForegroundWindow(hwnd)
        finally:
            if attached and hasattr(self.user32, "AttachThreadInput"):
                try:
                    self.user32.AttachThreadInput(curr_thread, fore_thread, False)
                except Exception:
                    pass

        # Activation is asynchronous and Windows may refuse it. Allow the user
        # to activate the target without capturing that click or sending input
        # elsewhere. Do not repeatedly steal focus while waiting.
        deadline = time.monotonic() + timeout
        while True:
            if self.user32.GetForegroundWindow() == int(ident):
                return self.active_window()
            if not self.user32.IsWindow(hwnd):
                self._ensure_desktop()
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
            except RuntimeError as err:
                if "closed" not in str(err).lower():
                    raise
            except (ValueError, OSError):
                pass
        open_wins = self.windows()
        if title:
            # 1. Exact title match
            for window in open_wins:
                if window["title"] == title:
                    return self.focus(window["id"], cancel=cancel)
            # 2. Similar title or application name match (e.g. "Notepad" or "Google Chrome")
            delimiters = [" - ", " — ", " | ", " · "]
            segments = []
            for d in delimiters:
                if d in title:
                    segments.extend([s.strip() for s in title.split(d) if len(s.strip()) >= 3])
            for seg in segments:
                for window in open_wins:
                    if seg.lower() in window["title"].lower() and not window["title"].startswith("Input Reply"):
                        try:
                            return self.focus(window["id"], cancel=cancel)
                        except (RuntimeError, ValueError):
                            pass

        # 3. Fall back to currently active desktop window if not Input Reply
        try:
            active = self.active_window()
            if active and not active["title"].startswith("Input Reply") and active.get("id") and str(active["id"]) != "0":
                return active
        except Exception:
            pass

        # 4. Open/focus another available desktop window
        candidates = [w for w in open_wins if not w["title"].startswith("Input Reply") and w.get("id")]
        for window in candidates:
            try:
                return self.focus(window["id"], cancel=cancel)
            except (RuntimeError, ValueError):
                pass

        raise RuntimeError("Recorded window is closed and no other open desktop windows were found. Open a window and retry.")

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

    def cursor_position(self):
        try:
            if self.mouse_controller:
                pos = self.mouse_controller.position
                if pos is not None:
                    return int(pos[0]), int(pos[1])
            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
            pt = POINT()
            if self.user32.GetCursorPos(ctypes.byref(pt)):
                return int(pt.x), int(pt.y)
        except Exception:
            pass
        return None

    def set_cursor_position(self, x, y):
        try:
            if self.mouse_controller:
                self.mouse_controller.position = (int(x), int(y))
            else:
                self.user32.SetCursorPos(int(x), int(y))
        except Exception:
            pass

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

        kl_holder = [None]

        def win32_event_filter(msg, data):
            # Prohibit F12 shortcuts in all apps by suppressing F12 keydown/keyup
            if getattr(data, "vkCode", None) == 0x7B:  # VK_F12
                if msg in (0x0100, 0x0104):  # WM_KEYDOWN, WM_SYSKEYDOWN
                    done.set()
                if kl_holder[0] is not None:
                    try:
                        kl_holder[0].suppress_event()
                    except Exception:
                        pass
            return True

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

        try:
            kl = keyboard.Listener(on_press=on_press, on_release=on_release,
                                   win32_event_filter=win32_event_filter)
        except Exception:
            kl = keyboard.Listener(on_press=on_press, on_release=on_release)
        kl_holder[0] = kl
        ml = mouse.Listener(on_move=on_move, on_click=on_click)
        with kl, ml:
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
