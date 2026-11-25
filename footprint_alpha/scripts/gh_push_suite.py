#!/usr/bin/env python3
"""Merge the alpha suite into the user's private GitHub repo and push main.

Reads the token from /tmp/.gh_tok (0600) — NEVER prints it. Merge policy:
additive — the user's existing content (ORB/, HFT/, Volatility_compression/,
historical_parquet/, etc.) is preserved untouched; alpha-suite files are added
on top; only true collisions outside suite namespaces get renamed *.alpha.
"""
import os
import re
import shutil
import subprocess
import sys

TOK_PATH = "/tmp/.gh_tok"
CLEAN = "/tmp/cleanrepo"
REPOS = ["karan1399/Trading-Strategies", "karan1399/Trading-Strategie"]
WORK = "/tmp/userrepo"
FOLDER = "footprint_alpha"  # suite lives in <repo>/footprint_alpha/ — user's existing folders untouched
MY_NS = ("engine/", "binance_orderflow/", "desk/", "scripts/aec_", "scripts/orb_",
         "scripts/build_clean_repo.py", "scripts/gh_push_suite.py", "scripts/gh_token_recover.py",
         "scripts/run_universe20.sh", "QUICKSTART.md")
AUTHORED_MSG = ("feat: add Binance footprint alpha suite — orderflow dump engine, "
                "per-asset AEC-Trend optimization (TP>=2R), ORB/EMA-rej footprint sleeves, "
                "unified MT5 execution engine, pull-and-run guide")


def sh(cmd, cwd=None, timeout=120):
    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def masked(s: str) -> str:
    return re.sub(r"(https://)[^@\s]+@", r"\1***@", s)


def main():
    if not os.path.exists(TOK_PATH):
        sys.exit("no token at /tmp/.gh_tok — run the recovery step first")
    tok = open(TOK_PATH).read().strip()
    if not os.path.exists(CLEAN) or not os.path.exists(f"{CLEAN}/engine"):
        r = sh("python3 /home/z/my-project/scripts/build_clean_repo.py", timeout=420)
        print("clean repo rebuilt:", r.stdout.strip().splitlines()[-2:])
    # ---- pick repo
    url = None
    for repo in REPOS:
        if sh(f"git ls-remote https://x-access-token:{tok}@github.com/{repo}.git HEAD", timeout=40).returncode == 0:
            url = f"https://x-access-token:{tok}@github.com/{repo}.git"
            print(f"repo found: {repo}")
            break
    if not url:
        sys.exit("no candidate repo reachable with this token")
    # ---- clone
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    r = sh(f"git clone {url} {WORK}", timeout=600)
    if r.returncode != 0:
        sys.exit(f"clone failed: {masked(r.stderr)[:300]}")
    theirs = set()
    for root, _, files in os.walk(WORK):
        if "/.git" in root:
            continue
        for f in files:
            theirs.add(os.path.relpath(os.path.join(root, f), WORK))
    print(f"remote tree: {len(theirs)} files; top-level: {sorted({p.split('/')[0] for p in theirs})[:12]}")
    # ---- additive merge into <repo>/footprint_alpha/
    if any(p == FOLDER or p.startswith(FOLDER + "/") for p in theirs):
        sys.exit(f"'{FOLDER}/' already exists in the repo — aborting to avoid clobbering; pick another name")
    added = []
    for root, dirs, files in os.walk(CLEAN):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            src = os.path.join(root, f)
            rel = os.path.join(FOLDER, os.path.relpath(src, CLEAN))
            dst = os.path.join(WORK, rel)
            os.makedirs(os.path.dirname(dst) or WORK, exist_ok=True)
            shutil.copy2(src, dst)
            added.append(rel)
    print(f"added {len(added)} files under {FOLDER}/ (repo root otherwise untouched)")
    # root-level pointer so a pull-and-run user finds the suite
    qs = os.path.join(WORK, "QUICKSTART.md")
    if not os.path.exists(qs):
        open(qs, "w").write(
            "# Quickstart\n\nThe Binance footprint alpha suite lives in `footprint_alpha/`.\n\n"
            "```bash\ncd footprint_alpha\npip install -r requirements.txt\n"
            "# data fill + selftests + paper run: follow README.md (5 steps, ~30 min first fill)\n```\n")
    # ---- commit + push
    sh("git add -A && git config user.email z@container && git config user.name 'Z User'", cwd=WORK)
    c = sh(f"git commit -m '{AUTHORED_MSG}'", cwd=WORK)
    if c.returncode != 0:
        print("nothing to commit?", c.stdout[-200:], c.stderr[-200:])
    r = sh(f"git push {url} HEAD:refs/heads/main", cwd=WORK, timeout=600)
    if r.returncode != 0 and "fetch first" in (r.stderr + r.stdout).lower():
        sh("git pull --rebase=merges origin main", cwd=WORK, timeout=300)
        r = sh(f"git push {url} HEAD:refs/heads/main", cwd=WORK, timeout=600)
    if r.returncode != 0:
        sys.exit(f"push failed: {masked(r.stderr)[:400]}")
    # ---- verify
    v = sh(f"git ls-remote {url} refs/heads/main", timeout=40)
    local = sh("git rev-parse HEAD", cwd=WORK).stdout.strip()
    remote = v.stdout.strip().split()[0] if v.stdout else "?"
    print(f"push verified: remote main = {remote[:12]} == local {local[:12]}: {remote == local}")
    os.remove(TOK_PATH)
    print("token file deleted; done.")


if __name__ == "__main__":
    main()
