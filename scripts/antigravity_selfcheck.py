#!/usr/bin/env python3
"""
antigravity_selfcheck.py — conformance test Antigravity runs on ITSELF.

WHY THIS EXISTS
---------------
The daemon's cycle function ends with log.append("Cycle complete.") no matter
what happened. A cycle where every action failed to parse, the push was
rejected, and the venue readback was empty looks identical in the log to a
clean cycle. That is the meta-defect: **the loop cannot tell that it is
failing**, so it will report health forever while doing nothing.

This script is the missing feedback signal. It checks the invariants the daemon
is *supposed* to hold and returns PASS / FAIL / WARN per check with evidence.
Run it at the end of every cycle and put the exit code in the council entry.

Exit codes:  0 = all pass, 1 = at least one FAIL, 2 = only warnings

USAGE
  python3 scripts/antigravity_selfcheck.py
  python3 scripts/antigravity_selfcheck.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACTIONS = ROOT / "desk/actions"
COUNCIL = ROOT / "desk/COUNCIL.md"
STATE = ROOT / "desk/state.json"
POLLER = ROOT / "scripts/antigravity_council_poller.py"

RESET = {"PASS": "", "FAIL": "", "WARN": ""}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Check:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, name: str, status: str, detail: str, fix: str = "") -> None:
        self.rows.append({"check": name, "status": status, "detail": detail, "fix": fix})

    def ok(self, name, detail, fix=""):
        self.add(name, "PASS", detail, fix)

    def fail(self, name, detail, fix=""):
        self.add(name, "FAIL", detail, fix)

    def warn(self, name, detail, fix=""):
        self.add(name, "WARN", detail, fix)


def check_action_routing(c: Check) -> None:
    """Every action kind the desk can raise must have a REAL handler."""
    if not ACTIONS.exists():
        c.warn("action-routing", "desk/actions/ does not exist yet")
        return
    kinds: dict[str, list[str]] = {}
    for f in sorted(ACTIONS.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        kind = d.get("kind", "?")
        status = d.get("status", "?")
        venue = d.get("venue", "?")
        kinds.setdefault(kind, []).append(f"{f.name} [{status}] venue={venue}")

    # what handlers does the poller actually implement?
    src = POLLER.read_text(encoding="utf-8") if POLLER.exists() else ""
    handles: dict[str, str] = {}
    for kind in ("ADD_STOP", "REVIEW_ORDER", "PLACE_ORDER", "RECONCILE", "AMEND_ORDER", "CANCEL_ORDER"):
        # a REAL handler mutates status or writes a file inside the branch
        m = re.search(rf'kind == "{kind}"(.*?)(?=\n\s*(?:el)?if |\n\s*else:|\n\s*# Step)', src, re.S)
        if not m:
            handles[kind] = "absent"
        else:
            body = m.group(1)
            if re.search(r"status.*=.*(DONE|DECLINED|SUPERSEDED)|write_text|json.dump", body):
                handles[kind] = "real"
            else:
                handles[kind] = "log-only"

    log_only = [k for k, v in handles.items() if v == "log-only"]
    absent = [k for k, v in handles.items() if v == "absent"]
    if log_only:
        c.fail("action-routing",
               f"handlers that only log and never close the action: {log_only}. "
               f"Any such action stays PENDING forever, and the raiser cannot tell "
               f"it was ignored vs rejected.",
               "A handler must always terminate the action: set status DONE or DECLINED "
               "with a resolution string, or explicitly re-queue it with a reason.")
    elif absent:
        c.warn("action-routing", f"no handler for kinds: {absent} (fine if never raised)")
    else:
        c.ok("action-routing", "every implemented kind has a terminating handler")

    stuck = [x for v in kinds.values() for x in v if "[PENDING]" in x]
    if stuck:
        c.warn("pending-actions", f"{len(stuck)} action(s) still PENDING: {stuck[:4]}",
               "Check whether they are inside their intended handling window, or stuck.")


def check_hardcoded_venue(c: Check) -> None:
    """A venue string hardcoded in a branch is a bug waiting to happen."""
    src = POLLER.read_text(encoding="utf-8") if POLLER.exists() else ""
    hard = re.findall(r'venue == "([^"]+)"', src)
    # only a problem if a hardcoded FAILURE branch exists that skips real venues
    log_only = re.search(r'kind == "(PLACE_ORDER|REVIEW_ORDER)"(.*?)(?=\n\s*(?:elif|else|# Step))', src, re.S)
    if log_only and not re.search(r"write_text|json.dump|status", log_only.group(2)):
        c.fail("venue-matching",
               f"the {log_only.group(1)} branch is log-only despite a real venue match existing",
               "See action-routing.")
        return
    if hard:
        # do those strings match anything actually raised?
        raised = set()
        if ACTIONS.exists():
            for f in ACTIONS.glob("*.json"):
                try:
                    raised.add(json.loads(f.read_text(encoding="utf-8")).get("venue"))
                except Exception:
                    pass
        mismatch = [h for h in hard if h not in raised]
        if mismatch:
            c.fail("venue-matching",
                   f"hardcoded venue match {mismatch} matches NO action actually raised "
                   f"(real venues: {sorted(x for x in raised if x)})",
                   "Match on kind + instrument, not on a literal venue string. Read the "
                   "action's own fields; never hardcode a venue.")
        else:
            c.ok("venue-matching", f"hardcoded venues {hard} match raised actions")
    else:
        c.ok("venue-matching", "no hardcoded venue matching")


def check_git_identity(c: Check) -> None:
    """A daemon that cannot sign a commit will silently stop pushing."""
    # Identity can come from repo config OR the environment. Git falls back to
    # GIT_AUTHOR_*/GIT_COMMITTER_* when config is empty, so reading only
    # `git config` produces a FALSE FAILURE for a daemon that sets env vars at
    # startup (which is exactly what this one now does). Check both, and check
    # what git itself resolves.
    import os
    name = os.environ.get("GIT_AUTHOR_NAME") or os.environ.get("GIT_COMMITTER_NAME") or ""
    email = os.environ.get("GIT_AUTHOR_EMAIL") or os.environ.get("GIT_COMMITTER_EMAIL") or ""
    if not name or not email:
        try:
            name = name or subprocess.run(["git", "config", "user.name"], cwd=ROOT,
                                          capture_output=True, text=True, timeout=15).stdout.strip()
            email = email or subprocess.run(["git", "config", "user.email"], cwd=ROOT,
                                            capture_output=True, text=True, timeout=15).stdout.strip()
        except Exception as e:  # noqa: BLE001
            c.warn("git-identity", f"could not query git config: {e}")
            return
    # does the POLLER set it at startup? that is the real requirement.
    src = POLLER.read_text(encoding="utf-8") if POLLER.exists() else ""
    poller_sets = "GIT_AUTHOR_NAME" in src and "GIT_AUTHOR_EMAIL" in src
    if (not name or not email) and not poller_sets:
        c.fail("git-identity", f"user.name={name!r} user.email={email!r} — commit will fail "
                               f"with 'empty ident name' and the push never happens",
               "Set GIT_AUTHOR_NAME/EMAIL and GIT_COMMITTER_NAME/EMAIL as env exports at the "
               "top of every daemon run, before any git command.")
    elif poller_sets and (not name or not email):
        c.ok("git-identity", "poller exports GIT_AUTHOR_*/GIT_COMMITTER_* at startup "
                             "(env fallback verified to work when config is empty)")
    else:
        c.ok("git-identity", f"{name} <{email}>")


def check_stale_rebase(c: Check) -> None:
    """A leftover rebase state blocks every future pull --rebase."""
    dirty = [p for p in (ROOT / ".git/rebase-merge", ROOT / ".git/rebase-apply") if p.exists()]
    if dirty:
        c.fail("git-rebase-state", f"stale {[str(p.name) for p in dirty]} present — "
                                   f"'cannot pull with rebase' until removed",
               "rm -rf .git/rebase-merge .git/rebase-apply, then re-run the pull.")
    else:
        c.ok("git-rebase-state", "clean")


def check_push_refspec(c: Check) -> None:
    """Plain 'push origin main' has failed on this repo; HEAD:main works."""
    src = POLLER.read_text(encoding="utf-8") if POLLER.exists() else ""
    if 'push", "origin", "main"' in src or "push origin main" in src:
        c.warn("push-refspec", "poller pushes with 'origin main'",
               "This repo has rejected that with 'tip is behind its remote' despite a verified "
               "fast-forward. Use 'git push origin HEAD:main'.")
    elif "HEAD:main" in src:
        c.ok("push-refspec", "uses HEAD:main")
    else:
        c.ok("push-refspec", "no plain 'origin main' push found")


def check_referenced_files(c: Check) -> None:
    """A branch that imports a missing file throws every cycle and is swallowed."""
    src = POLLER.read_text(encoding="utf-8") if POLLER.exists() else ""
    refs = re.findall(r'from (scripts\.[\w.]+) import', src)
    refs += re.findall(r'ROOT / "(desk/[\w.]+)"', src)
    missing = []
    for r in set(refs):
        p = ROOT / r.replace(".", "/") if r.startswith("scripts.") else ROOT / r
        cand = [p, p.with_suffix(".py") if p.suffix == "" else p]
        if not any(x.exists() for x in cand):
            missing.append(r)
    if missing:
        c.warn("referenced-files", f"poller references missing path(s): {missing}",
               "The import sits inside try/except, so failure is logged and skipped — "
               "the check silently never runs. Fix the path or delete the branch.")
    else:
        c.ok("referenced-files", f"all {len(set(refs))} referenced paths resolve")


def check_time_window_logic(c: Check) -> None:
    """Time guards that are permanently true (or false) are not guards."""
    src = POLLER.read_text(encoding="utf-8") if POLLER.exists() else ""
    m = re.search(r"now_dt\.hour == (\d+) and now_dt\.minute >= (\d+) or now_dt\.hour > (\d+)", src)
    if m:
        h = int(m.group(1))
        now = datetime.now(timezone.utc)
        # the condition as written is true for every hour > h, forever
        c.fail("time-window",
               f"resolver guard `hour == {h} and minute >= {m.group(2)} or hour > {h}` is "
               f"TRUE FOREVER after {h}:59Z — it is not a window, it is a permanent switch. "
               f"Current hour {now.hour}Z => runs: {now.hour > h}",
               "Anchor to an explicit timestamp: run once per resolved-cohort, keyed on the "
               "cohort's own earliest_due field, and record the cohort id so it cannot re-run.")
    else:
        c.ok("time-window", "no permanently-true time guard detected")


def check_loop_collision(c: Check) -> None:
    """Two loops on the same 6-slot upstream gate will collide."""
    cycles = sorted((ROOT / "desk/cycles").glob("*.json")) if (ROOT / "desk/cycles").exists() else []
    mins = set()
    for f in cycles[-12:]:
        m = re.search(r"T(\d\d)(\d\d)", f.name)
        if m:
            mins.add(int(m.group(2)))
    if len(mins) >= 3:
        near_zero = [m for m in mins if m % 20 <= 2]
        if len(near_zero) >= 2:
            c.warn("loop-collision",
                   f"arena cycles land at :{sorted(near_zero)} — Antigravity must stay on "
                   f":10/:30/:50 or both loops hit the same upstream rate-limit window",
                   "Confirm the daemon schedule is offset; 6-of-16 symbol scans share one "
                   "Coinglass budget and return 50001 under burst.")
        else:
            c.ok("loop-collision", f"cycle minutes {sorted(mins)} spread")
    else:
        c.warn("loop-collision", "too few cycles recorded to judge scheduling")


def check_council_append_only(c: Check) -> None:
    """The shared document is append-only by mandate."""
    if not COUNCIL.exists():
        c.warn("council", "desk/COUNCIL.md missing")
        return
    text = COUNCIL.read_text(encoding="utf-8")
    if "<<<<<<<" in text or ">>>>>>>" in text:
        c.fail("council", "merge conflict markers present in COUNCIL.md",
               "Resolve keeping BOTH sides — the file is append-only, never take one side.")
        return
    entries = re.findall(r"^### \[(\d{4}-\d\d-\d\dT[\d:]+Z)\] · (.+?) · (\w+)", text, re.M)
    if not entries:
        c.warn("council", "no parsable council entries")
        return
    last_ts, last_who, last_kind = entries[-1]
    c.ok("council", f"{len(entries)} entries; latest {last_ts} by {last_who} ({last_kind})")


def check_position_reconciliation(c: Check) -> None:
    """A position count is not evidence — every venue must be named."""
    if not STATE.exists():
        c.warn("positions", "desk/state.json missing")
        return
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        c.fail("positions", f"state.json unparsable: {e}")
        return
    pos = st.get("positions") or []
    venues = {}
    naked = []
    for p in pos:
        v = p.get("venue", "?")
        venues[v] = venues.get(v, 0) + 1
        if not p.get("sl"):
            naked.append(f"{v}:{p.get('symbol')}")
    detail = f"{len(pos)} position(s) across {venues}"
    if naked:
        c.fail("positions", f"{detail}; NO STOP on {naked}",
               "Every position must carry a stop. An unprotected position is the single "
               "highest-severity finding this desk can have.")
    else:
        c.ok("positions", f"{detail}; every position has a stop")


def check_authority(c: Check) -> None:
    """Arena proposes; Antigravity executes. Neither may cross over."""
    src = POLLER.read_text(encoding="utf-8") if POLLER.exists() else ""
    # a poller that auto-places orders without an interlock is out of policy
    # a bare log line like log.append("Place order request for ...") is NOT an order call
    # Only count a REAL call site: a quoted tool name or an HTTP path, never a dispatch
    # branch (elif kind == "PLACE_ORDER") and never a log string.
    call_lines = [ln for ln in src.splitlines()
                  if re.search(r"""["'](place_order|create_order|create_market_order|"""
                               r"""create_limit_order|send_order|trade_action)["']"""
                               r"|/v2/orders|/api/v2/trade", ln, re.I)
                  and not re.search(r"log\.append|print\(|^\s*#", ln)
                  and not re.search(r'kind ==\s*["\']', ln)]
    writes = [ln.strip()[:70] for ln in call_lines]
    interlock = re.findall(r"interlock", src, re.I)
    if writes and not interlock:
        c.fail("authority-interlock",
               f"order-placing calls {set(writes)} present with NO interlock assertion",
               "Assert venue interlock before ANY order batch; on failure write "
               "desk/events/interlock_YYYYMMDD_HHMMSS.json and HALT.")
    elif writes:
        c.ok("authority-interlock", f"order calls {set(writes)} guarded by interlock")
    else:
        c.ok("authority", "poller places nothing itself — correct: it is a supervisor, "
                          "execution stays in the agent")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    c = Check()
    for fn in (check_action_routing, check_hardcoded_venue, check_git_identity,
               check_stale_rebase, check_push_refspec, check_referenced_files,
               check_time_window_logic, check_loop_collision, check_council_append_only,
               check_position_reconciliation, check_authority):
        try:
            fn(c)
        except Exception as e:  # noqa: BLE001
            c.warn(fn.__name__, f"check itself threw: {e}")

    fails = [r for r in c.rows if r["status"] == "FAIL"]
    warns = [r for r in c.rows if r["status"] == "WARN"]

    if a.json:
        print(json.dumps({"at": utc_now(), "rows": c.rows,
                          "fail": len(fails), "warn": len(warns)}, indent=2))
        return 1 if fails else (2 if warns else 0)

    print(f"\nANTIGRAVITY SELF-CHECK · {utc_now()}")
    print("=" * 88)
    for r in c.rows:
        mark = {"PASS": "[PASS]", "FAIL": "[FAIL]", "WARN": "[WARN]"}[r["status"]]
        print(f"{mark} {r['check']}")
        print(f"       {r['detail']}")
        if r["status"] != "PASS" and r.get("fix"):
            print(f"       FIX: {r['fix']}")
    print("=" * 88)
    print(f"{len(c.rows) - len(fails) - len(warns)} pass · {len(warns)} warn · {len(fails)} fail")
    if fails:
        print("\nA cycle with any FAIL must NOT report itself healthy.")
    return 1 if fails else (2 if warns else 0)


if __name__ == "__main__":
    sys.exit(main())
