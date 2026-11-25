#!/bin/bash
cd /home/z/my-project
python3 binance_orderflow/binance_orderflow_dump.py \
  --assets BCHUSDT,XLMUSDT,UNIUSDT,ATOMUSDT,FILUSDT,NEARUSDT,XMRUSDT,ALGOUSDT \
  --from 2026-10-01 --to 2026-11-23 > binance_orderflow/run_universe20.log 2>&1
echo "UNIVERSE20 DOWNLOAD DONE exit=$?"
