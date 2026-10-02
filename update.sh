#!/bin/sh
# Update Input Reply to the latest version, refresh dependencies, deploy skills, and restart the service.
set -eu

echo "========================================"
echo "         Updating Input Reply           "
echo "========================================"

here=$(cd "$(dirname "$0")" && pwd)
data="${XDG_DATA_HOME:-$HOME/.local/share}/input-reply"
venv="$data/venv"
bin="$HOME/.local/bin"

# Step 1: Git pull if running inside git repository
echo "\n[1/8] Checking repository status..."
if git -C "$here" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "  Pulling latest updates from git repository..."
    if ! git -C "$here" pull --ff-only; then
        echo "  Notice: Could not fast-forward git repository automatically. Updating from local source." >&2
    fi
else
    echo "  Current directory is not a git repository. Updating from local source."
fi

# Step 2: Check Virtual Environment
echo "\n[2/8] Verifying installed virtual environment..."
if [ ! -d "$venv" ]; then
    echo "  Virtual environment not found at $venv. Running full installer..."
    sh "$here/install.sh"
    exit 0
fi
echo "  Found virtual environment at: $venv"

# Step 3: Stop running service
echo "\n[3/8] Stopping running Input Reply service..."
"$venv/bin/input-reply" stop 2>/dev/null || true
pkill -f "input_reply service" 2>/dev/null || true
sleep 1
echo "  Service stopped."

# Step 4: Upgrade dependencies and package
echo "\n[4/8] Upgrading pip and updating Input Reply package..."
"$venv/bin/python" -m pip install --quiet --upgrade pip
echo "  Installing latest package from: $here"
if ! "$venv/bin/python" -m pip install --upgrade "$here[desktop,ai]"; then
    echo "  Desktop extras could not be installed; installing core package..." >&2
    "$venv/bin/python" -m pip install --upgrade "$here"
fi
echo "  Package updated successfully."

# Step 5: Refresh symlinks and desktop launcher
echo "\n[5/8] Refreshing desktop application launcher and CLI symlinks..."
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

# Step 6: Deploy AI Agent Skills
echo "\n[6/8] Refreshing AI agent skills (Codex, Claude, AGY)..."
skill_src="$here/skills/input-reply/SKILL.md"
if [ -f "$skill_src" ]; then
    for target in "$here/.agents/skills/input-reply" \
                  "$here/.agent/skills/input-reply" \
                  "$here/.claude/skills/input-reply" \
                  "$HOME/.gemini/config/skills/input-reply" \
                  "$data/skills/input-reply"; do
        mkdir -p "$target"
        cp -f "$skill_src" "$target/SKILL.md"
        echo "  Deployed skill to: $target"
    done
fi

# Step 7: Refresh systemd service configuration
echo "\n[7/8] Refreshing systemd service configuration..."
if command -v systemctl >/dev/null 2>&1; then
    systemd_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$systemd_dir"
    cat > "$systemd_dir/input-reply.service" <<EOF
[Unit]
Description=Input Reply Background Service
After=network-online.target graphical-session.target
Wants=network-online.target

[Service]
ExecStart=$venv/bin/python -m input_reply service
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user enable input-reply.service 2>/dev/null || true
    echo "  Systemd user service updated."
fi

# Step 8: Restart service
echo "\n[8/8] Launching updated Input Reply service..."
"$venv/bin/input-reply" setup

echo "\n========================================================"
echo " [OK] Input Reply successfully updated to latest version!"
echo "========================================================"
echo "Service is running. Use 'input-reply app' to open the UI."
echo ""
