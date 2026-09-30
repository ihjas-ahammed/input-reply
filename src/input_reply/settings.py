"""Small per-machine settings file kept next to the recordings."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from . import core

_LOCK = threading.Lock()
DEFAULTS = {"remote_enabled": True, "autostart_configured": False,
            # Experimental Gemini Live assistant: off until the user enables it in Settings.
            "ai_enabled": False, "ai_voice": True, "ai_model": "gemini-3.8-flash-live"}


def path() -> Path:
    return core.data_dir() / "settings.json"


def load() -> dict:
    try:
        stored = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    return DEFAULTS | (stored if isinstance(stored, dict) else {})


def update(**values) -> dict:
    with _LOCK:
        current = load() | values
        destination = path()
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent,
                                         prefix=".settings-", delete=False) as out:
            temporary = Path(out.name)
            json.dump(current, out)
        os.replace(temporary, destination)
        return current
