"""Service lock file, port resolution, and active instance discovery.

Ensures that if a non-input-reply application occupies the default port, Input Reply
selects an alternate free port, writes service.lock, and lets other sessions connect
to the running server.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import core

DEFAULT_PORT = 8765
LOCK_FILENAME = "service.lock"


def lock_file_path() -> Path:
    """Return the path to the service.lock file in the user data directory."""
    return core.data_dir() / LOCK_FILENAME


def read_service_lock() -> dict[str, Any] | None:
    """Read service.lock if present and valid JSON."""
    path = lock_file_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "port" in data:
            return data
    except Exception:
        pass
    return None


def write_service_lock(port: int, host: str = "127.0.0.1", pid: int | None = None) -> Path:
    """Record active service port, host, and PID into service.lock."""
    path = lock_file_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = {
        "port": int(port),
        "host": str(host),
        "pid": pid or os.getpid(),
        "app": "input-reply",
        "updated_at": time.time(),
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def clear_service_lock(only_if_pid: int | None = None) -> bool:
    """Delete service.lock, optionally only if it belongs to the current PID."""
    path = lock_file_path()
    if not path.is_file():
        return False
    try:
        if only_if_pid is not None:
            existing = read_service_lock()
            if existing and existing.get("pid") != only_if_pid:
                return False
        path.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def probe(port: int, host: str = "127.0.0.1") -> bool:
    """Check if an authentic Input Reply service is answering on host:port."""
    try:
        url = f"http://{host}:{port}/api/health"
        with urllib.request.urlopen(url, timeout=1.5) as response:
            payload = json.load(response)
            return isinstance(payload, dict) and payload.get("app") == "input-reply"
    except (OSError, ValueError, TimeoutError):
        return False


def find_available_port(start_port: int = DEFAULT_PORT, host: str = "127.0.0.1", max_tries: int = 100) -> int:
    """Find a port that can be bound to, starting from start_port."""
    for port in range(start_port, start_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue

    # Fallback to ephemeral port assigned by operating system
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def get_active_service_port(default: int = DEFAULT_PORT, host: str = "127.0.0.1") -> int:
    """Find the port of a currently active input-reply server, checking .lock first."""
    # 1. Check lock file
    info = read_service_lock()
    if info and "port" in info:
        try:
            lock_port = int(info["port"])
            lock_host = info.get("host", host)
            if probe(lock_port, lock_host):
                return lock_port
            # Stale lock: port is no longer running an active input-reply server
            clear_service_lock()
        except (TypeError, ValueError):
            clear_service_lock()

    # 2. Check default port
    if probe(default, host):
        return default

    # 3. If neither active, return default
    return default
