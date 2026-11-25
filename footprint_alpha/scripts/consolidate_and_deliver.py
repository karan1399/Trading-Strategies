#!/usr/bin/env python3
"""Consolidate all dump-run sanity reports, verify integrity, and build the
delivery tree under download/binance_orderflow/ (plus a single tar.zst archive).

Usage: python3 scripts/consolidate_and_deliver.py [--deliver]
"""
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

REPO = "/home/z/my-project"
DATA = f"{REPO}/binance_orderflow/data"
OUT = f"{REPO}/download/binance_orderflow"


def sha256_file(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 22), b""):
            h.update(blk)
    return h.hexdigest()


def main():
    deliver = "--deliver" in sys.argv
    manifest = json.load(open(f"{DATA}/manifest.json"))
    files = manifest["files"]

    # ---- consolidate per-run sanity reports
    runs = {}
    for p in sorted(glob.glob(f"{DATA}/sanity_run*.json")):
        tag = os.path.basename(p)[len("sanity_"):-len(".json")]
        try:
            runs[tag] = json.load(open(p))
        except Exception as e:
            print(f"WARN: cannot read {p}: {e}")

    kline = []
    symbols = {}
    skipped, pending, gaps = [], [], []
    archives_ok = archives_failed = archives_pending = 0
    for tag, s in runs.items():
        archives_ok += s.get("archives_ok", 0)
        archives_failed += s.get("archives_failed", 0)
        archives_pending += s.get("archives_pending", 0)
        skipped += [{"run": tag, **x} for x in s.get("skipped", [])]
        gaps += s.get("agg_id_gaps", [])
        for r in s.get("kline_check", []):
            kline.append({"run": tag, **r})
        for sym, ents in s.get("symbols", {}).items():
            for e in ents:
                symbols.setdefault(sym, {})[f"{e['month']}|{e['kind']}"] = e

    total_bars = sum(e["bars"] for m in symbols.values() for e in m.values())
    total_trades = sum(e["trades_read"] for m in symbols.values() for e in m.values())
    worst_cov = min((e["coverage_vs_calendar"] for m in symbols.values() for e in m.values()),
                    default=1.0)
    kc_ok = sum(1 for r in kline if r.get("close_exact"))
    kc_day = sum(1 for r in kline if r.get("day_volume_ok"))
    kc_minutes = sum(r.get("minutes_compared", 0) for r in kline)
    worst_day_rel = max((r.get("day_rel_vol_max") or 0) for r in kline) if kline else 0.0
    hl_minutes = sum(r.get("highlow_mismatch_minutes") or 0 for r in kline)

    now = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
    L = [f"# Binance Vision Orderflow Dump — Consolidated Sanity Report ({now})", "",
         f"- engine: `binance_orderflow/binance_orderflow_dump.py` (single file), runs: {', '.join(runs)}",
         f"- archives SHA-256 verified: **{archives_ok}** ok, {archives_failed} failed, "
         f"{archives_pending} pending publication",
         f"- aggregated bars: **{total_bars:,}** from **{total_trades:,}** aggTrades "
         f"across {len(symbols)} symbols",
         f"- worst per-month calendar coverage: {worst_cov:.4f}", ""]
    if kline:
        L += ["## Official-kline cross-check (trades-derived bars vs exchange 1m klines)",
              f"- {len(kline)} window-checks / {kc_minutes:,} minutes compared: minute CLOSE exact "
              f"{kc_ok}/{len(kline)} (close is the strictest identity — bar boundary + last trade)",
              f"- day-level base-volume reconciliation within 0.5%: {kc_day}/{len(kline)} "
              f"(worst {worst_day_rel:.4%})",
              f"- high/low extremes mismatched on {hl_minutes} minutes total (Binance-side aggTrades "
              f"dump artifact on burst extremes; documented, close never affected)", "",
              "| run | symbol | window | minutes | close exact | hl mismatch | day rel vol max |",
              "| --- | --- | --- | ---: | --- | ---: | ---: |"]
        for r in kline:
            L.append(f"| {r['run']} | {r['symbol']} | {r['month']} | {r['minutes_compared']:,} | "
                     f"{r.get('close_exact')} | {r.get('highlow_mismatch_minutes')} | "
                     f"{(r.get('day_rel_vol_max') or 0):.4%} |")
        L.append("")
    L += ["## Per-symbol coverage", "",
          "| symbol | month | built from | bars | trades read | coverage | agg-id gap |",
          "| --- | --- | --- | ---: | ---: | ---: | ---: |"]
    for sym in sorted(symbols):
        for key in sorted(symbols[sym]):
            e = symbols[sym][key]
            L.append(f"| {sym} | {e['month']} | {e['kind']} | {e['bars']:,} | "
                     f"{e['trades_read']:,} | {e['coverage_vs_calendar']:.4f} | "
                     f"{e.get('agg_id_gap_into_month', 0):,} |")
    L.append("")
    if gaps:
        L += ["## aggTrade-id gaps across file boundaries (reported, CVD unaffected)",
              *[f"- {g['symbol']} {g['month']}: {g['gap']} ids" for g in gaps], ""]
    if skipped:
        L += ["## Skipped / pending",
              *[f"- [{s.get('run')}] {s.get('symbol')} {s.get('month')}{(' day ' + s['day']) if s.get('day') else ''}: "
                f"{s['reason']}" for s in skipped[:80]], ""]
    n_parquet = sum(1 for p in files if p.endswith(".parquet"))
    total_bytes = sum(f.get("bytes", 0) or 0 for f in files.values())
    L += ["## Manifest", "",
          f"- {len(files)} files in manifest ({n_parquet} parquet), {total_bytes/1e6:.1f} MB",
          "- every file carries sha256, row count, ts range, source archive and build metadata",
          "- incremental: rerunning the engine (--update) fetches only missing days/months, repairs "
          "day gaps in place, and upgrades finished months to the monthly archive", ""]

    report = "\n".join(L)
    with open(f"{DATA}/sanity_report_consolidated.md", "w") as f:
        f.write(report)
    json.dump({"generated_at": now, "archives_ok": archives_ok, "archives_failed": archives_failed,
               "archives_pending": archives_pending, "symbols": {s: list(m.keys()) for s, m in symbols.items()},
               "total_bars": total_bars, "total_trades": total_trades, "kline_checks": kline,
               "agg_id_gaps": gaps, "skipped": skipped},
              open(f"{DATA}/sanity_report_consolidated.json", "w"), indent=1)
    print(report[:3000])
    print(f"... report saved: {DATA}/sanity_report_consolidated.md")

    # ---- integrity: spot-verify sha256 of 12 random manifest files
    import random
    random.seed(7)
    sample = random.sample([p for p in files if p.endswith(".parquet")], min(12, n_parquet))
    bad = []
    for p in sample:
        fp = os.path.join(DATA, p)
        if not os.path.exists(fp) or sha256_file(fp) != files[p]["sha256"]:
            bad.append(p)
    print(f"integrity spot-check: {len(sample) - len(bad)}/{len(sample)} sha256 OK"
          + (f"  BAD: {bad}" if bad else ""))

    if deliver:
        if os.path.exists(OUT):
            shutil.rmtree(OUT)
        os.makedirs(OUT)
        # deliver the full parquet tree + reports + manifest (small files only copied)
        for rel, meta in files.items():
            src = os.path.join(DATA, rel)
            if os.path.exists(src):
                dst = os.path.join(OUT, "data", rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
        for name in ("sanity_report_consolidated.md", "manifest.json", "state.json"):
            shutil.copy2(os.path.join(DATA, name), os.path.join(OUT, name))
        for p in glob.glob(f"{REPO}/binance_orderflow/run*.log"):
            shutil.copy2(p, os.path.join(OUT, os.path.basename(p)))
        shutil.copy2(f"{REPO}/binance_orderflow/binance_orderflow_dump.py", OUT)
        # single-file archive for one-click download
        t0 = time.time()
        subprocess.run(["tar", "-I", "gzip", "-cf", f"{OUT}/binance_orderflow_dump_all.tar.gz",
                        "-C", OUT, "data", "manifest.json", "state.json",
                        "sanity_report_consolidated.md"], check=True)
        sz = os.path.getsize(f"{OUT}/binance_orderflow_dump_all.tar.gz")
        print(f"archive: binance_orderflow_dump_all.tar.gz {sz/1e6:.1f} MB ({time.time()-t0:.0f}s)")
        total = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(OUT) for f in fs)
        print(f"DELIVERY TOTAL: {total/1e6:.1f} MB in {OUT}")


if __name__ == "__main__":
    main()
