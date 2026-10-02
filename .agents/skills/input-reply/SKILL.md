---
name: input-reply
description: >-
  Desktop automation macro creator and runner. Use this skill to create, edit,
  and replay desktop macros. Supports creating macros via 1-command CLI or script,
  asking the user to record interactively, driving the UI autonomously via screenshots
  and human-like smooth mouse movements, dynamic Python scripting with parameters,
  conditionals, and loops, and replaying any saved macro or .py file.
trigger: /input-reply
---

# Input Reply Skill

Input Reply records, replays, and executes desktop automation macros with natural human-like mouse movements (cubic Bezier curves with easing), human typing cadence, visual element detection, and dynamic Python logic.

> [!IMPORTANT]
> **No codebase inspection needed!** Everything an AI agent needs to create, save, edit, and replay macros is fully documented in this file.

---

## ⚡ Quick Cheat Sheet (Copy & Paste)

### 1. Save / Create a Macro (1 Command)
```bash
# Option A: Save inline Python code directly
input-reply create my_macro --overwrite --code "
def run(context):
    context.human_click(450, 320)
    context.human_type('Hello from AI!')
    context.press_key('enter')
"

# Option B: Save from an existing script file
input-reply create my_macro --overwrite --code-file path/to/script.py
```
> Both commands create `<recordings_dir>/my_macro.json` and `<recordings_dir>/my_macro.py`.
> The `.py` extension is optional; `my_macro`, `my_macro.json`, and `my_macro.py` are all accepted.

### 2. Replay Any Macro or Python Script
```bash
# Run a saved macro by name
input-reply run my_macro

# Run with parameters, speed multiplier, and repetitions
input-reply run my_macro --set recipient="Alice" --set amount="250" --speed 1.5 --repeat 3

# Run a Python script directly without saving first
input-reply run ./custom_macro.py --set env="prod"
```
> **Emergency Abort**: Press **`Ctrl+Esc`** at any moment during replay to immediately stop all movements and abort all repetitions.

### 3. Ask User to Record Interactively (Mode A)
```bash
# 1. Find window ID
input-reply windows --json

# 2. Start recording (automatically centers mouse on window after 3s countdown)
input-reply record --name user_workflow --window-id <ID> --seconds 60 --countdown 3
```
> Tell the user: *"Recording starts in 3 seconds. The cursor will center on your window. Perform your workflow, then press **F12** to finish."*
> When stopped, `user_workflow.json` and editable `user_workflow.py` are saved.

### 4. Autonomous Vision & Human Action (Mode B)
```bash
# 1. List windows & focus target app
input-reply windows --json
input-reply focus "Calculator"

# 2. Take screenshot to locate button/input coordinates (x, y)
input-reply screenshot shot.png --window-id <ID>

# 3. Move & click in a natural human way (curved Bezier glide + 50-90ms button hold)
input-reply human-move 500 350
input-reply human-click 500 350 --button left

# 4. Type with realistic human cadence (random 30-80ms delays)
input-reply type-text "Invoice #1042" --human

# 5. Take verification screenshot
input-reply screenshot verify.png

# 6. Save the verified workflow as a reusable macro
input-reply create verified_workflow --overwrite --code "..."
```

---

## 🐍 Complete Python Macro Context API

Every macro script defines a `run(context)` function. Below is the complete API available on the `context` object:

```python
def run(context):
    # ----------------------------------------------------
    # 1. PARAMETERS & STATE
    # ----------------------------------------------------
    # Parameters passed from CLI: input-reply run my_macro --set user=Alice --set mode=fast
    user = context.params.get("user", "Guest")
    mode = context.params.get("mode", "normal")
    speed = context.speed  # Current playback speed multiplier (float, default 1.0)

    # ----------------------------------------------------
    # 2. HUMAN-LIKE MOVEMENTS (Recommended for UI automation)
    # ----------------------------------------------------
    # Smooth curved mouse glide using cubic Bezier curve and smoothstep easing
    context.human_move(x=600, y=400, duration=0.35)

    # Move along Bezier curve to (x, y), pause naturally, press & hold 50-90ms, release
    context.human_click(x=600, y=400, button="left")  # button: "left", "right", "middle"

    # Type string with realistic randomized intervals (30-80ms per character)
    context.human_type("Automated text with natural human keystroke speed")

    # ----------------------------------------------------
    # 3. DIRECT RAW ACTIONS (Instant / Fast)
    # ----------------------------------------------------
    context.click(x=600, y=400, button="left")    # Instant teleport and click
    context.double_click(x=600, y=400)           # Double click
    context.mouse_move(x=600, y=400)             # Instant cursor move
    context.mouse_down(button="left")            # Mouse button press
    context.mouse_up(button="left")              # Mouse button release
    context.drag(x1=100, y1=100, x2=400, y2=400) # Drag mouse between coordinates
    context.type_text("Fast instant text\n")     # Fast string insertion

    # ----------------------------------------------------
    # 4. KEYBOARD & SHORTCUTS
    # ----------------------------------------------------
    context.press_key("enter")                   # Single key ('enter', 'tab', 'escape', 'backspace', 'down', etc.)
    context.hotkey("ctrl", "c")                  # Key combination
    context.hotkey("ctrl", "shift", "s")

    # ----------------------------------------------------
    # 5. WINDOW MANAGEMENT & VISION
    # ----------------------------------------------------
    # Focus window by title or numeric window ID
    context.focus("Notepad")

    # Take screenshot mid-macro for visual validation or debugging
    image = context.screenshot(path="step_result.png")  # Returns PIL.Image and saves to path

    # ----------------------------------------------------
    # 6. TIMING & CANCELLATION
    # ----------------------------------------------------
    # Sleep/pause in seconds (automatically scaled by context.speed, e.g. 1.0s / 2.0x = 0.5s)
    context.sleep(1.0)

    # Check if user pressed Ctrl+Esc to abort (crucial in loops)
    if context.is_cancelled():
        return
```

---

## 📁 Macro File Storage Locations

Macros are stored as paired `.json` (metadata/events) and `.py` (executable Python logic) files in the user data directory:
- **Windows**: `%LOCALAPPDATA%\InputReply\recordings\` (e.g. `C:\Users\<user>\AppData\Local\InputReply\recordings\`)
- **Linux**: `~/.local/share/input-reply/recordings/`

When `input-reply run <name>` is called:
1. It automatically looks for `<name>.py` in the recordings folder.
2. If `<name>.py` exists, it executes `run(context)`.
3. You can edit `<name>.py` directly in any text editor or with code tools.

---

## 🛠️ Ready-to-Use Macro Templates

### Template 1: Automated Form / Data Entry
```python
def run(context):
    p = context.params
    name = p.get("name", "John Doe")
    email = p.get("email", "john@example.com")
    note = p.get("note", "Auto-generated note")

    # Click Name input field
    context.human_click(350, 220)
    context.human_type(name)
    context.press_key("tab")

    # Type Email
    context.human_type(email)
    context.press_key("tab")

    # Type Note
    context.human_type(note)
    context.sleep(0.3)

    # Click Submit button
    context.human_click(450, 480)
    context.sleep(0.5)

    if context.is_cancelled():
        return
```

### Template 2: Looping Over Data with Safety Check
```python
def run(context):
    items = context.params.get("items", "item1,item2,item3").split(",")
    
    for item in items:
        if context.is_cancelled():
            print("Aborted by user.")
            break
        context.human_click(300, 250)
        context.human_type(f"Processing: {item.strip()}")
        context.press_key("enter")
        context.sleep(0.5)
```

---

## 🔍 CLI Command Quick Reference

| Command | Arguments | What it does |
|---|---|---|
| `input-reply create` | `<name> [--code "..." \| --code-file file.py] [--overwrite]` | Saves a new macro with Python script in 1 step |
| `input-reply run` | `<name_or_file.py> [--set key=val] [--repeat N] [--speed S]` | Replays a macro or direct Python script |
| `input-reply record` | `--name <name> --window-id <id> [--seconds 60] [--countdown 3]` | Records user interaction; stops on `F12` |
| `input-reply windows` | `[--json]` | Lists all open desktop windows with IDs & titles |
| `input-reply focus` | `<title_or_id>` | Brings a desktop window into focus |
| `input-reply screenshot` | `[output.png] [--window-id <id>]` | Captures full desktop or specific window screenshot |
| `input-reply human-move` | `<x> <y> [--duration <sec>]` | Smoothly glides mouse along a human Bezier curve |
| `input-reply human-click`| `[x] [y] [--button left\|right\|middle]` | Glides to coordinates and clicks with realistic hold time |
| `input-reply type-text`  | `"<text>" [--human]` | Types text into active window (`--human` uses 30-80ms delays) |
| `input-reply catalog`    | `[--json]` | Lists all existing macros |
| `input-reply script`     | `<name> [--export file.py \| --save file.py \| --reset]` | Views, exports, or saves Python macro code |
| `input-reply doctor`     | | Checks desktop backend, active service port, and recordings dir |

---

## 🛡️ Safety Controls
- **Abort Replay (`Ctrl+Esc`)**: Instantly kills replay, ceases cursor movement, and cancels all repetitions.
- **Stop Recording (`F12`)**: Safely stops recording. The key event is trapped and suppressed so it won't trigger browser developer tools or application shortcuts.
- **Port Conflict Discovery**: Input Reply auto-resolves port conflicts and writes `%LOCALAPPDATA%\InputReply\service.lock`. All commands connect to the active server automatically.
