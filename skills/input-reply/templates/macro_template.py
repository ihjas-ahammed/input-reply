"""Input Reply Macro Template
Defines custom automation logic with conditionals, loops, parameters, and human-like movement.
"""

def run(context):
    params = context.params
    print(f"Running macro with params: {params}")

    # Example 1: Read parameter with fallback
    recipient = params.get("recipient", "Support")
    message = params.get("message", "Status check")

    # Example 2: Focus target window
    # context.focus("WhatsApp")
    # context.sleep(0.5)

    # Example 3: Smooth human-like mouse movement and click
    # context.human_move(400, 300)
    # context.human_click(400, 300, button="left")

    # Example 4: Realistic human keystroke typing
    # context.human_type(f"Hello {recipient},\n{message}\n")

    # Example 5: Hotkeys
    # context.hotkey("ctrl", "enter")

    # Example 6: Screenshot for visual validation
    # shot = context.screenshot("after_send.png")

    # Safe exit if user pressed Ctrl+Esc
    if context.is_cancelled():
        return
