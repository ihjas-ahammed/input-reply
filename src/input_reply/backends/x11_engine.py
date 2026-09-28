#!/usr/bin/env python3
"""Record and replay short X11 keyboard/mouse sessions.

Requires xinput, xmodmap, libX11, and libXtst. Recordings contain raw keycodes,
which can reveal typed text. Only record nonsensitive actions and protect files.
"""

import argparse
import ctypes
import ctypes.util
import json
import os
import re
import selectors
import subprocess
import time
from collections import Counter
from pathlib import Path


EVENT = re.compile(r"^EVENT type \d+ \((\w+)\)")
DETAIL = re.compile(r"^\s*detail:\s*(\d+)")
XTIME = re.compile(r"^\s*time:\s*(\d+)")
KEYMAP = re.compile(r"^keycode\s+(\d+)\s+=\s*(\S+)")
KINDS = {"motion", "key_down", "key_up", "button_down", "button_up"}
MODIFIER_NAMES = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
                  "Meta_L", "Meta_R", "Super_L", "Super_R", "Hyper_L", "Hyper_R",
                  "ISO_Level3_Shift", "Mode_switch"}


class X11:
    def __init__(self):
        self.xlib = ctypes.CDLL(ctypes.util.find_library("X11"))
        self.xtst = ctypes.CDLL(ctypes.util.find_library("Xtst"))
        self.xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.xlib.XOpenDisplay.restype = ctypes.c_void_p
        self.xlib.XDefaultScreen.argtypes = [ctypes.c_void_p]
        self.xlib.XDefaultScreen.restype = ctypes.c_int
        self.xlib.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.xlib.XRootWindow.restype = ctypes.c_ulong
        self.xlib.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint)]
        self.xlib.XQueryPointer.restype = ctypes.c_int
        self.xlib.XFlush.argtypes = [ctypes.c_void_p]
        self.xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                  ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
        self.xtst.XTestFakeMotionEvent.restype = ctypes.c_int
        self.xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                               ctypes.c_int, ctypes.c_ulong]
        self.xtst.XTestFakeKeyEvent.restype = ctypes.c_int
        self.xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                                  ctypes.c_int, ctypes.c_ulong]
        self.xtst.XTestFakeButtonEvent.restype = ctypes.c_int
        self.display = self.xlib.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError("Cannot open X11 display")
        self.screen = self.xlib.XDefaultScreen(self.display)
        self.root = self.xlib.XRootWindow(self.display, self.screen)

    def pointer(self):
        root = ctypes.c_ulong()
        child = ctypes.c_ulong()
        rx = ctypes.c_int()
        ry = ctypes.c_int()
        wx = ctypes.c_int()
        wy = ctypes.c_int()
        mask = ctypes.c_uint()
        ok = self.xlib.XQueryPointer(self.display, self.root, ctypes.byref(root),
            ctypes.byref(child), ctypes.byref(rx), ctypes.byref(ry),
            ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask))
        if not ok:
            raise RuntimeError("Cannot read pointer position")
        return rx.value, ry.value

    def emit(self, event):
        kind = event["type"]
        if kind == "motion":
            ok = self.xtst.XTestFakeMotionEvent(self.display, self.screen,
                                                event["x"], event["y"], 0)
        elif kind.startswith("key_"):
            ok = self.xtst.XTestFakeKeyEvent(self.display, event["code"],
                                             int(kind == "key_down"), 0)
        else:
            ok = self.xtst.XTestFakeButtonEvent(self.display, event["button"],
                                                int(kind == "button_down"), 0)
        if not ok:
            raise RuntimeError(f"X11 rejected {kind}")
        self.xlib.XFlush(self.display)

    def close(self):
        if self.display:
            self.xlib.XCloseDisplay(self.display)
            self.display = None


def keyboard_map():
    output = subprocess.check_output(["xmodmap", "-pke"], text=True)
    mapping = {}
    for line in output.splitlines():
        match = KEYMAP.match(line)
        if match:
            mapping[int(match.group(1))] = match.group(2)
    return mapping


def stop_keycode():
    for code, name in keyboard_map().items():
        if name == "F12":
            return code
    raise RuntimeError("F12 keycode not found")


def record(args):
    if args.seconds <= 0 or args.seconds > 300:
        raise ValueError("Recording duration must be between 0 and 300 seconds")
    x11 = X11()
    stop_code = stop_keycode()
    selector = selectors.DefaultSelector()
    processes = {}
    pending = {2: None, 3: None}
    buffers = {2: b"", 3: b""}
    seen = {}
    events = []
    started = time.monotonic()
    deadline = started + args.seconds
    stopped_by_f12 = False
    stop_xtime = None
    interrupted = False
    interrupted_at = None

    def add(event):
        event["t"] = round(time.monotonic() - started, 4)
        events.append(event)

    def handle(device, raw):
        nonlocal stopped_by_f12, stop_xtime
        if not raw or "detail" not in raw:
            return
        kind, code = raw["kind"], raw["detail"]
        if stop_xtime is not None and raw.get("xtime", 0) > stop_xtime:
            return
        fingerprint = (device, kind, code, raw.get("xtime"))
        if fingerprint in seen:
            return
        seen[fingerprint] = time.monotonic()
        if len(seen) > 200:
            cutoff = time.monotonic() - 2
            for item, moment in list(seen.items()):
                if moment < cutoff:
                    del seen[item]
        if kind == "RawKeyPress" and code == stop_code:
            stopped_by_f12 = True
            stop_xtime = raw.get("xtime")
        elif kind in ("RawKeyPress", "RawKeyRelease") and code != stop_code:
            add({"type": "key_down" if kind == "RawKeyPress" else "key_up", "code": code})
        elif kind in ("RawButtonPress", "RawButtonRelease"):
            x, y = x11.pointer()
            add({"type": "button_down" if kind == "RawButtonPress" else "button_up",
                 "button": code, "x": x, "y": y})

    def parse_line(device, line):
        match = EVENT.match(line)
        if match:
            handle(device, pending[device])
            pending[device] = {"kind": match.group(1)}
        elif pending[device]:
            match = DETAIL.match(line)
            if match:
                pending[device]["detail"] = int(match.group(1))
            match = XTIME.match(line)
            if match:
                pending[device]["xtime"] = int(match.group(1))
        if pending[device] and not line.strip():
            handle(device, pending[device])
            pending[device] = None

    def read_available(timeout):
        for key, _ in selector.select(timeout=timeout):
            device = key.data
            chunk = os.read(key.fileobj.fileno(), 65536)
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            buffers[device] += chunk
            while b"\n" in buffers[device]:
                line, buffers[device] = buffers[device].split(b"\n", 1)
                parse_line(device, line.decode("utf-8", errors="replace"))

    try:
        for device in (2, 3):
            proc = subprocess.Popen(["stdbuf", "-oL", "xinput", "test-xi2", "--root", str(device)],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    bufsize=0)
            processes[device] = proc
            os.set_blocking(proc.stdout.fileno(), False)
            selector.register(proc.stdout, selectors.EVENT_READ, device)
        last_position = x11.pointer()
        add({"type": "motion", "x": last_position[0], "y": last_position[1]})
        print(f"Recording for up to {args.seconds:g} seconds. Press F12 to stop.", flush=True)
        while not stopped_by_f12 and time.monotonic() < deadline:
            read_available(0.02)
            position = x11.pointer()
            if position != last_position:
                add({"type": "motion", "x": position[0], "y": position[1]})
                last_position = position
    except KeyboardInterrupt:
        interrupted = True
        interrupted_at = time.monotonic()
    finally:
        # Drain events already queued by the server, including a release that
        # may arrive after the F12 event on the other device's pipe.
        drain_until = time.monotonic() + 0.15
        try:
            while time.monotonic() < drain_until:
                read_available(0.01)
            for device, raw in pending.items():
                handle(device, raw)
        except KeyboardInterrupt:
            pass
        for proc in processes.values():
            proc.terminate()
        for proc in processes.values():
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        selector.close()
        x11.close()

    events.sort(key=lambda e: e["t"])
    if interrupted and args.trim_end:
        cutoff = max(0, (interrupted_at or time.monotonic()) - started - args.trim_end)
        events = [e for e in events if e["t"] <= cutoff]
    data = {"format": "codex-x11-input-replay-v1", "duration": round(time.monotonic() - started, 4),
            "events": events}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(data, out, separators=(",", ":"))
    counts = Counter(e["type"] for e in events)
    print(json.dumps({"saved": str(args.output), "duration_seconds": data["duration"],
                      "stop_reason": "F12" if stopped_by_f12 else "Ctrl+C" if interrupted else "time_limit",
                      "counts": counts}, indent=2))


def load_recording(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != "codex-x11-input-replay-v1" or not isinstance(data.get("events"), list):
        raise ValueError("Not a recording made by this program")
    events = data["events"]
    if len(events) > 100000:
        raise ValueError("Recording has too many events")
    previous = -1
    for e in events:
        if not isinstance(e, dict) or e.get("type") not in KINDS:
            raise ValueError("Invalid event")
        t = e.get("t")
        if not isinstance(t, (float, int)) or not previous <= t <= 300:
            raise ValueError("Invalid event timing")
        previous = t
        kind = e["type"]
        if kind in ("motion", "button_down", "button_up"):
            if not all(isinstance(e.get(n), int) and -32768 <= e[n] <= 32767 for n in ("x", "y")):
                raise ValueError("Invalid pointer coordinate")
        if kind.startswith("key_") and not (isinstance(e.get("code"), int) and 8 <= e["code"] <= 255):
            raise ValueError("Invalid key code")
        if kind.startswith("button_") and not (isinstance(e.get("button"), int) and 1 <= e["button"] <= 32):
            raise ValueError("Invalid mouse button")
    return events


def prepare_replay(events, preserve_key_holds=False):
    """Remove keyboard auto-repeat and turn ordinary keys into short taps."""
    modifiers = {code for code, name in keyboard_map().items() if name in MODIFIER_NAMES}
    filtered = []
    pressed_since = {}
    index = 0
    while index < len(events):
        event = events[index]
        kind = event["type"]
        code = event.get("code")
        following = events[index + 1] if index + 1 < len(events) else None
        # X11 auto-repeat can appear as a release followed immediately by
        # another press of the same held key. Keep the original hold only.
        if (kind == "key_up" and following and following["type"] == "key_down"
                and following["code"] == code and code in pressed_since
                and event["t"] - pressed_since[code] >= 0.15
                and following["t"] - event["t"] <= 0.035):
            index += 2
            continue
        if kind == "key_down":
            pressed_since.setdefault(code, event["t"])
        elif kind == "key_up":
            pressed_since.pop(code, None)
        filtered.append(event)
        index += 1

    result = []
    down = {}
    for event in filtered:
        kind = event["type"]
        if kind == "key_down":
            down.setdefault(event["code"], event)
        elif kind == "key_up":
            press = down.pop(event["code"], None)
            if press is None:
                continue
            result.append(press)
            release_time = event["t"]
            if not preserve_key_holds and event["code"] not in modifiers:
                release_time = min(release_time, press["t"] + 0.06)
            result.append({"type": "key_up", "code": event["code"], "t": release_time})
        else:
            result.append(event)
    for code, press in down.items():
        result.append(press)
        result.append({"type": "key_up", "code": code, "t": press["t"] + 0.06})
    result.sort(key=lambda e: e["t"])
    return result


def replay(args):
    events = prepare_replay(load_recording(args.input), args.preserve_key_holds)
    counts = Counter(e["type"] for e in events)
    print(json.dumps({"events": len(events), "counts": counts,
                      "duration_seconds": events[-1]["t"] if events else 0}, indent=2), flush=True)
    if args.dry_run:
        return
    print(f"Replaying in {args.countdown:g} seconds. Focus the target window now.", flush=True)
    time.sleep(args.countdown)
    x11 = X11()
    held_keys = set()
    held_buttons = set()
    start = time.monotonic()
    try:
        for event in events:
            delay = start + event["t"] - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if event["type"] in ("button_down", "button_up"):
                x11.emit({"type": "motion", "x": event["x"], "y": event["y"]})
            x11.emit(event)
            if event["type"] == "key_down":
                held_keys.add(event["code"])
            elif event["type"] == "key_up":
                held_keys.discard(event["code"])
            elif event["type"] == "button_down":
                held_buttons.add(event["button"])
            elif event["type"] == "button_up":
                held_buttons.discard(event["button"])
    finally:
        for code in held_keys:
            x11.emit({"type": "key_up", "code": code})
        for button in held_buttons:
            x11.emit({"type": "button_up", "button": button})
        x11.close()
    print("Replay complete.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="Capture a new short session")
    rec.add_argument("--seconds", type=float, default=30)
    rec.add_argument("--output", type=Path, required=True)
    rec.add_argument("--trim-end", type=float, default=0, help="Remove final seconds when GUI Stop is used")
    play = sub.add_parser("replay", help="Repeat a saved session")
    play.add_argument("--input", type=Path, required=True)
    play.add_argument("--countdown", type=float, default=3)
    play.add_argument("--dry-run", action="store_true", help="Validate and summarize without replaying")
    play.add_argument("--preserve-key-holds", action="store_true", help="Allow held keys to repeat")
    args = parser.parse_args()
    if args.command == "record":
        if args.trim_end < 0 or args.trim_end > 2:
            parser.error("--trim-end must be between 0 and 2")
        record(args)
    else:
        if args.countdown < 0:
            parser.error("--countdown cannot be negative")
        replay(args)


if __name__ == "__main__":
    main()
