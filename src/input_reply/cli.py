"""Command line interface for people and automation clients."""

import argparse
import getpass
import json
import os
import signal
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

from . import autostart, core, settings
from .actions import mapping_for, record_once, replay_once, seconds
from .backends import select_backend
from .desktop import DEFAULT_PORT, open_app, probe, run_service, run_window
from .firebase import CloudError, Session, load_config
from .cloud import RemoteClient
from .server import access_token, serve


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


def _saved_session():
    try:
        saved = json.loads(Session.path().read_text(encoding="utf-8"))
        return saved if saved.get("uid") else None
    except (OSError, ValueError, AttributeError):
        return None


def _scope_account():
    """Use the signed-in account's recordings, the same folder the service uses."""
    saved = _saved_session()
    if saved:
        core.set_account(saved["uid"])


def _local_api(port, path, body):
    """Call the running local service so it can pick up an account change immediately."""
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {access_token()}",
                                              "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            message = json.load(error).get("error")
        except ValueError:
            message = None
        raise CloudError(message or f"Service returned HTTP {error.code}") from None


def _credentials(args):
    email = args.email or input("Email: ").strip()
    password = os.environ.get("INPUT_REPLY_PASSWORD") or getpass.getpass("Password: ")
    return email, password


def _login(args):
    email, password = _credentials(args)
    if probe(args.port):
        result = _local_api(args.port, "/api/cloud/" + ("signup" if args.create else "login"),
                            {"email": email, "password": password})
        print(f"Signed in as {result['email']}; the running service is connected.")
        return
    session = Session(load_config())
    session.sign_in(email, password, args.create)
    core.adopt_legacy_recordings(session.uid)
    print(f"Signed in as {session.email}. Start the app with: input-reply app")


def _logout(args):
    if probe(args.port):
        _local_api(args.port, "/api/cloud/logout", {})
    else:
        Session(load_config()).sign_out()
    print("Signed out.")


def _device_line(key, device):
    state = "online" if device.get("online") else "offline"
    return f"{key}  {device.get('name', '?')}  {state}  {device.get('platform', '')}/{device.get('backend', '')}"


def _remote(args):
    client = RemoteClient()
    if args.action == "devices":
        found = client.devices()
        print(json.dumps(found, indent=2) if args.json else "\n".join(_device_line(k, v) for k, v in found.items()))
        return
    device = client.resolve(args.device)
    payload, action = {}, args.action
    if action == "list":
        action = "recordings"
    elif action == "run":
        action, payload = "replay", {"name": args.name, "params": _values(args), "countdown": args.countdown}
        if args.window_id:
            payload["window_id"] = args.window_id
    elif action == "record":
        payload = {"name": args.name, "seconds": args.seconds, "countdown": args.countdown, "window_id": args.window_id}
    elif action == "inspect":
        payload = {"name": args.name}
    result = client.send(device, action, payload)
    if result.get("status") != "done":
        raise RuntimeError(result.get("error") or "Command failed")
    print(json.dumps(result.get("result"), indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="input-reply", description="Record and replay editable desktop macros")
    sub = parser.add_subparsers(dest="command")
    web = sub.add_parser("serve", help="Run only the local web server (no tray, no cloud)")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=DEFAULT_PORT)
    service = sub.add_parser("service", help="Run the background service: web API, cloud agent, tray icon")
    service.add_argument("--host", default="127.0.0.1")
    service.add_argument("--port", type=int, default=DEFAULT_PORT)
    service.add_argument("--no-tray", action="store_true")
    service.add_argument("--no-autostart", action="store_true", help="Do not register startup at login on first run")
    window = sub.add_parser("window", help="Open the dashboard in a native window")
    window.add_argument("--port", type=int, default=DEFAULT_PORT)
    app = sub.add_parser("app", help="Start the service if needed and open the desktop window")
    app.add_argument("--port", type=int, default=DEFAULT_PORT)
    setup = sub.add_parser("setup", help="Register startup at login and launch the service now")
    setup.add_argument("--host", default="127.0.0.1")
    setup.add_argument("--port", type=int, default=DEFAULT_PORT)
    for name, help_text in (("login", "Sign in to your Input Reply account"), ("signup", "Create an account")):
        account = sub.add_parser(name, help=help_text)
        account.add_argument("--email")
        account.add_argument("--port", type=int, default=DEFAULT_PORT)
        account.set_defaults(create=name == "signup")
    logout = sub.add_parser("logout", help="Sign out of this computer")
    logout.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("whoami", help="Show the signed-in account")
    remote = sub.add_parser("remote", help="Control another signed-in computer through the cloud")
    rsub = remote.add_subparsers(dest="action", required=True)
    rdevices = rsub.add_parser("devices")
    rdevices.add_argument("--json", action="store_true")
    for name in ("list", "windows", "status", "stop", "inspect", "record", "run"):
        item = rsub.add_parser(name)
        item.add_argument("--device", help="Device id or name (needed when several are online)")
        if name in {"inspect", "record", "run"}:
            item.add_argument("name")
        if name == "record":
            item.add_argument("--seconds", type=float, default=30)
            item.add_argument("--countdown", type=float, default=3)
            item.add_argument("--window-id", required=True)
        if name == "run":
            item.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
            item.add_argument("--params-file")
            item.add_argument("--params-stdin", action="store_true")
            item.add_argument("--countdown", type=float, default=3)
            item.add_argument("--window-id")
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
    start = sub.add_parser("install-autostart", help="Start the service after desktop login")
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("remove-autostart", help="Remove automatic startup")
    args = parser.parse_args(argv)
    args.command = args.command or "app"
    _scope_account()

    if args.command == "serve":
        serve(args.host, args.port)
    elif args.command == "service":
        run_service(args.host, args.port, tray=not args.no_tray, enable_autostart=not args.no_autostart)
    elif args.command == "window":
        run_window(args.port)
    elif args.command == "app":
        open_app(getattr(args, "port", DEFAULT_PORT))
    elif args.command == "setup":
        print(f"Installed startup at login: {autostart.install(args.host, args.port)}")
        settings.update(autostart_configured=True)
        if not probe(args.port):
            from .desktop import _spawn
            _spawn("service", "--host", args.host, "--port", str(args.port))
            print("Started the Input Reply service. Open the app with: input-reply app")
    elif args.command in {"login", "signup"}:
        _login(args)
    elif args.command == "logout":
        _logout(args)
    elif args.command == "whoami":
        saved = _saved_session()
        print(f"{saved['email']} ({saved['uid']})" if saved else "Not signed in")
    elif args.command == "remote":
        _remote(args)
    elif args.command == "token":
        print(access_token())
    elif args.command == "install-autostart":
        print(f"Installed desktop startup: {autostart.install(args.host, args.port)}")
        settings.update(autostart_configured=True)
    elif args.command == "remove-autostart":
        print(f"Removed desktop startup: {autostart.remove()}")
        settings.update(autostart_configured=True)
    elif args.command in {"catalog", "list"}:
        result = core.catalog()
        print(json.dumps({"recordings": result}, indent=2) if args.json else
              "\n".join(f"{r['name']}  {r['duration']}s  {r['keys']} keys  {r['clicks']} clicks  {r['target']}" for r in result))
    else:
        backend = select_backend()
        if args.command == "doctor":
            print(json.dumps({"backend": backend.name, "desktop_available": backend.available(),
                              "recordings_folder": str(core.recordings_dir()),
                              "autostart_installed": autostart.is_installed(),
                              "signed_in_as": (_saved_session() or {}).get("email")}, indent=2))
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
    except (ValueError, RuntimeError, FileNotFoundError, json.JSONDecodeError, CloudError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    entrypoint()


def app_entrypoint():
    """Launcher for the desktop app (no console window on Windows)."""
    try:
        main(["app"])
    except (ValueError, RuntimeError, FileNotFoundError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
