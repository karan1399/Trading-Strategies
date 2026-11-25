#!/bin/bash
cd /home/z/my-project
python3 binance_orderflow/binance_orderflow_dump.py --assets BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,AVAXUSDT,DOTUSDT,LTCUSDT,TRXUSDT \
  --from 2026-10-01 --to 2026-11-23 \
  > binance_orderflow/run_research.log 2>&1
cp binance_orderflow/data/sanity_report.json binance_orderflow/data/sanity_research.json 2>/dev/null
echo "RESEARCH DOWNLOAD DONE exit=$?"
