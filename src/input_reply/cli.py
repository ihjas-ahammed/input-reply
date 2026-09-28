"""Command line interface for people and automation clients."""

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path

from . import core
from .backends import select_backend
from .server import access_token, mapping_for, record_once, replay_once, seconds, serve


def _values(args):
    values = {}
    if args.params_file:
        content = json.loads(Path(args.params_file).read_text(encoding="utf-8"))
        if not isinstance(content, dict):
            raise ValueError("Parameter file must contain a JSON object")
        values.update(content)
    if args.params_stdin:
        content = json.load(sys.stdin)
        if not isinstance(content, dict):
            raise ValueError("Standard input must contain a JSON object")
        values.update(content)
    for pair in args.set:
        if "=" not in pair:
            raise ValueError("--set must be NAME=VALUE")
        name, value = pair.split("=", 1)
        values[name] = value
    return values


def _cancel_on_sigint():
    event = threading.Event()
    prior = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: event.set())
    return event, prior


def _restore_sigint(prior):
    signal.signal(signal.SIGINT, prior)


def _autostart_path():
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Input Reply.bat"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "input-reply.desktop"


def install_autostart(host, port):
    path = _autostart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        executable = pythonw if pythonw.exists() else Path(sys.executable)
        body = f'@echo off\r\nstart "" /MIN "{executable}" -m input_reply serve --host {host} --port {port}\r\n'
        path.write_text(body, encoding="utf-8")
    else:
        launcher = core.data_dir() / "start-server.sh"
        launcher.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        import shlex
        launcher.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-m", "input_reply", "serve",
                                                           "--host", host, "--port", str(port)]) + "\n", encoding="utf-8")
        launcher.chmod(0o700)
        launcher_exec = '"' + str(launcher).replace('\\', '\\\\').replace('"', '\\"') + '"'
        path.write_text("[Desktop Entry]\nType=Application\nName=Input Reply\n"
                        f"Exec={launcher_exec}\nTerminal=false\nX-GNOME-Autostart-enabled=true\n", encoding="utf-8")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(prog="input-reply", description="Record and replay editable desktop macros")
    sub = parser.add_subparsers(dest="command", required=True)
    web = sub.add_parser("serve", help="Run the web app")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    sub.add_parser("token", help="Print the web access code")
    sub.add_parser("doctor", help="Check the desktop backend")
    windows = sub.add_parser("windows", help="List desktop windows")
    windows.add_argument("--json", action="store_true")
    catalog = sub.add_parser("catalog", aliases=["list"], help="List recordings")
    catalog.add_argument("--json", action="store_true")
    inspect = sub.add_parser("inspect", help="List typing blocks without text")
    inspect.add_argument("name")
    inspect.add_argument("--json", action="store_true")
    param = sub.add_parser("param", help="Name or remove a typing block")
    psub = param.add_subparsers(dest="action", required=True)
    add = psub.add_parser("add")
    add.add_argument("recording")
    add.add_argument("--block", type=int, required=True)
    add.add_argument("--name", required=True)
    remove = psub.add_parser("remove")
    remove.add_argument("recording")
    remove.add_argument("name")
    record = sub.add_parser("record", help="Record desktop input")
    record.add_argument("--name", default="macro")
    record.add_argument("--seconds", type=float, default=30)
    record.add_argument("--countdown", type=float, default=3)
    record.add_argument("--window-id")
    run = sub.add_parser("run", aliases=["replay"], help="Replay with replacement text")
    run.add_argument("name")
    run.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    run.add_argument("--params-file")
    run.add_argument("--params-stdin", action="store_true")
    run.add_argument("--countdown", type=float, default=3)
    run.add_argument("--window-id")
    run.add_argument("--preserve-key-holds", action="store_true")
    run.add_argument("--json", action="store_true")
    start = sub.add_parser("install-autostart", help="Start the server after desktop login")
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=8765)
    sub.add_parser("remove-autostart", help="Remove automatic startup")
    args = parser.parse_args(argv)

    if args.command == "serve":
        serve(args.host, args.port)
    elif args.command == "token":
        print(access_token())
    elif args.command == "install-autostart":
        print(f"Installed desktop startup: {install_autostart(args.host, args.port)}")
    elif args.command == "remove-autostart":
        path = _autostart_path()
        path.unlink(missing_ok=True)
        print(f"Removed desktop startup: {path}")
    elif args.command in {"catalog", "list"}:
        result = core.catalog()
        print(json.dumps({"recordings": result}, indent=2) if args.json else
              "\n".join(f"{r['name']}  {r['duration']}s  {r['keys']} keys  {r['clicks']} clicks  {r['target']}" for r in result))
    else:
        backend = select_backend()
        if args.command == "doctor":
            print(json.dumps({"backend": backend.name, "desktop_available": backend.available(),
                              "recordings_folder": str(core.recordings_dir()),
                              "autostart_file": str(_autostart_path())}, indent=2))
        elif args.command == "windows":
            result = backend.windows()
            print(json.dumps({"windows": result}, indent=2) if args.json else
                  "\n".join(f"{w['id']}  {w['title']}" for w in result))
        elif args.command == "inspect":
            data = core.read_recording(args.name)
            result = core.inspect(args.name, mapping_for(data, backend))
            print(json.dumps(result, indent=2) if args.json else
                  f"{args.name}: " + ", ".join(f"block {b['block']} ({b['keys']} keys)" for b in result["blocks"]))
        elif args.command == "param":
            data = core.read_recording(args.recording)
            mapping = mapping_for(data, backend)
            result = core.add_parameter(args.recording, args.block, args.name, mapping) if args.action == "add" else \
                     core.remove_parameter(args.recording, args.name, mapping)
            print(json.dumps(result, indent=2))
        elif args.command == "record":
            if not backend.available():
                raise RuntimeError("Interactive desktop is unavailable; run input-reply doctor")
            duration, countdown = seconds(args.seconds), seconds(args.countdown, 30)
            if duration <= 0:
                raise ValueError("Recording duration must be positive")
            name = core.new_name(args.name)
            cancel, prior = _cancel_on_sigint()
            try:
                data = record_once(backend, name, duration, countdown, args.window_id, cancel)
            finally:
                _restore_sigint(prior)
            print(f"Saved {name}" if data else "Cancelled")
        else:
            if not backend.available():
                raise RuntimeError("Interactive desktop is unavailable; run input-reply doctor")
            values = _values(args)
            cancel, prior = _cancel_on_sigint()
            try:
                target, completed = replay_once(backend, args.name, values,
                                                seconds(args.countdown, 30), args.window_id,
                                                args.preserve_key_holds, cancel)
            finally:
                _restore_sigint(prior)
            result = {"recording": args.name, "target": target["title"] if target else None,
                      "completed": completed, "parameters_overridden": sorted(values)}
            print(json.dumps(result) if args.json else
                  f"Replayed {args.name} in {target['title']}" if completed else "Cancelled")


def entrypoint():
    try:
        main()
    except (ValueError, RuntimeError, FileNotFoundError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    entrypoint()
