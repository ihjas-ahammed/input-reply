#!/bin/sh
# Update Input Reply to the latest version, refresh dependencies, and restart the service.
set -eu

echo "========================================"
echo "         Updating Input Reply           "
echo "========================================"

here=$(cd "$(dirname "$0")" && pwd)
data="${XDG_DATA_HOME:-$HOME/.local/share}/input-reply"
venv="$data/venv"
bin="$HOME/.local/bin"

# Step 1: Git pull if running inside git repository
echo "\n[1/6] Checking repository status..."
if git -C "$here" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "  Pulling latest updates from git repository..."
    if ! git -C "$here" pull --ff-only; then
        echo "  Notice: Could not fast-forward git repository automatically. Updating from local source." >&2
    fi
else
    echo "  Current directory is not a git repository. Updating from local source."
fi

# Step 2: Check Virtual Environment
echo "\n[2/6] Verifying installed virtual environment..."
if [ ! -d "$venv" ]; then
    echo "  Virtual environment not found at $venv. Running full installer..."
    sh "$here/install.sh"
    exit 0
fi
echo "  Found virtual environment at: $venv"

# Step 3: Stop running service
echo "\n[3/6] Stopping running Input Reply service..."
"$venv/bin/input-reply" stop 2>/dev/null || true
pkill -f "input_reply service" 2>/dev/null || true
sleep 1
echo "  Service stopped."

# Step 4: Upgrade dependencies and package
echo "\n[4/6] Upgrading pip and updating Input Reply package..."
"$venv/bin/python" -m pip install --quiet --upgrade pip
echo "  Installing latest package from: $here"
if ! "$venv/bin/python" -m pip install --upgrade "$here[desktop,ai]"; then
    echo "  Desktop extras could not be installed; installing core package..." >&2
    "$venv/bin/python" -m pip install --upgrade "$here"
fi
echo "  Package updated successfully."

# Step 5: Refresh symlinks and desktop launcher
echo "\n[5/6] Refreshing desktop application launcher and CLI symlinks..."
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
echo "  Symlinks and desktop launcher refreshed."

# Step 6: Restart service
echo "\n[6/6] Launching updated Input Reply service..."
"$venv/bin/input-reply" setup

echo "\n========================================================"
echo " [OK] Input Reply successfully updated to latest version!"
echo "========================================================"
echo "Service is running. Use 'input-reply app' to open the UI."
echo ""
