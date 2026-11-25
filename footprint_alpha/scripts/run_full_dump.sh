#!/bin/bash
# Full historical orderflow dump — sequential runs (single process: manifest/state safety)
cd /home/z/my-project
D=binance_orderflow/data
mkdir -p "$D"

echo "=== RUN A: all 30 suite assets, 2026-10-01 -> 2026-11-23 ==="
python3 binance_orderflow/binance_orderflow_dump.py --assets SUITE --from 2026-10-01 --to 2026-11-23 \
  > binance_orderflow/runA.log 2>&1
cp "$D/sanity_report.json" "$D/sanity_runA.json" 2>/dev/null
echo "RUN A exit=$?"

echo "=== RUN C: core-3 deeper window, 2026-06-01 -> 2026-09-30 ==="
python3 binance_orderflow/binance_orderflow_dump.py --assets BTCUSDT,ETHUSDT,SOLUSDT --from 2026-06-01 --to 2026-09-30 \
  > binance_orderflow/runC.log 2>&1
cp "$D/sanity_report.json" "$D/sanity_runC.json" 2>/dev/null
echo "RUN C exit=$?"

echo "=== RUN B: era-proof dailies with kline cross-check ==="
for spec in "BTCUSDT 2019-09-10 B1" "BTCUSDT 2020-01-15 B2" "BTCUSDT 2021-01-15 B3" "BTCUSDT,ETHUSDT 2024-06-01 B4"; do
  set -- $spec
  python3 binance_orderflow/binance_orderflow_dump.py --assets "$1" --from "$2" --to "$2" --kline-check \
    > "binance_orderflow/run$3.log" 2>&1
  cp "$D/sanity_report.json" "$D/sanity_run$3.json" 2>/dev/null
  echo "RUN $3 exit=$?"
done
echo "=== RUN B5: spot era proof (8-col, us, True/False) ==="
python3 binance_orderflow/binance_orderflow_dump.py --market spot --assets BTCUSDT --from 2026-11-22 --to 2026-11-22 --raw --kline-check \
  > binance_orderflow/runB5.log 2>&1
cp "$D/sanity_report.json" "$D/sanity_runB5.json" 2>/dev/null
echo "RUN B5 exit=$?"

echo "=== ALL RUNS DONE ==="
du -sh "$D"
