"""One-click launcher for exchange_feed."""

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

PORT = int(os.environ.get("FOOTPRINT_PORT", "8060"))
URL = f"http://127.0.0.1:{PORT}"


def _is_up() -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/api/meta", timeout=1.2) as r:
            return r.status == 200
    except Exception:
        return False


def main() -> None:
    if not _is_up():
        subprocess.Popen(
            [sys.executable, "-m", "exchange_feed.app"],
            cwd=str(_REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for _ in range(30):
            if _is_up():
                break
            time.sleep(0.25)
    webbrowser.open(URL)


if __name__ == "__main__":
    main()
