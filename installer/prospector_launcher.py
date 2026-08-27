"""PyInstaller entry point.

A frozen build has no console by default, so anything that would normally be
printed has to go somewhere the user can find. Everything is written to a log
file next to the user's data, and a failure to start shows a message box rather
than vanishing silently -- an installed app that closes instantly with no
explanation is the worst possible failure mode.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _log_path() -> Path:
    folder = Path.home() / ".prospector"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "startup.log"


def _show_error(message: str) -> None:
    """Tell the user something, even with no console attached."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            f"Prospector could not start.\n\n{message}\n\n"
            f"Details were written to:\n{_log_path()}",
            "Prospector", 0x10,
        )
    except Exception:  # noqa: BLE001 - not on Windows, or no user32
        print(message, file=sys.stderr)


def main() -> int:
    log = _log_path()
    try:
        # Frozen builds get no stdout/stderr, so anything the app prints (and
        # any traceback) is captured here instead of being lost.
        sys.stdout = sys.stderr = log.open("w", encoding="utf-8", buffering=1)
    except OSError:
        pass

    try:
        from prospector.config import ensure_dirs, load_env
        from prospector.database import init_db
        from prospector.webui import serve_app

        load_env()
        ensure_dirs()
        init_db()

        port = int(os.environ.get("PROSPECTOR_PORT", "8740"))
        serve_app(port=port, open_browser=True)
        return 0

    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _show_error(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
