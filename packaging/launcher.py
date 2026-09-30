"""Entry point for frozen (PyInstaller) builds. With no arguments it opens the desktop app."""

from input_reply.cli import entrypoint

if __name__ == "__main__":
    entrypoint()
