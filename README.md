# Input Reply

Record mouse and keyboard actions, name the places where you typed text, then replay the macro with different values. The same controls are available from a responsive web page and a JSON-friendly command line.

## Platform support

| Desktop | Record and replay | Window focus | Pointer coordinates |
| --- | --- | --- | --- |
| Windows 10/11 | `pynput` | Win32 foreground window API | Absolute screen coordinates |
| Linux X11 | XInput and XTest | `xdotool` | Absolute screen coordinates |
| Linux Wayland | `wdotool` with portal or evdev capture | Depends on compositor; manual focus works | Portal: absolute. Evdev: relative movement only. |

Wayland deliberately restricts global input capture and window control. `wdotool record` first tries the desktop portal, then evdev. The portal can ask for permission; evdev needs access to `/dev/input/event*`. `wdotool` replay needs a working input backend, normally libei, compositor protocols, or `/dev/uinput`. GNOME, KDE, and wlroots based desktops expose different capabilities. Run `wdotool diag` and `input-reply doctor` if the desktop cannot be controlled. [Wayland portal documentation](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html) · [wdotool documentation](https://github.com/cushycush/wdotool)

The app can be installed on other Linux desktops too, but recording or replay may need additional compositor permissions. A graphical desktop session must be logged in; the server cannot control the login screen. Windows secure desktop prompts and elevated applications may also reject input from a normal user process.

## Install

Python 3.10 or newer is required. Clone this repository, open a terminal in it, then create a virtual environment:

**Linux:**

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

On X11, install your distribution's `xinput`, `xmodmap`, `xdotool`, `libX11`, and `libXtst` packages. For example, Debian/Ubuntu provide them through `xinput x11-xserver-utils xdotool libx11-6 libxtst6`. Set `DISPLAY` and `XAUTHORITY` to the desktop session when running from SSH; `INPUT_REPLY_DISPLAY` and `INPUT_REPLY_XAUTHORITY` can override them.

On Wayland, install [wdotool](https://github.com/cushycush/wdotool) with its `record` command (its default build includes this feature). The capture backend can be selected with `INPUT_REPLY_WAYLAND_CAPTURE=portal` or `evdev`; `auto` tries both. Grant the permission requested by the desktop portal, or configure input device access for evdev. Some compositors may require `/dev/uinput` access for replay. Avoid running the web server as root.

**Windows PowerShell:**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
```

On Windows, `pynput` installs automatically. The app needs an interactive desktop session.

Check installation with `input-reply doctor`.

## Record and replay

```sh
input-reply windows --json
input-reply record --name send-message --seconds 30 --window-id WINDOW_ID
input-reply catalog --json
input-reply inspect send-message.json --json
input-reply param add send-message.json --block 1 --name friend
input-reply param add send-message.json --block 2 --name message
printf '%s' '{"friend":"Alex","message":"Hi!"}' | input-reply run send-message.json --params-stdin --json
```

Press F12 to end an X11 or Windows recording, or stop it from the web app. For Wayland recording, use the web Stop button or Ctrl+C in the terminal. A click, Enter, Tab, shortcut, or pause of more than 1.5 seconds separates typing blocks. `inspect` shows their number, time, and key count without showing what was typed. On replay, omit a parameter to keep its original keystrokes. An empty string removes that typing block.

For AI clients, use `catalog --json`, `inspect NAME --json`, and `run NAME --params-stdin --json`. Replacement text can also come from `--params-file values.json`; this keeps it out of command arguments and process listings. `--set friend=Alex` is available for quick manual use. The program does not print replacement values in its result.

Recordings are saved outside the repository: `~/.local/share/input-reply/recordings/` on Linux or `%LOCALAPPDATA%\InputReply\recordings\` on Windows. X11 recordings from the earlier local v3 app can be copied into this folder. A recording can only replay through the backend that captured it.

## Desktop app, startup, and accounts

The quickest install gives you the desktop app, registers it to start at login, and launches it:

```sh
./install.sh          # Linux
.\install.ps1         # Windows PowerShell
```

Or, from a virtual environment, `python -m pip install ".[desktop]"` then `input-reply setup`. On Linux the native window needs a system WebKit (`python3-gi` with `gir1.2-webkit2-4.1`, or Qt); without it the same dashboard opens in your browser.

- **Starts at login.** Input injection needs a live desktop session, so "boot" means desktop login. `input-reply setup` (and the first launch of the service) registers it: an XDG autostart entry on Linux, the `HKCU\...\Run` key on Windows, a LaunchAgent on macOS. Turn it off from the tray menu, the **Account** dialog, or `input-reply remove-autostart`.
- **Tray icon and window.** `input-reply service` runs in the background with a tray icon (Open, Remote control, Start at login, Quit). `input-reply app` (or the *Input Reply* launcher) starts the service if needed and opens the window. The window signs itself in to the local API; you never type the access code.
- **Web dashboard.** `input-reply serve` still runs only the local web server on `http://localhost:8765/` (use `--host 0.0.0.0` for a phone on your LAN; it needs the access code from `input-reply token`). The server is plain HTTP: use a trusted LAN, Tailscale, or an HTTPS reverse proxy, never the open internet. It refuses account sign-in from other machines so a password never crosses the LAN.

### Accounts and remote control

Sign in from **Account** in the app, or `input-reply login` / `input-reply signup`. Each account has its own recordings folder on the computer (`.../accounts/<uid>/recordings`) and its own cloud data; nobody else can read or trigger yours. The first account to sign in adopts recordings made before accounts existed. Passwords are never stored, only Firebase's refresh token (owner-only file, `session.json`).

While signed in, the desktop service connects to Firebase Realtime Database and listens for commands from any client signed in as the same user. Everything the app can do is available remotely: `status`, `recordings`, `windows`, `inspect`, `record`, `replay`, `stop`, `delete`, `parameter_add`, `parameter_remove`.

Clients:

- **Remote web app**: `remote/index.html`, a static page for Firebase Hosting (phone or any browser).
- **CLI**: `input-reply remote devices`, `remote list`, `remote run NAME --set friend=Alex`, `remote record NAME --window-id ID`, `remote stop`, with `--device` when several computers are online.
- **Your own client**: write a command node (below) using any Firebase SDK.

Protocol, all under `inputReply/users/<uid>/`:

```
devices/<device>          name, platform, backend, online, lastSeen, remoteEnabled, job{phase,busy,message},
                          recordings[]{name, duration, keys, blocks[], parameters[]}   (never typed text)
commands/<device>/<id>    {action, args, createdAt: {".sv": "timestamp"}, status: "pending"}
                          -> status running -> done|error|expired, plus result / error
```

Safety: commands older than two minutes are marked `expired` and never run; replacement text is deleted from the database as soon as the job starts; **Remote control** can be switched off per computer (tray, Account dialog); actions are an allow-list, and database rules confine each user to their own subtree.

### Firebase setup

1. In the Firebase console enable **Authentication > Email/Password** and create a **Realtime Database**.
2. Merge `firebase/database.rules.json` into your rules. It only touches the `inputReply` node. If the project already hosts other apps, add that node to the existing rules instead of overwriting them.
3. Put your web app config on each computer as `cloud.json` in the data folder (`~/.local/share/input-reply/` or `%LOCALAPPDATA%\InputReply\`), or point `INPUT_REPLY_FIREBASE_CONFIG` at it. Use `firebase/cloud.example.json` as the template. The Firebase web config identifies the project but is not a secret; the rules are what protect the data.
4. Optional remote web app: copy `remote/firebase-config.example.js` to `remote/firebase-config.js`, then `firebase deploy --only hosting`.

The command channel uses only the Python standard library (Auth and Realtime Database REST, with server-sent events for instant delivery), so there is nothing extra to install.

### Release builds

`.github/workflows/release.yml` bundles Windows and Linux apps with PyInstaller (manual or on `v*` tags). The recording backends exist for Windows, Linux X11 and Linux Wayland; there is no macOS input backend yet.

## Privacy and limits

Record only non-sensitive actions. Recording files contain raw key events and can reveal typed text even though the web editor does not reconstruct it. Files, the access code, your Firebase config and session are ignored by Git; never commit real recordings. Remote commands travel through your Firebase project, so anyone who can sign in as you can control the computer: use a strong password. Replay acts on the live desktop, so check the target window and values before running a macro.

Macro coordinates depend on screen layout and scale. Wayland evdev capture stores relative movement and may start from the wrong cursor position if your starting position changes. `wdotool` recording captures clicks as taps, so drag operations are not reproduced on Wayland. Linux X11 and Windows preserve button down/up events.

## Development

```sh
python -m unittest discover -s tests -v
```

MIT licensed. See [LICENSE](LICENSE).
