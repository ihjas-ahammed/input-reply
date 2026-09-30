#!/bin/sh
# Install Input Reply for the current user (Linux), register it to start at login, and launch it.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
data="${XDG_DATA_HOME:-$HOME/.local/share}/input-reply"
venv="$data/venv"
bin="$HOME/.local/bin"

command -v python3 >/dev/null || { echo "python3 (3.10 or newer) is required" >&2; exit 1; }

# System site packages lets the app use the distribution's GTK/WebKit and AppIndicator bindings if present.
python3 -m venv --system-site-packages "$venv"
"$venv/bin/python" -m pip install --quiet --upgrade pip
if ! "$venv/bin/python" -m pip install --quiet "$here[desktop]"; then
    echo "Desktop extras could not be installed; installing the core app (the dashboard will open in your browser)." >&2
    "$venv/bin/python" -m pip install --quiet "$here"
fi

mkdir -p "$bin" "${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ln -sf "$venv/bin/input-reply" "$bin/input-reply"
ln -sf "$venv/bin/input-reply-app" "$bin/input-reply-app"

cat > "${XDG_DATA_HOME:-$HOME/.local/share}/applications/input-reply.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Input Reply
Comment=Record and replay desktop macros
Exec=$bin/input-reply-app
Terminal=false
Categories=Utility;
EOF

"$venv/bin/input-reply" setup
echo "Installed. Sign in from the app (Account), or run: input-reply login"
case ":$PATH:" in *":$bin:"*) ;; *) echo "Add $bin to your PATH to use the input-reply command." ;; esac
