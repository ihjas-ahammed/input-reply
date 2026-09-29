"""Select the desktop integration for the current interactive session."""

import os
import sys


def select_backend():
    requested = os.environ.get("INPUT_REPLY_BACKEND", "auto").lower()
    if requested == "auto":
        if sys.platform == "win32":
            requested = "windows"
        elif os.environ.get("XDG_SESSION_TYPE", "").lower() == "x11":
            requested = "x11"
        elif os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
            requested = "wayland"
        else:
            requested = "x11"
    if requested == "windows":
        if sys.platform != "win32":
            raise RuntimeError("Windows backend requires Windows")
        from .windows import WindowsBackend
        return WindowsBackend()
    if requested == "x11":
        if not sys.platform.startswith("linux"):
            raise RuntimeError("X11 backend requires Linux")
        from .x11 import X11Backend
        return X11Backend()
    if requested == "wayland":
        if not sys.platform.startswith("linux"):
            raise RuntimeError("Wayland backend requires Linux")
        from .wayland import WaylandBackend
        return WaylandBackend()
    raise ValueError("INPUT_REPLY_BACKEND must be auto, windows, x11, or wayland")
