"""Desktop app: background service with a tray icon, plus a native window.

``service`` runs the local API and the cloud agent and lives in the tray. It is
what starts at login. ``window`` shows the dashboard in a native window
(pywebview) and falls back to the default browser when that is not installed.
``app`` is what a launcher runs: it starts the service if needed, then opens the window.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from . import autostart, settings
from .cloud import CloudService
from .lockfile import (DEFAULT_PORT, clear_service_lock, get_active_service_port,
                       probe, write_service_lock)
from .network import NetworkMonitor
from .server import access_token, make_server


def _spawn(*args: str) -> subprocess.Popen:
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        options["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return subprocess.Popen(autostart.launch_command(*args), **options)


def _icon():
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 12, 60, 52), radius=8, fill=(59, 130, 246, 255))
    for row, y in enumerate((23, 33, 43)):
        for column in range(4 if row < 2 else 1):
            x = 14 + column * 11 + (5 if row == 1 else 0)
            draw.rectangle((x, y, x + 6 if row == 2 else x + 3, y + 3), fill=(255, 255, 255, 255))
    return image


def _run_tray(port: int, cloud: CloudService) -> bool:
    """Show the tray icon until Quit. Returns False when no tray is available."""
    try:
        import pystray
        image = _icon()
    except Exception:
        return False
    window: list[subprocess.Popen] = []

    def open_window(*_):
        if not window or window[0].poll() is not None:
            window[:] = [_spawn("window", "--port", str(port))]

    def toggle_remote(icon, item):
        cloud.set_remote(not settings.load()["remote_enabled"])

    def toggle_autostart(icon, item):
        if autostart.is_installed():
            autostart.remove()
        else:
            autostart.install("127.0.0.1", port)
        settings.update(autostart_configured=True)

    def quit_app(icon, item):
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open Input Reply", open_window, default=True),
        pystray.MenuItem("Remote control", toggle_remote, checked=lambda item: settings.load()["remote_enabled"]),
        pystray.MenuItem("Start at login", toggle_autostart, checked=lambda item: autostart.is_installed()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app))
    try:
        pystray.Icon("input-reply", image, "Input Reply", menu).run()
    except Exception:
        return False
    return True


def run_service(host: str = "127.0.0.1", port: int = DEFAULT_PORT, tray: bool = True, enable_autostart: bool = True):
    active_port = get_active_service_port(default=port, host=host)
    if probe(active_port, host):
        print(f"Input Reply is already running on port {active_port}", flush=True)
        return
    server = make_server(host, port)
    actual_port = getattr(server, "actual_port", port)
    cloud = CloudService(server.backend, server.state)
    server.cloud = cloud
    cloud.start_saved()
    if enable_autostart and not settings.load()["autostart_configured"]:
        # First launch after install: register startup at login. The user can turn it off from the tray or UI.
        try:
            autostart.install(host, actual_port)
        finally:
            settings.update(autostart_configured=True)

    def on_net_change(old_state, new_state):
        print(f"[Network] Network configuration changed. Reconnecting cloud...", flush=True)
        try:
            cloud.reconnect()
        except Exception:
            pass

    net_mon = NetworkMonitor(on_change=on_net_change)
    net_mon.start()

    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
    print(f"Input Reply service on {host}:{actual_port} ({server.backend.name})", flush=True)
    try:
        if not (tray and _run_tray(actual_port, cloud)):
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        net_mon.stop()
        clear_service_lock(only_if_pid=os.getpid())
        server.state.stop()
        cloud.shutdown()
        server.shutdown()
        server.server_close()


class DesktopBridge:
    """Let a user click in the foreground UI authorize service activation."""

    def __init__(self, port):
        self._port = port

    def allow_focus(self):
        if os.name != "nt":
            return False
        request = urllib.request.Request(
            f"http://127.0.0.1:{self._port}/api/desktop-process",
            headers={"Authorization": f"Bearer {access_token()}"})
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                pid = json.load(response)["pid"]
            if not isinstance(pid, int) or not 0 < pid < 0xFFFFFFFF:
                return False
            allow = ctypes.windll.user32.AllowSetForegroundWindow
            allow.argtypes = [ctypes.c_uint32]
            allow.restype = ctypes.c_int
            return bool(allow(pid))
        except (OSError, ValueError, KeyError):
            return False


def run_window(port: int = DEFAULT_PORT):
    actual_port = get_active_service_port(default=DEFAULT_PORT) if port == DEFAULT_PORT else port
    url = f"http://127.0.0.1:{actual_port}/#token={access_token()}"   # fragment stays in the client; the page removes it
    try:
        import webview
    except ImportError:
        print("pywebview is not installed (pip install 'input-reply[desktop]'); opening your browser instead.")
        webbrowser.open(url)
        return
    webview.create_window("Input Reply", url, width=1200, height=820, min_size=(420, 560),
                          js_api=DesktopBridge(actual_port))
    webview.start()


def open_app(port: int = DEFAULT_PORT):
    actual_port = get_active_service_port(default=DEFAULT_PORT) if port == DEFAULT_PORT else port
    if not probe(actual_port):
        _spawn("service", "--port", str(actual_port))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            current = get_active_service_port(default=actual_port)
            if probe(current):
                actual_port = current
                break
            time.sleep(0.25)
        if not probe(actual_port):
            raise RuntimeError("The Input Reply service did not start. Run: input-reply service")
    run_window(actual_port)
