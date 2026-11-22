#!/usr/bin/env python3
"""
Near-Miss Calibration Resolver
==============================
Scores what price ACTUALLY did against the playbook's rejected candidates
(no-trade formulations with confluence >= 3, captured by
batch_formulate_playbook.py into data/near_miss/candidates.jsonl).

Purpose: empirically answer "are the grade thresholds too tight?" — if the
3-4/10 buckets show positive expectancy against the same §5 levels the
engine would have traded, the thresholds are conservative; if negative,
the gate is saving the desk from noise. Verdict is only issued with n >= 30
per bucket; otherwise the report says INSUFFICIENT SAMPLE.

Method notes (documented in the calibration report):
- Entry is treated as filled at capture (market-reference mode). The
  playbook's live entries use limit-at-zone retests, so live fills would be
  strictly better-priced than this simulation on winners and worse on losers
  — the simulator is the pessimistic baseline.
- Same-candle ambiguity (stop and target both touched) is resolved
  pessimistically: stop first.
- Klines come from the terminal's own /api/cg/kline route (5m, 2880 bars
  ≈ 10 days) — same data path as the dumps, no direct venue traffic.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from dump_for_llm import TERMINAL, http_get  # reuse the sandbox-safe HTTP helper

CANDIDATES = Path("data/near_miss/candidates.jsonl")
RESOLVED = Path("data/near_miss/resolved.jsonl")
CALIB_REPORT = Path("desk/NEAR_MISS_CALIBRATION.md")
MIN_BUCKET_N = 30  # verdict threshold per bucket


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} [near-miss] {msg}", file=sys.stderr, flush=True)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def fetch_klines(terminal: str, sym: str, interval: str = "m5", limit: int = 2880) -> list[list]:
    d = http_get(terminal, f"/api/cg/kline?symbol=Binance_{sym}USDT&interval={interval}&limit={limit}")
    rows = d.get("rows") or []
    # rows: [t_sec, o, h, l, c, v], oldest-first per the route
    return [r for r in rows if isinstance(r, list) and len(r) >= 6]


def simulate(row: dict[str, Any], klines: list[list], horizon_h: float) -> dict[str, Any]:
    """Walk 5m candles after capture; classify outcome vs entry/stop/targets."""
    direction = row["direction"]
    entry, stop = float(row["entry"]), float(row["stop"])
    t1, t2 = float(row["targets"][0]), float(row["targets"][1])
    risk = abs(entry - stop)
    if risk <= 0:
        return {"outcome": "INVALID", "realizedR": 0.0, "mfeR": 0.0, "maeR": 0.0}

    cut = row["capturedAtEpoch"] + horizon_h * 3600
    mfe = mae = 0.0
    outcome = "OPEN"
    t1_move_r = ((t1 - entry) if direction == "long" else (entry - t1)) / risk

    for k in klines:
        t = float(k[0])
        if t <= row["capturedAtEpoch"]:
            continue
        if t > cut:
            break
        hi, lo = float(k[2]), float(k[3])
        # adverse/favorable excursion in R (direction-aware)
        if direction == "long":
            fav, adv = (hi - entry) / risk, (entry - lo) / risk
            hit_stop, hit_t1, hit_t2 = lo <= stop, hi >= t1, hi >= t2
        else:
            fav, adv = (entry - lo) / risk, (hi - entry) / risk
            hit_stop, hit_t1, hit_t2 = hi >= stop, lo <= t1, lo <= t2
        mfe, mae = max(mfe, fav), max(mae, adv)

        if hit_stop:  # pessimistic: same-candle stop-first
            outcome = "STOP-AFTER-T1" if (hit_t1 and not hit_t2) else "LOSS"
            break
        if hit_t2:
            outcome = "WIN2"
            break
        if hit_t1:
            outcome = "T1"  # half off at T1, runner still live at horizon end

    if outcome == "LOSS":
        realized_r = -1.0
    elif outcome == "STOP-AFTER-T1":
        realized_r = 0.5 * t1_move_r + 0.5 * (-1.0)
    elif outcome == "WIN2":
        realized_r = ((t2 - entry) if direction == "long" else (entry - t2)) / risk
    elif outcome == "T1":
        realized_r = t1_move_r  # half banked at T1; runner marked at T1 (no adverse assumption)
    else:  # OPEN — mark at last available close within horizon
        last_close = float(klines[-1][4]) if klines else entry  # rows: [t,o,h,l,c,v]
        realized_r = ((last_close - entry) if direction == "long" else (entry - last_close)) / risk
    return {"outcome": outcome, "realizedR": round(realized_r, 3), "mfeR": round(mfe, 3), "maeR": round(mae, 3)}


def bucket_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    wins2 = sum(1 for r in rows if r["outcome"] == "WIN2")
    losses = sum(1 for r in rows if r["outcome"] == "LOSS")
    t1s = sum(1 for r in rows if r["outcome"] == "T1")
    opens = sum(1 for r in rows if r["outcome"] == "OPEN")
    avg = lambda k: round(sum(r[k] for r in rows) / n, 3) if n else 0.0
    return {
        "n": n, "win2": wins2, "stopAfterT1": t1s, "loss": losses, "open": opens,
        "winRate": round(wins2 / n, 3) if n else None,
        "lossRate": round(losses / n, 3) if n else None,
        "avgRealizedR": avg("realizedR"), "avgMfeR": avg("mfeR"), "avgMaeR": avg("maeR"),
        "verdict": ("EDGE SUPPORTED (avgRealizedR > 0)" if avg("realizedR") > 0 else "EDGE REJECTED (avgRealizedR <= 0)")
                   if n >= MIN_BUCKET_N else f"INSUFFICIENT SAMPLE (n < {MIN_BUCKET_N})",
    }


def run_resolution(terminal: str = TERMINAL, min_age_h: float = 4.0, horizon_h: float = 4.0) -> None:
    cands = load_jsonl(CANDIDATES)
    done_ids = {r["id"] for r in load_jsonl(RESOLVED)}
    now = time.time()
    resolved_now, unresolved = [], []

    for row in cands:
        if row.get("resolved") or row.get("id") in done_ids:
            continue
        age_h = (now - row["capturedAtEpoch"]) / 3600
        if age_h < max(min_age_h, row.get("horizonH", horizon_h)):
            unresolved.append(row)
            continue
        try:
            klines = fetch_klines(terminal, row["sym"])
            sim = simulate(row, klines, row.get("horizonH", horizon_h))
            row.update({"resolved": True, "resolvedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "outcome": sim["outcome"], "realizedR": sim["realizedR"],
                        "mfeR": sim["mfeR"], "maeR": sim["maeR"]})
            resolved_now.append(row)
            log(f"{row['sym']} ({row['bucket']} bucket, {row['direction']}): {sim['outcome']} "
                f"realized {sim['realizedR']}R (mfe {sim['mfeR']}R / mae {sim['maeR']}R)")
        except Exception as e:
            log(f"{row['sym']} resolution failed: {e}")
            unresolved.append(row)

    if resolved_now:
        RESOLVED.parent.mkdir(parents=True, exist_ok=True)
        with RESOLVED.open("a") as fh:
            for r in resolved_now:
                fh.write(json.dumps(r) + "\n")

    # Refresh ledger resolved flags (candidates.jsonl stays append-only capture log)
    all_resolved = load_jsonl(RESOLVED)
    by_bucket: dict[str, list[dict[str, Any]]] = {}
    for r in all_resolved:
        by_bucket.setdefault(r["bucket"], []).append(r)

    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lines = [
        "# NEAR-MISS CALIBRATION REPORT",
        f"> **Generated UTC**: {ts}  |  **Engine**: `desk/LLM_TRADE_FORMULATION_PLAYBOOK.md` rejected candidates",
        "> **Question**: do rejected 3–6/10 formulations carry positive expectancy? If yes, grade thresholds are too tight; if no, the gate is saving the desk from noise.",
        "",
        "## Method (pessimistic baseline)",
        "- Entry assumed filled at capture (market-reference). Live playbook entries are limit-at-zone retests — strictly better-priced, so this simulation understates live edge.",
        "- Same-candle stop/target ambiguity resolved **stop-first** (worst case).",
        "- Horizon: 4h from capture, 5m candles from the terminal's own `/api/cg/kline` route (same data path as the dumps).",
        "- realizedR: WIN2 = full move to T2; T1 = half at T1 + runner marked at T1; LOSS = −1R; OPEN = marked at horizon close.",
        f"- Verdict requires n ≥ {MIN_BUCKET_N} resolved rows per bucket. Below that: INSUFFICIENT SAMPLE — no rule changes.",
        "",
        "## Resolved outcomes by confluence bucket",
        "",
        "| Bucket | n | WIN2 | T1-then-stop | LOSS | OPEN | Win rate | Loss rate | avg realized R | avg MFE R | avg MAE R | Verdict |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |",
    ]
    buckets = sorted(by_bucket.keys(), key=lambda b: (len(b), b))
    if not buckets:
        lines.append("| — | 0 | — | — | — | — | — | — | — | — | — | no resolved rows yet |")
    else:
        for b in buckets:
            s = bucket_stats(by_bucket[b])
            lines.append(
                f"| **{b}/10** | {s['n']} | {s['win2']} | {s['stopAfterT1']} | {s['loss']} | {s['open']} | "
                f"{s['winRate'] if s['winRate'] is not None else '—'} | {s['lossRate'] if s['lossRate'] is not None else '—'} | "
                f"**{s['avgRealizedR']}** | {s['avgMfeR']} | {s['avgMaeR']} | {s['verdict']} |"
            )

    lines += ["", "## Pending (captured, horizon not yet elapsed)", ""]
    if unresolved:
        lines += ["| Symbol | Captured UTC | Bucket | Direction | Entry | Stop | T2 | Evidence (top) |", "| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :--- |"]
        for r in unresolved[-25:]:
            lines.append(f"| {r['sym']} | {r['capturedAtUtc']} | {r['bucket']}/10 | {r['direction']} | "
                         f"{r['entry']} | {r['stop']} | {r['targets'][1]} | {(r.get('evidence') or ['—'])[0][:90]} |")
    else:
        lines.append("_None — every captured candidate has resolved._")

    lines += [
        "",
        "## Calibration policy",
        "- **No threshold changes from small samples.** Verdicts only at n ≥ 30 per bucket (est. 2–3 weeks of daily scans).",
        "- If the 3-bucket shows EDGE REJECTED at scale: the playbook gate is confirmed correct — thresholds stay.",
        "- If the 3-bucket shows EDGE SUPPORTED at scale AND MFE materially exceeds realized: re-test a B-grade-at-4 rule in paper mode before any live tiering change (Kaizen amendment per PRE_TRADE_INTERLOCK_RULE.md §6 authority chain).",
        "- Ledger: `data/near_miss/candidates.jsonl` (capture log, append-only) · `data/near_miss/resolved.jsonl` (outcomes).",
        "",
    ]
    CALIB_REPORT.parent.mkdir(parents=True, exist_ok=True)
    CALIB_REPORT.write_text("\n".join(lines), encoding="utf-8")
    download_dir = Path(os.environ.get("CALIB_DOWNLOAD_DIR", "download"))
    if download_dir.exists():
        (download_dir / "NEAR_MISS_CALIBRATION.md").write_text("\n".join(lines), encoding="utf-8")
    else:
        log("download mirror skipped (no download/ on this host)")
    log(f"resolution done: {len(resolved_now)} newly resolved, {len(unresolved)} pending; "
        f"report -> {CALIB_REPORT}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve near-miss candidates and build the calibration report")
    ap.add_argument("--min-age-h", type=float, default=4.0)
    ap.add_argument("--horizon-h", type=float, default=4.0)
    ap.add_argument("--terminal", default=TERMINAL)
    a = ap.parse_args()
    run_resolution(terminal=a.terminal, min_age_h=a.min_age_h, horizon_h=a.horizon_h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
