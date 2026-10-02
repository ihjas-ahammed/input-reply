"""Dynamic Python scripting for Input Reply macros.

Allows converting recorded macros (events/steps) into clean, editable Python scripts
where users can add conditionals (e.g. ``if params.get("status") == "ok": ...``),
loops, data transformations, and custom automation logic.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import core


class MacroContext:
    """Execution context passed to macro run(context) functions."""

    def __init__(self, backend, params: dict | None = None, speed: float = 1.0, cancel: threading.Event | None = None):
        self.backend = backend
        self.params = dict(params or {})
        self.speed = float(speed) if speed and float(speed) > 0 else 1.0
        self.cancel = cancel
        self._opened_player = False

    def is_cancelled(self) -> bool:
        return bool(self.cancel and self.cancel.is_set())

    def sleep(self, seconds: float) -> None:
        """Sleep for the specified duration (scaled inversely by playback speed)."""
        if self.is_cancelled():
            return
        duration = max(0.0, float(seconds) / self.speed)
        if self.cancel:
            self.cancel.wait(duration)
        else:
            time.sleep(duration)

    def mouse_move(self, x: int, y: int) -> None:
        """Move cursor to screen coordinates (x, y)."""
        if self.is_cancelled():
            return
        x, y = int(x), int(y)
        if hasattr(self.backend, "set_cursor_position"):
            self.backend.set_cursor_position(x, y)
        elif hasattr(self.backend, "mouse_controller") and self.backend.mouse_controller:
            self.backend.mouse_controller.position = (x, y)
        elif hasattr(self.backend, "emit"):
            self.backend.emit({"type": "motion", "x": x, "y": y})

    def mouse_down(self, button: str = "left") -> None:
        """Press mouse button down ('left', 'right', 'middle')."""
        if self.is_cancelled():
            return
        btn_name = str(button).lower()
        if hasattr(self.backend, "mouse_controller") and self.backend.mouse_controller:
            from pynput.mouse import Button
            b = Button.right if btn_name == "right" else (Button.middle if btn_name == "middle" else Button.left)
            self.backend.mouse_controller.press(b)
        elif hasattr(self.backend, "emit"):
            self.backend.emit({"type": "button_down", "button": btn_name})

    def mouse_up(self, button: str = "left") -> None:
        """Release mouse button ('left', 'right', 'middle')."""
        if self.is_cancelled():
            return
        btn_name = str(button).lower()
        if hasattr(self.backend, "mouse_controller") and self.backend.mouse_controller:
            from pynput.mouse import Button
            b = Button.right if btn_name == "right" else (Button.middle if btn_name == "middle" else Button.left)
            self.backend.mouse_controller.release(b)
        elif hasattr(self.backend, "emit"):
            self.backend.emit({"type": "button_up", "button": btn_name})

    def click(self, x: int | None = None, y: int | None = None, button: str = "left") -> None:
        """Click at coordinates (x, y) or at current cursor location."""
        if self.is_cancelled():
            return
        if x is not None and y is not None:
            self.mouse_move(x, y)
            self.sleep(0.04)
        self.mouse_down(button)
        self.sleep(0.04)
        self.mouse_up(button)

    def double_click(self, x: int | None = None, y: int | None = None, button: str = "left") -> None:
        """Double click at coordinates (x, y)."""
        if self.is_cancelled():
            return
        self.click(x, y, button)
        self.sleep(0.08)
        self.click(x, y, button)

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> None:
        """Drag mouse from (x1, y1) to (x2, y2)."""
        if self.is_cancelled():
            return
        self.mouse_move(x1, y1)
        self.sleep(0.05)
        self.mouse_down(button)
        self.sleep(0.05)
        self.mouse_move(x2, y2)
        self.sleep(0.05)
        self.mouse_up(button)

    def type_text(self, text: str) -> None:
        """Type text into the focused window."""
        if self.is_cancelled() or not text:
            return
        text_str = str(text)
        if hasattr(self.backend, "type_text"):
            self.backend.type_text(text_str, self.cancel)
        elif hasattr(self.backend, "keyboard_controller") and self.backend.keyboard_controller:
            self.backend.keyboard_controller.type(text_str)
        elif hasattr(self.backend, "emit"):
            self.backend.emit({"type": "text", "text": text_str})

    def key_down(self, key: str) -> None:
        """Press a keyboard key down."""
        if self.is_cancelled():
            return
        k_str = str(key).lower()
        if hasattr(self.backend, "keyboard_controller") and self.backend.keyboard_controller:
            from pynput.keyboard import Key, KeyCode
            k = getattr(Key, k_str, None) or KeyCode.from_char(k_str)
            self.backend.keyboard_controller.press(k)
        elif hasattr(self.backend, "emit"):
            self.backend.emit({"type": "key_down", "key": k_str})

    def key_up(self, key: str) -> None:
        """Release a keyboard key."""
        if self.is_cancelled():
            return
        k_str = str(key).lower()
        if hasattr(self.backend, "keyboard_controller") and self.backend.keyboard_controller:
            from pynput.keyboard import Key, KeyCode
            k = getattr(Key, k_str, None) or KeyCode.from_char(k_str)
            self.backend.keyboard_controller.release(k)
        elif hasattr(self.backend, "emit"):
            self.backend.emit({"type": "key_up", "key": k_str})

    def press_key(self, key: str) -> None:
        """Press and release a key (e.g. 'enter', 'tab', 'backspace', 'escape')."""
        if self.is_cancelled():
            return
        self.key_down(key)
        self.sleep(0.04)
        self.key_up(key)

    def hotkey(self, *keys: str) -> None:
        """Press a key chord in order and release in reverse (e.g. 'ctrl', 'c')."""
        if self.is_cancelled():
            return
        for k in keys:
            self.key_down(k)
            self.sleep(0.02)
        self.sleep(0.04)
        for k in reversed(keys):
            self.key_up(k)
            self.sleep(0.02)


def macro_to_python(data: Any, name: Any = "macro") -> str:
    """Translate a macro recording JSON into an editable Python script."""
    if isinstance(data, str) and isinstance(name, dict):
        data, name = name, data
    elif not isinstance(data, dict):
        data = {}
    name = str(name) if not isinstance(name, dict) else "macro"
    target_info = data.get("target_window") or {}
    target_title = target_info.get("title", "Desktop Window")
    recorded_at = data.get("recorded_at", "Unknown")

    header = f'''"""
Input Reply Macro: {name}
Target Window: {target_title}
Recorded: {recorded_at}

You can edit this script dynamically to add conditionals, loops, calculations,
or custom automation logic!

Available context methods:
  context.params                       - Dictionary of replay parameters passed in
  context.speed                        - Playback speed multiplier
  context.sleep(seconds)               - Wait/pause in seconds (scaled by speed)
  context.click(x, y, button="left")   - Click at coordinates
  context.double_click(x, y)           - Double click at coordinates
  context.mouse_move(x, y)             - Move cursor to coordinates
  context.mouse_down(button="left")
  context.mouse_up(button="left")
  context.drag(x1, y1, x2, y2)         - Drag mouse between coordinates
  context.type_text(text)              - Type string into focused window
  context.press_key(key)               - Press & release key ('enter', 'tab', etc.)
  context.hotkey(*keys)                - Key combination (e.g. 'ctrl', 'v')
  context.is_cancelled()               - Check if user cancelled
"""

def run(context):
    params = context.params

    # Example dynamic logic:
    # if params.get("status") == "approved":
    #     context.type_text("Approved by Admin\\n")
    # elif params.get("status") == "rejected":
    #     context.type_text("Rejected\\n")
    # else:
    #     context.type_text("Pending Review\\n")

'''

    lines: list[str] = []

    # Agent format: high-level steps
    if data.get("format") == core.FORMAT_AGENT:
        for step in data.get("steps", []):
            stype = step.get("type")
            if stype == "click":
                lines.append(f'    context.click({step["x"]}, {step["y"]}, button="{step.get("button", "left")}")')
            elif stype == "double_click":
                lines.append(f'    context.double_click({step["x"]}, {step["y"]}, button="{step.get("button", "left")}")')
            elif stype == "move":
                lines.append(f'    context.mouse_move({step["x"]}, {step["y"]})')
            elif stype == "drag":
                lines.append(f'    context.drag({step["from_x"]}, {step["from_y"]}, {step["to_x"]}, {step["to_y"]})')
            elif stype == "wait":
                lines.append(f'    context.sleep({step.get("seconds", 1.0)})')
            elif stype == "type":
                text = step.get("text", "")
                param = step.get("param")
                if param:
                    lines.append(f'    if "{param}" in params:')
                    lines.append(f'        context.type_text(params["{param}"])')
                    lines.append(f'    else:')
                    lines.append(f'        context.type_text({json.dumps(text)})')
                else:
                    lines.append(f'    context.type_text({json.dumps(text)})')
            elif stype == "key":
                lines.append(f'    context.press_key({json.dumps(step["key"])})')
            elif stype == "hotkey":
                keys_arg = ", ".join(json.dumps(k) for k in step.get("keys", []))
                lines.append(f'    context.hotkey({keys_arg})')

    # Raw event format
    else:
        events = data.get("events", [])
        blocks = core.typing_blocks(data)
        block_indices = {}
        param_by_block = {}
        for p in data.get("parameters", []):
            if isinstance(p, dict) and p.get("name") and p.get("block"):
                param_by_block[p["block"]] = p["name"]

        for b in blocks:
            b_num = b["block"]
            for idx in b.get("indices", []):
                block_indices[idx] = b_num

        skip_indices = set()
        last_t = 0.0
        last_x, last_y = None, None

        i = 0
        while i < len(events):
            if i in skip_indices:
                i += 1
                continue

            event = events[i]
            t = event.get("t", 0.0)
            dt = t - last_t
            if dt >= 0.25 and last_t > 0:
                lines.append(f'    context.sleep({round(dt, 2)})')
            last_t = t

            etype = event.get("type", "")

            # Check if this event starts a typing block
            if i in block_indices:
                b_num = block_indices[i]
                b_info = next((b for b in blocks if b["block"] == b_num), None)
                if b_info:
                    # Gather all keys in this block
                    b_keys = []
                    for idx in b_info.get("indices", []):
                        skip_indices.add(idx)
                        ev = events[idx]
                        if ev.get("type") == "key_down" and ev.get("key"):
                            k = ev["key"]
                            if len(k) == 1:
                                b_keys.append(k)
                            elif k == "space":
                                b_keys.append(" ")
                    typed_str = "".join(b_keys)
                    param_name = param_by_block.get(b_num)
                    if param_name:
                        lines.append(f'    # Typing Block {b_num} (Parameter: {param_name})')
                        lines.append(f'    if "{param_name}" in params:')
                        lines.append(f'        context.type_text(params["{param_name}"])')
                        lines.append(f'    else:')
                        lines.append(f'        context.type_text({json.dumps(typed_str)})')
                    else:
                        lines.append(f'    # Typing Block {b_num}')
                        lines.append(f'    context.type_text({json.dumps(typed_str)})')
                    i += 1
                    continue

            # Mouse click detection (button_down followed by button_up)
            if etype == "button_down":
                btn = event.get("button", "left")
                x, y = event.get("x", 0), event.get("y", 0)
                # Look ahead for matching button_up
                up_idx = None
                for j in range(i + 1, min(i + 10, len(events))):
                    if events[j].get("type") == "button_up" and events[j].get("button") == btn:
                        up_idx = j
                        break
                if up_idx is not None:
                    skip_indices.add(up_idx)
                    lines.append(f'    context.click({x}, {y}, button="{btn}")')
                    last_x, last_y = x, y
                    i += 1
                    continue
                else:
                    lines.append(f'    context.mouse_down("{btn}")')
                    i += 1
                    continue

            elif etype == "button_up":
                btn = event.get("button", "left")
                lines.append(f'    context.mouse_up("{btn}")')
                i += 1
                continue

            elif etype == "motion":
                x, y = event.get("x", 0), event.get("y", 0)
                # Only record significant motions
                if last_x is None or math.hypot(x - last_x, y - last_y) > 40:
                    lines.append(f'    context.mouse_move({x}, {y})')
                    last_x, last_y = x, y
                i += 1
                continue

            elif etype == "key_down":
                key = event.get("key", "")
                if key not in {"f12"}:
                    lines.append(f'    context.press_key({json.dumps(key)})')
                i += 1
                continue

            i += 1

    if not lines:
        lines.append('    # No actions recorded yet')
        lines.append('    pass')

    return header + "\n".join(lines) + "\n"


def execute_python_macro(code: str, backend, params: dict | None = None, speed: float = 1.0, cancel: threading.Event | None = None) -> bool:
    """Execute python macro code inside a controlled environment."""
    context = MacroContext(backend, params=params, speed=speed, cancel=cancel)
    env: dict[str, Any] = {
        "__name__": "__macro__",
        "context": context,
        "params": context.params,
        "time": time,
        "math": __import__("math"),
        "re": re,
    }

    # Open backend player if needed
    opened = False
    if hasattr(backend, "open_player"):
        try:
            backend.open_player()
            opened = True
        except Exception:
            pass

    try:
        compiled = compile(code, "<macro>", "exec")
        exec(compiled, env)
        if "run" in env and callable(env["run"]):
            env["run"](context)
        return not context.is_cancelled()
    except Exception as err:
        if cancel and cancel.is_set():
            return False
        raise RuntimeError(f"Error executing Python macro: {err}") from err
    finally:
        if opened and hasattr(backend, "close_player"):
            try:
                backend.close_player()
            except Exception:
                pass


def get_macro_script(name: str) -> dict[str, Any]:
    """Get the current Python code for a macro (custom or auto-generated)."""
    data = core.read_recording(name)
    py_path = core.safe_path(name).with_suffix(".py")

    if py_path.exists():
        try:
            code = py_path.read_text(encoding="utf-8")
            if code.strip():
                return {"name": name, "python_code": code, "custom": True}
        except Exception:
            pass

    if data.get("python_code"):
        return {"name": name, "python_code": data["python_code"], "custom": True}

    generated = macro_to_python(data, name)
    return {"name": name, "python_code": generated, "custom": False}


def save_macro_script(name: str, code: str) -> dict[str, Any]:
    """Validate and save custom Python code for a macro."""
    if not isinstance(code, str) or not code.strip():
        raise ValueError("Macro Python code cannot be empty")

    # Validate Python syntax
    try:
        compile(code, f"<{name}>", "exec")
    except SyntaxError as err:
        raise ValueError(f"Python syntax error at line {err.lineno}: {err.msg}") from err

    data = core.read_recording(name, fresh=True)
    data["python_code"] = code
    core.write_recording(name, data)

    # Also sync .py file in recordings directory for external editors
    try:
        py_path = core.safe_path(name).with_suffix(".py")
        py_path.write_text(code, encoding="utf-8")
    except Exception:
        pass

    return {"name": name, "python_code": code, "custom": True, "saved": True}


def reset_macro_script(name: str) -> dict[str, Any]:
    """Reset macro to default generated steps (removes custom python_code)."""
    data = core.read_recording(name, fresh=True)
    data.pop("python_code", None)
    core.write_recording(name, data)

    py_path = core.safe_path(name).with_suffix(".py")
    if py_path.exists():
        try:
            py_path.unlink()
        except Exception:
            pass

    generated = macro_to_python(data, name)
    return {"name": name, "python_code": generated, "custom": False, "reset": True}
