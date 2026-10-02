#!/bin/sh
# Install Input Reply for the current user (Linux), register it to start at login, and launch it.
set -eu

echo "========================================"
echo "       Installing Input Reply           "
echo "========================================"

here=$(cd "$(dirname "$0")" && pwd)
data="${XDG_DATA_HOME:-$HOME/.local/share}/input-reply"
venv="$data/venv"
bin="$HOME/.local/bin"

# Step 1: Check Python
echo "\n[1/6] Checking Python installation..."
command -v python3 >/dev/null || { echo "ERROR: python3 (3.10 or newer) is required." >&2; exit 1; }
py_ver=$(python3 --version 2>&1)
echo "  Found Python: $py_ver"

# Step 2: Virtual Environment
echo "\n[2/6] Setting up virtual environment at $venv..."
mkdir -p "$data"
# System site packages lets the app use the distribution's GTK/WebKit and AppIndicator bindings if present.
if [ ! -d "$venv" ]; then
    python3 -m venv --system-site-packages "$venv"
    echo "  Created virtual environment."
else
    echo "  Existing virtual environment found."
fi

# Step 3: Upgrade pip
echo "\n[3/6] Upgrading pip in virtual environment..."
"$venv/bin/python" -m pip install --quiet --upgrade pip
echo "  pip is up to date."

# Step 4: Install Input Reply
echo "\n[4/6] Installing Input Reply and dependencies from $here..."
if ! "$venv/bin/python" -m pip install --upgrade "$here[desktop,ai]"; then
    echo "  Desktop extras could not be installed; installing the core app (the dashboard will open in your browser)." >&2
    "$venv/bin/python" -m pip install --upgrade "$here"
fi
echo "  Input Reply installed successfully."

# Step 5: Desktop launcher and CLI symlinks
echo "\n[5/6] Creating desktop application launcher and CLI symlinks..."
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
echo "  Created desktop launcher and symlinks in $bin."

# Step 6: Register autostart and launch service
echo "\n[6/6] Configuring startup and launching Input Reply service..."
"$venv/bin/input-reply" setup

echo "\n========================================================"
echo " [OK] Input Reply successfully installed and running!   "
echo "========================================================"
echo "Sign in from the app (Account), or run: input-reply login"
case ":$PATH:" in *":$bin:"*) ;; *) echo "NOTE: Add $bin to your PATH to use the input-reply command directly." ;; esac
echo ""
