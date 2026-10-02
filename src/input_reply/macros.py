"""Replay of AI-designed macros (steps), including parameter substitution."""

from __future__ import annotations

import re
import threading
import time

from . import core

PAUSE = 0.35   # settle time between steps


def resolve(data: dict, values: dict) -> list[dict]:
    """Steps with parameter values substituted. Omitted parameters keep the text typed when the macro was made."""
    if not isinstance(values, dict):
        raise ValueError("Parameter values must be a JSON object")
    names = {p["name"] for p in data.get("parameters", [])}
    for name, value in values.items():
        if name not in names:
            raise ValueError(f"Unknown parameter: {name}")
        if not isinstance(value, str) or len(value) > 4000 or "\x00" in value:
            raise ValueError(f"Parameter {name} must be text up to 4000 characters")
    return [step | {"text": values[step["param"]]} if step["type"] == "type" and step.get("param") in values else step
            for step in data["steps"]]


def play(data: dict, values: dict, actuator, cancel: threading.Event | None = None, speed: float = 1.0) -> bool:
    steps = resolve(data, values)
    rate = float(speed) if speed and float(speed) > 0 else 1.0
    pause = max(0.02, PAUSE / rate)
    with actuator.session():
        actuator.set_scale(data.get("screen"), actuator.screen_size())
        try:
            for step in steps:
                if cancel and cancel.is_set():
                    return False
                if step["type"] == "wait":
                    wait_time = max(0.0, step.get("seconds", 0) / rate)
                    if cancel:
                        if cancel.wait(wait_time):
                            return False
                    else:
                        time.sleep(wait_time)
                else:
                    actuator.perform(step, cancel)
                    if cancel:
                        if cancel.wait(pause):
                            return False
                    else:
                        time.sleep(pause)
            return not (cancel and cancel.is_set())
        finally:
            actuator.set_scale(None, None)


def parameterize(steps: list[dict], parameters: list[dict]) -> tuple[list[dict], list[dict]]:
    """Turn typed text that matches an example value into a named parameter.

    ``parameters`` is [{"name": ..., "value": ...}]. A value that is only part of a typed string
    splits that step so just the value becomes the parameter. Raises if a value was never typed.
    """
    output = [dict(step) for step in steps]
    declared = []
    for item in parameters:
        name, value = item.get("name"), item.get("value")
        if not isinstance(name, str) or not core.PARAM_RE.fullmatch(name):
            raise ValueError(f"Invalid parameter name: {name!r}")
        if not isinstance(value, str) or not value:
            raise ValueError(f"Parameter {name} needs the example text that was typed")
        if any(p["name"] == name for p in declared):
            raise ValueError(f"Duplicate parameter: {name}")
        rebuilt, found = [], False
        for step in output:
            if step["type"] != "type" or "param" in step or value not in step["text"]:
                rebuilt.append(step)
                continue
            found = True
            for index, piece in enumerate(re.split(f"({re.escape(value)})", step["text"])):
                if piece == "":
                    continue
                rebuilt.append({"type": "type", "text": piece, **({"param": name} if piece == value and index % 2 else {})})
        if not found:
            raise ValueError(f"The text {value!r} for parameter {name} was never typed in this macro. A value picked by "
                             "clicking cannot be a parameter. Call save_macro again without that parameter (or with only "
                             "values you typed); do not redo the task.")
        output = rebuilt
        declared.append({"name": name})
    return output, declared
