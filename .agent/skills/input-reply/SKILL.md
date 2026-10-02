---
name: input-reply
description: Desktop automation macro creator and runner. Allows AI agents (Codex, Claude, AGY) to create macros either by asking the user to record interactively or by driving the UI autonomously via screenshots and human-like smooth mouse movement. Supports running existing macros, parameter substitution, and editing Python scripts for custom logic.
---

# Input Reply Skill

Input Reply is a local desktop automation tool that records, replays, and script-customizes mouse and keyboard actions. It provides smooth Bezier curve mouse movements, human-like keystroke intervals, visual screen capture, and dynamic Python scripting.

## Overview of Capabilities

1. **Replay Existing Macros**:
   - Run any saved macro by name.
   - Adjust playback speed (`--speed 1.5`) and repetition count (`--repeat 3`).
   - Stop instantly at any time with `Ctrl+Esc`.
2. **Create Macros - Mode A (Interactive Recording)**:
   - Ask the user to record actions manually.
   - Focuses target window, centers the cursor, and records input until user presses `F12`.
   - Generates both a JSON event recording and an editable Python macro script.
3. **Create Macros - Mode B (Autonomous Visual Automation)**:
   - The agent takes screenshots of the target window or desktop.
   - Computes target element coordinates.
   - Interacts **in a human way like actual mouse movement**:
     - `human_move(x, y)`: Cubic Bezier curves with smoothstep easing.
     - `human_click(x, y, button)`: Glide movement, natural hover, realistic button hold time (50–90ms).
     - `human_type(text)`: Realistic human typing cadence (30–80ms per key).
   - Takes a verification screenshot to confirm the action succeeded.
   - Saves the verified sequence as a reusable macro.
4. **Dynamic Python Customization**:
   - Add parameters, loops, conditionals (`if/else`), error checking, or API calls to any macro.
   - Macros run through `def run(context):` with the `MacroContext` API.
5. **Port Conflict & Multi-Session Resilience**:
   - If port 8765 is occupied by another app, Input Reply automatically uses an alternate available port (e.g. 8766) and records it in `service.lock`. All CLI commands and sessions connect automatically.

---

## Quick Reference CLI Commands

| Action | CLI Command |
|---|---|
| **List Windows** | `input-reply windows --json` |
| **Focus Window** | `input-reply focus <title_or_window_id>` |
| **Take Screenshot** | `input-reply screenshot [output.png] [--window-id <id>]` |
| **Human Move Mouse** | `input-reply human-move <x> <y> [--duration <sec>]` |
| **Human Click** | `input-reply human-click [x] [y] [--button left\|right]` |
| **Human Type Text** | `input-reply type-text "hello world" --human` |
| **List Macros** | `input-reply catalog --json` |
| **Replay Macro** | `input-reply run <name> [--set key=val] [--speed 1.0] [--repeat 1]` |
| **Interactive Record** | `input-reply record --name <name> --window-id <id> --seconds 60` |
| **Create Macro** | `input-reply create <name> --window-id <id> --title <title> [--code-file script.py]` |
| **Edit/View Script** | `input-reply script <name>` (or `--export file.py`, `--save file.py`) |
| **Check Health** | `input-reply doctor` |

> [!NOTE]
> On Windows, if `input-reply` is not directly on PATH, use:
> `& "$env:LOCALAPPDATA\InputReply\venv\Scripts\input-reply.exe"` or
> `& "$env:LOCALAPPDATA\InputReply\venv\Scripts\python.exe" -m input_reply`

---

## Workflow 1: Replay an Existing Macro

To inspect available macros and run one:

```bash
# 1. List existing macros
input-reply catalog --json

# 2. Run the macro (example: replay 3 times at 1.5x speed)
input-reply run whatsapp_message.json --repeat 3 --speed 1.5

# 3. Replay with parameter values
input-reply run invoice_filler.json --set customer="Acme Corp" --set amount="150.00"
```

To stop playback at any time, press **`Ctrl+Esc`**.

---

## Workflow 2: Create Macro via Interactive Recording (Mode A)

Use this when user actions are complex, require private authentication (login credentials, 2FA), or when the user wants to demonstrate the workflow manually.

### Steps:
1. List open windows to identify the target:
   ```bash
   input-reply windows --json
   ```
2. Ask the user for confirmation and start recording:
   ```bash
   input-reply record --name my_macro --window-id <WINDOW_ID> --seconds 60 --countdown 3
   ```
3. Inform the user:
   > "Recording starts in 3 seconds. The cursor will automatically center on your window. Perform your workflow, then press **F12** to finish recording."
4. When finished, Input Reply saves:
   - `<recordings_dir>/my_macro.json`: Raw event recording.
   - `<recordings_dir>/my_macro.py`: Generated Python script ready for custom logic.

---

## Workflow 3: Create Macro Autonomously via Vision & Human Movement (Mode B)

Use this when the agent performs the action directly on behalf of the user.

### Step 1: Find and Focus the Target Window
```bash
input-reply windows --json
input-reply focus "Calculator"
```

### Step 2: Take a Screenshot to Locate Elements
```bash
input-reply screenshot shot_initial.png --window-id <WINDOW_ID>
```
Inspect `shot_initial.png` to find coordinates `(x, y)` of buttons, search fields, or menus.

### Step 3: Interact in a Natural Human Way
Move mouse along a curved Bezier trajectory:
```bash
input-reply human-move 450 320 --duration 0.35
```
Click with natural human button press timing:
```bash
input-reply human-click 450 320 --button left
```
Type with realistic human typing cadence (30–80ms intervals):
```bash
input-reply type-text "Searching for report" --human
```

### Step 4: Verify Success Visually
```bash
input-reply screenshot shot_verify.png --window-id <WINDOW_ID>
```
Confirm the UI has updated as expected (e.g. dialog opened, text appeared).

### Step 5: Save as a Reusable Macro
Create the macro and save the action script:
```bash
input-reply create my_autonomous_macro.json --window-id <WINDOW_ID> --title "Target App" --code-file my_script.py
```

---

## Workflow 4: Customizing the Python Macro Script

Every macro has an accompanying Python script `<name>.py` in the recordings directory (`%LOCALAPPDATA%\InputReply\recordings` on Windows).

### Structure of a Macro Script:
```python
"""
Input Reply Macro Script
Target Window: Notepad
"""

def run(context):
    params = context.params
    
    # 1. Custom logic and branching
    mode = params.get("mode", "default")
    if mode == "urgent":
        context.human_type("[URGENT] ")
    
    # 2. Smooth human-like movements
    context.human_move(250, 300)
    context.human_click(250, 300, button="left")
    
    # 3. Type text
    context.human_type("Automated text with realistic human typing\n")
    
    # 4. Keyboard shortcuts
    context.hotkey("ctrl", "s")
    context.sleep(0.5)
    
    # 5. Visual verification mid-macro
    context.screenshot("check.png")
    
    # 6. Check for cancellation
    if context.is_cancelled():
        return
```

### Context API Reference:

| Method | Description |
|---|---|
| `context.params` | `dict` of parameters passed via `--set KEY=VALUE` |
| `context.speed` | Playback speed multiplier (default `1.0`) |
| `context.sleep(sec)` | Pause in seconds (automatically scaled by `context.speed`) |
| `context.human_move(x, y, duration=None)` | Smooth curved mouse movement using cubic Bezier curves |
| `context.human_click(x, y, button="left")` | Glide to coordinates and click with human timing |
| `context.human_type(text, min_delay=0.03, max_delay=0.08)` | Type text with realistic human keystroke intervals |
| `context.click(x, y, button="left")` | Instant click at coordinates |
| `context.mouse_move(x, y)` | Instant mouse move |
| `context.drag(x1, y1, x2, y2)` | Mouse drag from `(x1, y1)` to `(x2, y2)` |
| `context.press_key(key)` | Press and release a single key (`enter`, `tab`, `esc`, etc.) |
| `context.hotkey(*keys)` | Trigger a key combination (e.g. `context.hotkey("ctrl", "shift", "s")`) |
| `context.screenshot(path=None, bbox=None)` | Capture current screen as PIL Image and optionally save to path |
| `context.focus(title_or_id)` | Focus another window by title or ID |
| `context.is_cancelled()` | Returns `True` if `Ctrl+Esc` was pressed to abort |

### Editing Macro Scripts:
1. Export script:
   ```bash
   input-reply script my_macro.json --export edit_macro.py
   ```
2. Modify `edit_macro.py` with your editor or agent tool.
3. Save back into the macro:
   ```bash
   input-reply script my_macro.json --save edit_macro.py
   ```
4. Or directly edit `<recordings_dir>/<name>.py`. When `input-reply run <name>` is called, it automatically prioritizes the `.py` script!

---

## Stopping & Safety Controls
- **Stop Playback**: Press **`Ctrl+Esc`** during replay to instantly abort all repetitions and stop mouse/keyboard events.
- **Stop Recording**: Press **`F12`** during recording. The global keyboard hook suppresses F12 from reaching the target application.
