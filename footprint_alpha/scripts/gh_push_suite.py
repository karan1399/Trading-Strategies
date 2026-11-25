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
    r = sh(f"git clone {url} {WORK}", timeout=180)
    if r.returncode != 0:
        sys.exit(f"clone failed: {masked(r.stderr)[:300]}")
    theirs = set()
    for root, _, files in os.walk(WORK):
        if "/.git" in root:
            continue
        for f in files:
            theirs.add(os.path.relpath(os.path.join(root, f), WORK))
    print(f"remote tree: {len(theirs)} files; top-level: {sorted({p.split('/')[0] for p in theirs})[:12]}")
    # ---- additive merge
    renamed, added, skipped = [], [], []
    for root, dirs, files in os.walk(CLEAN):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            src = os.path.join(root, f)
            rel = os.path.relpath(src, CLEAN)
            dst = os.path.join(WORK, rel)
            in_ns = any(rel == ns or rel.startswith(ns) for ns in MY_NS)
            if rel in theirs and not in_ns:
                stem, ext = os.path.splitext(rel)
                rel = f"{stem}.alpha{ext}"
                dst = os.path.join(WORK, rel)
                renamed.append(rel)
            elif rel in theirs:
                skipped.append(rel)
            os.makedirs(os.path.dirname(dst) or WORK, exist_ok=True)
            shutil.copy2(src, dst)
            added.append(rel)
    print(f"merged: {len(added)} files added ({len(skipped)} suite files overwrote own copies), "
          f"{len(renamed)} collisions renamed: {renamed[:6]}")
    # QUICKSTART pointer
    if renamed or "README.md" in theirs:
        qs = os.path.join(WORK, "QUICKSTART.md")
        if os.path.exists(qs) and "README.md" in theirs:
            sh("printf '\\n## Alpha suite quickstart\\nSee QUICKSTART.md.\\n' >> README.md", cwd=WORK)
    # ---- commit + push
    sh("git add -A && git config user.email z@container && git config user.name 'Z User'", cwd=WORK)
    c = sh(f"git commit -m '{AUTHORED_MSG}'", cwd=WORK)
    if c.returncode != 0:
        print("nothing to commit?", c.stdout[-200:], c.stderr[-200:])
    r = sh(f"git push {url} HEAD:refs/heads/main", cwd=WORK, timeout=180)
    if r.returncode != 0 and "fetch first" in (r.stderr + r.stdout).lower():
        sh("git pull --rebase=merges origin main", cwd=WORK, timeout=120)
        r = sh(f"git push {url} HEAD:refs/heads/main", cwd=WORK, timeout=180)
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
