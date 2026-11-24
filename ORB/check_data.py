import pandas as pd
from pathlib import Path

root = Path("historical_parquet")
symbols = ["SP500.p", "NAS100.p", "DJ30.p", "GER40.p", "UK100.p", "JP225.p", "XAUUSD.pi", "BTCUSD.pi", "EURUSD.pi", "USWTI.p"]

for sym in symbols:
    print(f"=== {sym} ===")
    for tf in ["5M", "15M", "1H", "4H"]:
        p = root / tf / f"{sym}.parquet"
        if p.exists():
            df = pd.read_parquet(p)
            t0 = str(df["datetime"].iloc[0])[:19]
            t1 = str(df["datetime"].iloc[-1])[:19]
            print(f"  {tf:3s}: {len(df):5d} bars | {t0} -> {t1}")
        else:
            print(f"  {tf:3s}: NOT FOUND")
