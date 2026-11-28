"""Re-fit the whole local 26-strategy grid on settled labels using read-only cached features."""

import argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from contextlib import redirect_stdout
from hashlib import file_digest
import json
from pathlib import Path
import sys

import pandas as pd

from desk.quant_audits.r047_review import load_bars, offline_oi
from footprint_Chart.backtesting import strategy_26_5k_optimizer as optimizer


def refit_one(inputs):
    symbol, spec, directory = inputs
    optimizer.load_asset_tfs_cached = load_bars
    optimizer.ensure_full_metrics = offline_oi
    before_path = optimizer.CFG_DIR / f"{symbol}.json"
    before = json.loads(before_path.read_text(encoding="utf-8"))
    with redirect_stdout(sys.stderr):
        after = optimizer._optimize_asset_all26(symbol, spec, optimizer.discover_chart_strategies(),
                                                force=True, output_dir=directory)
    sources = [*(optimizer.DATA_DIR / "cache" / f"{symbol}_{tf}.parquet" for tf in ("2h", "4h", "m1")),
               optimizer.DATA_DIR / f"{symbol}_metrics_5m_full.parquet"]
    hashes = {}
    for source in sources:
        if source.exists():
            with source.open("rb") as stream:
                hashes[source.name] = file_digest(stream, "sha256").hexdigest()
    if after is None:
        return symbol, {"status": "missing_history", "inputs_sha256": hashes}
    original = {r["sid"]: r for r in before["strategy_scorecard"]}
    changes = []
    for row in after["strategy_scorecard"]:
        old = original.get(row["sid"], {})
        changes.append({"sid": row["sid"], "old_config": old.get("cfg"), "new_config": row["cfg"],
                        "old_train_n": old.get("nis"), "train_n": row["nis"], "train_is_ok": row["is_ok"],
                        "train_net_r": row["isr"], "validation_n": row["noos"],
                        "validation_r": row["oosr"], "validation_pf": row["oospf"]})
    return symbol, {"status": "refit", "pairs": changes, "inputs_sha256": hashes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=2)
    opts = parser.parse_args()
    if opts.output_dir.exists():
        parser.error("Use a fresh output directory for this isolated refit")
    specs = json.loads(optimizer.MUTUAL_SPECS_PATH.read_text(encoding="utf-8"))
    items = [(p.stem, specs[p.stem], opts.output_dir)
             for p in sorted(optimizer.CFG_DIR.glob("*.json")) if p.stem in specs]
    with ProcessPoolExecutor(max_workers=opts.workers) as workers:
        asset_reports = dict(workers.map(refit_one, items))
    families = defaultdict(list)
    for symbol, result in asset_reports.items():
        for row in result.get("pairs", []):
            families[row["sid"]].append(dict(row, symbol=symbol))
    totals = {}
    for sid, rows in sorted(families.items()):
        qualified = [row for row in rows if row["train_is_ok"]]
        totals[sid] = {
            "refit_pairs": len(rows), "changed_recipe_count": sum(r["old_config"] != r["new_config"] for r in rows),
            "is_qualified_pairs": len(qualified),
            "validation_positive_among_is_qualified": sum(r["validation_r"] > 0 for r in qualified),
            "validation_r_among_is_qualified": round(sum(r["validation_r"] for r in qualified), 2),
        }
    with redirect_stdout(sys.stderr):
        master = optimizer.build_26_accounts(specs, output_dir=opts.output_dir)
    print(json.dumps({"cost_model": "snapshot_research_proxy", "families": totals,
                      "per_asset": asset_reports, "isolated_stage_b": master}, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
