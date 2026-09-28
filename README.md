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

## Web app and startup

```sh
input-reply serve                     # local computer only, port 8765
input-reply serve --host 0.0.0.0      # reachable on your local network
input-reply token                     # show web access code
input-reply install-autostart --host 0.0.0.0
```

Open `http://localhost:8765/` or `http://COMPUTER_IP:8765/` on a phone. The web page lets you select recordings, name typing blocks, enter replay values, and stop a running job. `install-autostart` starts the server after desktop login on Linux and Windows; `remove-autostart` removes that entry. Start the app from the desktop session on Wayland so it can reach the portal.

The access code is stored in the app data folder and is required for every API request. Keep it private. The built-in HTTP server does **not** encrypt LAN traffic; use a trusted LAN, Tailscale, or an HTTPS reverse proxy. Do not expose port 8765 directly to the public internet.

## Privacy and limits

Record only non-sensitive actions. Recording files contain raw key events and can reveal typed text even though the web editor does not reconstruct it. Files and the access code are ignored by Git; never commit real recordings. Replay acts on the live desktop, so check the target window and values before running a macro.

Macro coordinates depend on screen layout and scale. Wayland evdev capture stores relative movement and may start from the wrong cursor position if your starting position changes. `wdotool` recording captures clicks as taps, so drag operations are not reproduced on Wayland. Linux X11 and Windows preserve button down/up events.

## Development

```sh
python -m unittest discover -s tests -v
```

MIT licensed. See [LICENSE](LICENSE).
