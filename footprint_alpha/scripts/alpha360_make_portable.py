#!/usr/bin/env python3
"""Make the repo alpha360_ema_rejection/ folder self-contained for local runs:
patch sandbox-hardcoded paths to relative ones, copy dfib_engine + robustness
in, and add the daily signal watcher."""
import os
import re
import shutil

SRC = "/home/z/my-project/scripts"
DEST = "/home/z/my-project/repos/karan-ts/alpha360_ema_rejection"
os.makedirs(DEST, exist_ok=True)

# ---- dfib_engine.py: auto-detect repo root, optional candle_rejection import
eng = open(os.path.join(SRC, "dfib_engine.py")).read()
eng = eng.replace(
    'REPO = "/home/z/my-project/repos/karan-ts"\nPARQUET_ROOT = os.path.join(REPO, "historical_parquet")',
    '_HERE = os.path.dirname(os.path.abspath(__file__))\n'
    '_REPO = os.path.dirname(_HERE)  # repo root when this file sits in alpha360_ema_rejection/\n'
    'PARQUET_ROOT = os.environ.get("ALPHA360_PARQUET_ROOT") or os.path.join(_REPO, "historical_parquet")')
eng = eng.replace(
    'sys.path.insert(0, os.path.join(REPO, "Delta-Fibonacci-Institutional-Business"))\n'
    'from engine.candle_rejection import (  # noqa: E402\n'
    '    check_vwap_band_signal, check_ema_rejection_signal, evaluate_level_rejection)',
    'try:  # optional: only needed for the Delta-Fibonacci pattern family\n'
    '    sys.path.insert(0, os.path.join(_REPO, "Delta-Fibonacci-Institutional-Business"))\n'
    '    from engine.candle_rejection import (  # noqa: E402\n'
    '        check_vwap_band_signal, check_ema_rejection_signal, evaluate_level_rejection)\n'
    'except Exception:\n'
    '    pass')
open(os.path.join(DEST, "dfib_engine.py"), "w").write(eng)

shutil.copy2(os.path.join(SRC, "dfib_robustness.py"), os.path.join(DEST, "dfib_robustness.py"))

# ---- alpha360 scripts: point sys.path at the folder itself
for f in ["alpha360_engine.py", "alpha360_common.py", "alpha360_sweep_a.py",
          "alpha360_sweep_b.py", "alpha360_stage_c.py", "alpha360_summarize.py",
          "alpha360_watch.py"]:
    src = open(os.path.join(SRC, f)).read()
    src = src.replace(
        'sys.path.insert(0, "/home/z/my-project/scripts")',
        'import os as _os\nsys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)) or ".")')
    open(os.path.join(DEST, f), "w").write(src)

print("portable engine files written to", DEST)
for f in sorted(os.listdir(DEST)):
    print("  ", f, os.path.getsize(os.path.join(DEST, f)) // 1024, "KB")
