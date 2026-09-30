"""Recording validation, editable typing blocks, and timed replay."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
from collections import Counter, OrderedDict
from pathlib import Path
from threading import Event

FORMAT = "input-reply-v1"
LEGACY_FORMAT = "codex-x11-input-replay-v1"
KINDS = {"motion", "motion_delta", "scroll", "key_down", "key_up", "button_down", "button_up"}
PARAM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}\.json$")
BREAK_KEYS = {"enter", "return", "tab", "escape", "delete", "insert", "home", "end",
              "left", "right", "up", "down", "page_up", "page_down", "caps_lock",
              "num_lock", "scroll_lock", "f12"}
SHORTCUT_KEYS = {"ctrl", "ctrl_l", "ctrl_r", "control_l", "control_r", "alt", "alt_l",
                 "alt_r", "super", "super_l", "super_r", "cmd", "cmd_l", "cmd_r",
                 "meta_l", "meta_r", "hyper_l", "hyper_r"}
TEXT_NAMES = {"space", "backspace", "kp_space", "kp_decimal", "minus", "equal",
              "bracketleft", "bracketright", "semicolon", "apostrophe", "grave",
              "backslash", "comma", "period", "slash", "less", "greater", "plus",
              "asterisk", "underscore", "quotedbl", "colon", "question", "exclam",
              "at", "numbersign", "dollar", "percent", "asciicircum", "ampersand",
              "parenleft", "parenright", "braceleft", "braceright", "bar", "asciitilde"}


def data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "InputReply"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "input-reply"


UID_RE = re.compile(r"^[A-Za-z0-9]{1,128}$")
_account: str | None = None


def set_account(uid: str | None) -> None:
    """Scope recordings to the signed-in account; None uses the offline folder."""
    global _account
    if uid is not None and not UID_RE.fullmatch(uid):
        raise ValueError("Invalid account id")
    _account = uid


def current_account() -> str | None:
    return _account


def recordings_dir() -> Path:
    if _account:
        path = data_dir() / "accounts" / _account / "recordings"
    else:
        path = data_dir() / "recordings"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def adopt_legacy_recordings(uid: str) -> int:
    """Copy pre-login recordings into the first account that signs in on this computer."""
    marker = data_dir() / "accounts" / ".legacy-claimed"
    legacy = data_dir() / "recordings"
    if marker.exists() or not legacy.is_dir() or not UID_RE.fullmatch(uid):
        return 0
    target = data_dir() / "accounts" / uid / "recordings"
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    copied = 0
    for source in legacy.glob("*.json"):
        destination = target / source.name
        if NAME_RE.fullmatch(source.name) and not destination.exists():
            shutil.copy2(source, destination)
            copied += 1
    marker.write_text(uid, encoding="utf-8")
    return copied


def safe_path(name: str) -> Path:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError("Recording name must be a simple .json filename")
    return recordings_dir() / name


def new_name(stem: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", str(stem).strip()).strip("._-")[:60]
    stem = stem.removesuffix(".json") or "macro"
    proposed = stem + ".json"
    if not safe_path(proposed).exists():
        return proposed
    stamp = time.strftime("%Y%m%d-%H%M%S")
    index = 1
    while True:
        proposed = f"{stem}-{stamp}-{index}.json"
        if not safe_path(proposed).exists():
            return proposed
        index += 1


def validate_recording(data: dict) -> dict:
    if not isinstance(data, dict) or data.get("format") not in {FORMAT, LEGACY_FORMAT}:
        raise ValueError("Unsupported recording format")
    events = data.get("events")
    if not isinstance(events, list) or len(events) > 100000:
        raise ValueError("Invalid recording events")
    previous = -1.0
    for event in events:
        if not isinstance(event, dict) or event.get("type") not in KINDS:
            raise ValueError("Invalid input event")
        at = event.get("t")
        if not isinstance(at, (float, int)) or not math.isfinite(at) or not previous <= at <= 300:
            raise ValueError("Invalid event time")
        previous = at
        kind = event["type"]
        if kind.startswith("key_"):
            if data["format"] == LEGACY_FORMAT:
                if not isinstance(event.get("code"), int) or not 8 <= event["code"] <= 255:
                    raise ValueError("Invalid X11 keycode")
            elif not isinstance(event.get("key"), str) or len(event["key"]) > 80:
                raise ValueError("Invalid key")
        elif kind.startswith("button_"):
            if data["format"] == LEGACY_FORMAT:
                valid = isinstance(event.get("button"), int) and 1 <= event["button"] <= 32
            else:
                valid = isinstance(event.get("button"), str) and len(event["button"]) <= 40
            if not valid:
                raise ValueError("Invalid mouse button")
        if kind in {"motion", "button_down", "button_up", "motion_delta", "scroll"}:
            axes = ("dx", "dy") if kind in {"motion_delta", "scroll"} else ("x", "y")
            for axis in axes:
                value = event.get(axis)
                if not isinstance(value, int) or not -32768 <= value <= 32767:
                    raise ValueError("Invalid pointer coordinate")
    return data


_cache_lock = threading.Lock()
_parsed: OrderedDict = OrderedDict()   # path -> (signature, recording); small LRU of parsed files
_summaries: dict = {}                  # path -> (signature, catalog row)
PARSED_LIMIT = 8


def _signature(path: Path):
    info = path.stat()
    return info.st_mtime_ns, info.st_size


def read_recording(name: str, fresh: bool = False) -> dict:
    """Parse and validate a recording.

    Parsed files are cached by modification time. The returned dict is shared, so
    callers that change it must pass ``fresh=True`` to get a private copy.
    """
    path = safe_path(name)
    signature = _signature(path)
    if not fresh:
        with _cache_lock:
            hit = _parsed.get(path)
            if hit and hit[0] == signature:
                _parsed.move_to_end(path)
                return hit[1]
    data = validate_recording(json.loads(path.read_text(encoding="utf-8")))
    if not fresh:
        with _cache_lock:
            _parsed[path] = (signature, data)
            while len(_parsed) > PARSED_LIMIT:
                _parsed.popitem(last=False)
    return data


def write_recording(name: str, data: dict) -> None:
    validate_recording(data)
    destination = safe_path(name)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent,
                                     prefix=".recording-", delete=False) as out:
        temporary = Path(out.name)
        if os.name != "nt":
            os.fchmod(out.fileno(), 0o600)
        json.dump(data, out, separators=(",", ":"))
    os.replace(temporary, destination)
    with _cache_lock:
        _parsed.pop(destination, None)
        _summaries.pop(destination, None)


def delete_recording(name: str) -> str:
    path = safe_path(name)
    path.unlink()
    with _cache_lock:
        _parsed.pop(path, None)
        _summaries.pop(path, None)
    return path.name


def _summary(path: Path, signature) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_recording(data)
    counts = Counter(e["type"] for e in data["events"])
    return {"name": path.name, "duration": round(float(data.get("duration", 0)), 1),
            "recorded_at": data.get("recorded_at") or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(signature[0] / 1e9)),
            "keys": counts["key_down"], "clicks": counts["button_down"],
            "moves": counts["motion"],
            "target": (data.get("target_window") or {}).get("title", "Unknown window")}


def catalog() -> list[dict]:
    """Recording summaries, newest first. Unchanged files are not re-parsed."""
    rows = []
    for path in recordings_dir().glob("*.json"):
        try:
            signature = _signature(path)
            with _cache_lock:
                hit = _summaries.get(path)
            if not hit or hit[0] != signature:
                hit = (signature, _summary(path, signature))
                with _cache_lock:
                    _summaries[path] = hit
            rows.append((signature[0], hit[1]))
        except (ValueError, OSError, KeyError, json.JSONDecodeError):
            continue
    rows.sort(key=lambda row: row[0], reverse=True)
    return [row[1] for row in rows]


def _name(event: dict, mapping: dict[int, str] | None = None) -> str:
    if "key" in event:
        return event["key"].lower()
    return (mapping or {}).get(event.get("code"), "").lower()


def typing_blocks(data: dict, mapping: dict[int, str] | None = None) -> list[dict]:
    events = data["events"]
    blocks: list[dict] = []
    current: list[int] = []
    count = 0
    last_down = None
    shortcuts = set()

    def finish():
        nonlocal current, count, last_down
        if count and current:
            blocks.append({"block": len(blocks) + 1, "start": round(events[current[0]]["t"], 2),
                           "end": round(events[current[-1]]["t"], 2), "keys": count,
                           "indices": current[:]})
        current, count, last_down = [], 0, None

    for index, event in enumerate(events):
        kind = event["type"]
        if kind.startswith("button_"):
            finish()
            continue
        if not kind.startswith("key_"):
            continue
        name = _name(event, mapping)
        ident = event.get("code", event.get("key"))
        if name in SHORTCUT_KEYS:
            finish()
            if kind == "key_down":
                shortcuts.add(ident)
            else:
                shortcuts.discard(ident)
            continue
        if shortcuts:
            continue
        printable = event.get("text_key") if isinstance(event.get("text_key"), bool) else (
            len(name) == 1 or name in TEXT_NAMES or name.startswith("kp_") and name not in BREAK_KEYS)
        if name in BREAK_KEYS or not (printable or name.startswith("shift")):
            finish()
            continue
        if kind == "key_down" and last_down is not None and event["t"] - last_down > 1.5:
            finish()
        current.append(index)
        if kind == "key_down" and printable:
            count += 1
            last_down = event["t"]
    finish()
    return blocks


def definitions(data: dict, blocks: list[dict]) -> list[dict]:
    raw = data.get("parameters", [])
    if not isinstance(raw, list):
        raise ValueError("Invalid parameter definitions")
    by_block = {b["block"]: b for b in blocks}
    names, used = set(), set()
    result = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not PARAM_RE.fullmatch(item["name"]):
            raise ValueError("Invalid parameter name")
        name, block = item["name"], by_block.get(item.get("block"))
        if not block or name in names or used.intersection(block["indices"]):
            raise ValueError("Parameter refers to an unavailable typing block")
        names.add(name)
        used.update(block["indices"])
        result.append({"name": name, "block": block["block"]})
    return result


def inspect(name: str, mapping: dict[int, str] | None = None) -> dict:
    data = read_recording(name)
    blocks = typing_blocks(data, mapping)
    params = definitions(data, blocks)
    assigned = {p["block"]: p["name"] for p in params}
    return {"name": name, "target": (data.get("target_window") or {}).get("title", "Unknown window"),
            "duration": data.get("duration", 0), "parameters": params,
            "blocks": [{k: v for k, v in block.items() if k != "indices"} |
                       {"parameter": assigned.get(block["block"])} for block in blocks]}


def add_parameter(name: str, block_number: int, param: str, mapping=None) -> dict:
    if not isinstance(param, str) or not PARAM_RE.fullmatch(param):
        raise ValueError("Parameter name must start with a letter or underscore and use letters, numbers, or underscores")
    data = read_recording(name, fresh=True)
    blocks = typing_blocks(data, mapping)
    prior = definitions(data, blocks)
    if block_number not in {b["block"] for b in blocks}:
        raise ValueError("Typing block does not exist")
    if any(p["name"] == param or p["block"] == block_number for p in prior):
        raise ValueError("Parameter name or typing block is already used")
    data["parameters"] = prior + [{"name": param, "block": block_number}]
    write_recording(name, data)
    return inspect(name, mapping)


def remove_parameter(name: str, param: str, mapping=None) -> dict:
    data = read_recording(name, fresh=True)
    prior = definitions(data, typing_blocks(data, mapping))
    if not any(p["name"] == param for p in prior):
        raise ValueError("Parameter does not exist")
    data["parameters"] = [p for p in prior if p["name"] != param]
    write_recording(name, data)
    return inspect(name, mapping)


def substitute(data: dict, values: dict[str, str], mapping=None) -> list[dict]:
    if not isinstance(values, dict):
        raise ValueError("Parameter values must be a JSON object")
    blocks = typing_blocks(data, mapping)
    params = definitions(data, blocks)
    names = {p["name"] for p in params}
    for name, value in values.items():
        if name not in names:
            raise ValueError(f"Unknown parameter: {name}")
        if not isinstance(value, str) or len(value) > 4000 or "\x00" in value:
            raise ValueError(f"Parameter {name} must be text up to 4000 characters")
    by_block = {b["block"]: b for b in blocks}
    omit, additions = set(), {}
    for item in params:
        if item["name"] in values:
            indices = by_block[item["block"]]["indices"]
            omit.update(indices)
            additions[indices[0]] = {"type": "text", "text": values[item["name"]],
                                     "t": data["events"][indices[0]]["t"]}
    output = []
    for index, event in enumerate(data["events"]):
        if index in additions:
            output.append(additions[index])
        if index not in omit:
            output.append(event)
    return output


def normalize_repeats(events: list[dict], preserve=False) -> list[dict]:
    filtered = []
    held_since = {}
    index = 0
    while index < len(events):
        event = events[index]
        kind = event["type"]
        ident = event.get("code", event.get("key"))
        following = events[index + 1] if index + 1 < len(events) else None
        if (kind == "key_up" and following and following["type"] == "key_down"
                and following.get("code", following.get("key")) == ident
                and ident in held_since and event["t"] - held_since[ident] >= 0.15
                and following["t"] - event["t"] <= 0.035):
            index += 2
            continue
        if kind == "key_down":
            held_since.setdefault(ident, event["t"])
        elif kind == "key_up":
            held_since.pop(ident, None)
        filtered.append(event)
        index += 1

    output, pressed = [], {}
    for event in filtered:
        kind = event["type"]
        if kind == "text":
            output.append(event)
            continue
        if kind.startswith("key_"):
            key = event.get("code", event.get("key"))
            if kind == "key_down":
                if key in pressed:
                    continue
                pressed[key] = event
                output.append(event)
            else:
                down = pressed.pop(key, None)
                if down is None:
                    continue
                release = dict(event)
                if not preserve and _name(event) not in SHORTCUT_KEYS and not _name(event).startswith("shift"):
                    release["t"] = min(release["t"], down["t"] + 0.06)
                output.append(release)
        else:
            output.append(event)
    output.sort(key=lambda e: e["t"])
    return output


def play(events: list[dict], backend, cancel: Event | None = None, preserve=False) -> bool:
    events = normalize_repeats(events, preserve)
    held_keys, held_buttons = {}, {}
    started = time.monotonic()
    try:
        for event in events:
            if cancel and cancel.is_set():
                return False
            delay = started + event["t"] - time.monotonic()
            if delay > 0:
                if cancel:
                    if cancel.wait(delay):
                        return False
                else:
                    time.sleep(delay)
            kind = event["type"]
            if kind == "text":
                backend.type_text(event["text"], cancel)
                if cancel and cancel.is_set():
                    return False
            else:
                backend.emit(event)
                if kind == "key_down":
                    held_keys[event.get("code", event.get("key"))] = event
                elif kind == "key_up":
                    held_keys.pop(event.get("code", event.get("key")), None)
                elif kind == "button_down":
                    held_buttons[event["button"]] = event
                elif kind == "button_up":
                    held_buttons.pop(event["button"], None)
        return True
    finally:
        for event in held_keys.values():
            backend.emit(event | {"type": "key_up"})
        for event in held_buttons.values():
            backend.emit(event | {"type": "button_up"})
