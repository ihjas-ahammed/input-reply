"""Start the Input Reply background service when the user logs in.

Input injection needs a live graphical session, so "on boot" means "when the
desktop session starts". Each platform uses its native per-user mechanism and
needs no administrator rights.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import sys
from pathlib import Path

from . import core

APP_NAME = "Input Reply"
RUN_VALUE = "InputReply"
MAC_LABEL = "com.inputreply.service"


def launch_command(*args: str) -> list[str]:
    """Command line that starts this app, for source installs and frozen builds."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    executable = sys.executable
    if os.name == "nt":
        pythonw = Path(executable).with_name("pythonw.exe")
        if pythonw.exists():
            executable = str(pythonw)
    return [executable, "-m", "input_reply", *args]


def _linux_entry() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "input-reply.desktop"


def _mac_entry() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"


def _legacy_windows_entry() -> Path:
    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Input Reply.bat"


def entry_path() -> Path | None:
    """File that represents the startup entry, or None on Windows (registry)."""
    if os.name == "nt":
        return None
    return _mac_entry() if sys.platform == "darwin" else _linux_entry()


def is_installed() -> bool:
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                winreg.QueryValueEx(key, RUN_VALUE)
            return True
        except OSError:
            return False
    return entry_path().exists()


def install(host: str = "127.0.0.1", port: int = 8765) -> str:
    command = launch_command("service", "--host", host, "--port", str(port))
    if os.name == "nt":
        import subprocess
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, subprocess.list2cmdline(command))
        _legacy_windows_entry().unlink(missing_ok=True)
        return r"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run\InputReply"
    path = entry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        with path.open("wb") as out:
            plistlib.dump({"Label": MAC_LABEL, "ProgramArguments": command, "RunAtLoad": True,
                           "KeepAlive": {"SuccessfulExit": False}, "ProcessType": "Interactive"}, out)
        return str(path)
    launcher = core.data_dir() / "start-service.sh"
    launcher.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    launcher.write_text("#!/bin/sh\nexec " + shlex.join(command) + "\n", encoding="utf-8")
    launcher.chmod(0o700)
    quoted = '"' + str(launcher).replace("\\", "\\\\").replace('"', '\\"') + '"'
    path.write_text("[Desktop Entry]\nType=Application\n"
                    f"Name={APP_NAME}\nComment=Input Reply background service\n"
                    f"Exec={quoted}\nTerminal=false\nX-GNOME-Autostart-enabled=true\n"
                    "X-GNOME-Autostart-Delay=3\n", encoding="utf-8")
    return str(path)


def remove() -> str:
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                                0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_VALUE)
        except OSError:
            pass
        _legacy_windows_entry().unlink(missing_ok=True)
        return "Run registry entry"
    path = entry_path()
    path.unlink(missing_ok=True)
    return str(path)
