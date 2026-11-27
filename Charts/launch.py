"""One-click launcher for Charts Lab: ensures server is running and opens browser."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

PORT = int(os.environ.get("CHARTS_PORT", "8050"))
URL = f"http://127.0.0.1:{PORT}"


def _is_up() -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/api/meta", timeout=1.2) as resp:
            return resp.status == 200
    except Exception:
        return False


def main() -> None:
    """Start Charts FastAPI server if not already listening, then open browser."""
    if not _is_up():
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            [sys.executable, "-m", "Charts.app"],
            cwd=str(_REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        for _ in range(30):
            if _is_up():
                break
            time.sleep(0.25)
    webbrowser.open(URL)


if __name__ == "__main__":
    main()
