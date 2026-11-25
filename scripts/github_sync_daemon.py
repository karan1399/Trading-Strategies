#!/usr/bin/env python3
"""
Continuous GitHub Sync Daemon for Trading-Strategies.

Safely stages tracked workspace changes (rules, desk logs, scripts) and
telemetry updates while strictly respecting .gitignore (never staging untracked
local runtime secrets/state like desk/mt5.json, .env, or engine/data/),
rebases cleanly onto origin/main, and pushes HEAD:main continuously.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK_FILE = ROOT / "desk" / ".git-sync.lock"
SAFE_ADD_PATHS = [
    "data/telemetry",
    ".agents",
    "desk/COUNCIL.md",
    "desk/log.md",
    "scripts/antigravity_auto_wake.ps1",
    "scripts/github_sync_daemon.py",
]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def run_git(args: list[str], timeout: int = 45) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def acquire_lock() -> bool:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age < 300:
                return False
            LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            return False
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        os.close(fd)
        return True
    except OSError:
        return False


def release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def sync_once() -> bool:
    if not acquire_lock():
        print(f"[github-daemon] Lock busy, skipping cycle ({utc_stamp()} UTC)", flush=True)
        return True

    try:
        run_git(["add", "-u"], timeout=25)
        existing_paths = [p for p in SAFE_ADD_PATHS if (ROOT / p).exists()]
        if existing_paths:
            run_git(["add", *existing_paths], timeout=25)

        diff_res = run_git(["diff", "--cached", "--quiet"], timeout=15)
        committed = False
        if diff_res.returncode != 0:
            ts = utc_stamp()
            c_res = run_git(["commit", "-m", f"sync: workspace + desk + telemetry [{ts} UTC]"], timeout=30)
            if c_res.returncode == 0:
                committed = True
            else:
                print(f"[github-daemon] Commit failed: {c_res.stderr.strip() or c_res.stdout.strip()}", flush=True)
                return False

        # Check ahead/behind vs origin/main and push if ahead or freshly committed
        run_git(["fetch", "origin", "main"], timeout=30)
        counts = run_git(["rev-list", "--left-right", "--count", "origin/main...HEAD"], timeout=15)
        behind, ahead = 0, 0
        if counts.returncode == 0 and len(counts.stdout.strip().split()) == 2:
            behind, ahead = (int(x) for x in counts.stdout.strip().split())

        if behind > 0:
            rb = run_git(["pull", "--rebase", "--autostash", "origin", "main"], timeout=45)
            if rb.returncode != 0:
                run_git(["rebase", "--abort"], timeout=15)
                print(f"[github-daemon] Rebase conflict aborted cleanly: {rb.stderr.strip()}", flush=True)
                return False
            counts2 = run_git(["rev-list", "--left-right", "--count", "origin/main...HEAD"], timeout=15)
            if counts2.returncode == 0 and len(counts2.stdout.strip().split()) == 2:
                behind, ahead = (int(x) for x in counts2.stdout.strip().split())

        if committed or ahead > 0:
            p_res = run_git(["push", "origin", "HEAD:main"], timeout=45)
            if p_res.returncode == 0:
                head_sha = run_git(["rev-parse", "--short", "HEAD"], timeout=10).stdout.strip()
                print(f"[github-daemon] Pushed HEAD:main ({head_sha}, ahead={ahead}) at {utc_stamp()} UTC", flush=True)
                return True
            err = (p_res.stderr or p_res.stdout).strip()
            print(f"[github-daemon] Push error: {err}", flush=True)
            return False

        print(f"[github-daemon] Up-to-date with origin/main ({utc_stamp()} UTC)", flush=True)
        return True
    except Exception as exc:
        print(f"[github-daemon] Exception: {exc}", flush=True)
        return False
    finally:
        release_lock()


def main() -> int:
    once = "--once" in sys.argv
    interval = int(os.environ.get("GITHUB_SYNC_INTERVAL_SECS", "120"))
    print("==================================================================", flush=True)
    print(f"Starting Continuous GitHub Sync Daemon | Interval: {interval}s | Branch: HEAD:main", flush=True)
    print("==================================================================", flush=True)

    if once:
        return 0 if sync_once() else 1

    while True:
        sync_once()
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
