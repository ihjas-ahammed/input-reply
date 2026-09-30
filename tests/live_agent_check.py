"""End-to-end check: the REAL Gemini Live model drives the assistant against a simulated desktop.

    GEMINI_API_KEY=... python tests/live_agent_check.py

No real desktop input happens: WhatsApp is simulated and drawn as an image the model looks at.
It checks that the model designs a macro once, saves it with parameters, and reuses it on the next request.
"""

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image, ImageDraw  # noqa: E402
from test_ai import CONTACTS, SCREEN, SimDesktop  # noqa: E402

from input_reply import actions, ai, core, settings  # noqa: E402


class VisibleDesktop(SimDesktop):
    """The simulated desktop, drawn so a model can read it."""

    def key(self, keys):
        super().key("Return" if keys.lower() in {"enter", "return", "kp_enter"} else keys)

    def screenshot(self):
        image = Image.new("RGB", SCREEN, (30, 30, 30))
        draw = ImageDraw.Draw(image)
        lines = [f"Desktop. Open application: {self.app or 'none'}"]
        if self.app == "mspaint":
            lines = ["Paint - draw by dragging the mouse across the white canvas below."]
            draw.rectangle((100, 200, 1820, 1000), fill="white")
            for stroke in self.strokes:
                draw.line([tuple(p) for p in stroke], fill="black", width=8)
        elif self.app == "spectacle":
            lines += ["Spectacle screenshot app. Press the Print key to capture the whole screen to the clipboard.",
                      f"Clipboard: {'screenshot image' if self.clipboard else 'empty'}"]
        elif self.app == "whatsapp":
            lines += ["WhatsApp - keyboard driven: type a name in the Search box, press Enter to open that chat,",
                      "then type the message and press Enter to send.", "",
                      f"Search box{' (focused)' if self.field == 'search' else ''}: [{self.search}]",
                      "Contacts: " + ", ".join(CONTACTS),
                      f"Open chat: {self.chat or '(none)'}",
                      f"Message box{' (focused)' if self.field == 'message' else ''}: [{self.draft}]",
                      "Attach: ctrl+u opens the file picker (type a full file path, press Enter to attach); "
                      "ctrl+v pastes an image from the clipboard.",
                      f"Clipboard: {'screenshot image' if self.clipboard else 'empty'}",
                      f"File picker path: [{self.dialog}]" if self.dialog is not None else "File picker: closed",
                      f"Attachment ready to send: {self.attachment or 'none'}"]
            lines += [f"Sent to {c}: {m}" for c, m, _ in self.sent]
        if self.app not in {"whatsapp", "mspaint", "spectacle"}:
            lines.append("Installed apps: whatsapp, mspaint (Paint), spectacle (screenshot app). Use launch_app.")
        for i, line in enumerate(lines):
            draw.text((60, 60 + i * 70), line, fill=(240, 240, 240), font_size=40)
        return image


def main():
    key = ai.api_key()
    if not key:
        sys.exit("Set GEMINI_API_KEY first.")
    with tempfile.TemporaryDirectory() as temp, patch.object(core, "data_dir", return_value=Path(temp)):
        os.environ["GEMINI_API_KEY"] = key
        settings.update(ai_enabled=True)
        desktop = VisibleDesktop()
        state = actions.MacroState(desktop.backend)
        assistant = ai.Assistant(desktop.backend, state, player=None, actuator=desktop)

        def ask(text, timeout=180):
            assistant.ask(text)
            end = time.time() + timeout
            while state.busy() and time.time() < end:
                time.sleep(0.2)
            job = state.snapshot()
            print(f"\n> {text}\n  result: {job['phase']} - {job['message']}")
            for entry in assistant.status()["entries"]:
                if entry["id"] > ask.seen:
                    print(f"  [{entry['role']}] {entry['text'][:150]}")
                    ask.seen = entry["id"]
            return job

        ask.seen = 0
        first = ask("Open WhatsApp and send Hi to Farsan.")
        macros = core.catalog()
        ok = [("design finished", first["phase"] == "done"),
              ("message really sent in the simulation", ("Farsan", "Hi", None) in desktop.sent),
              ("macro saved", len(macros) == 1)]
        if macros:
            info = core.inspect(macros[0]["name"])
            ok.append(("macro has parameters", len(info["parameters"]) >= 1))
            print("  saved macro:", macros[0]["name"], "parameters:", [p["name"] for p in info["parameters"]])
        calls_before, steps_before = len(desktop.log), len(desktop.sent)
        second = ask("Send Hello to Alex on WhatsApp.")
        new_steps = desktop.log[calls_before:]
        ok += [("reuse finished", second["phase"] == "done"),
               ("second message sent by the saved macro", any(m[0] == "Alex" and m[1] and "Hello" in m[1] for m in desktop.sent[steps_before:])),
               ("reuse did not redesign (no new macro)", len(core.catalog()) == 1),
               ("exactly one message per request (no duplicates)", len(desktop.sent) == 2)]
        # ---- more scenarios, each designed by the real model against the simulated desktop ----
        desktop.app = None
        ask("Open Paint and draw a large square on the canvas.")
        boxes = [(min(x for x, _ in st), max(x for x, _ in st), min(y for _, y in st), max(y for _, y in st)) for st in desktop.strokes]
        ok.append(("drew a square-like stroke in Paint", any(b[1] - b[0] > 200 and b[3] - b[2] > 200 for b in boxes)))
        print("  strokes (screen px):", [(b[0], b[2], b[1], b[3]) for b in boxes])

        before = len(desktop.sent)
        ask("Take a screenshot with the screenshot app (spectacle) and send it to Farsan on WhatsApp.")
        ok.append(("screenshot picture sent on WhatsApp (clipboard or saved file)", any(
            m[0] == "Farsan" and m[2] and (m[2] == "clipboard-image" or str(m[2]).endswith(".png")) for m in desktop.sent[before:])))

        before = len(desktop.sent)
        ask("Save a screenshot to a file, then send that file to my own number on WhatsApp. My own number is the contact named Me.")
        sent_file = [m for m in desktop.sent[before:] if m[0] == "Me" and m[2] and str(m[2]).endswith(".png")]
        ok.append(("saved screenshot file sent to my own number", bool(sent_file) and Path(sent_file[0][2]).is_file()))
        ok.append(("no duplicate sends across scenarios", len(desktop.sent) == before + 1 or len(sent_file) == 1))
        print("\nResults")
        for label, passed in ok:
            print(f"[{'PASS' if passed else 'FAIL'}] {label}")
        print("Sent messages:", desktop.sent)
        assistant.close()
        sys.exit(0 if all(p for _, p in ok) else 1)


if __name__ == "__main__":
    main()
