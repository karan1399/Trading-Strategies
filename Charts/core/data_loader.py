"""Extensible OHLCV data discovery and loader for historical_parquet."""

from __future__ import annotations

import os
from pathlib import Path
import pandas as pd
import pyarrow.parquet as pq

DEFAULT_PARQUET_ROOT = Path(
    os.environ.get(
        "CHARTS_PARQUET_ROOT",
        r"C:\Users\SIGMA\Documents\Trading-Strategies\historical_parquet",
    )
)

_TF_ORDER = {"1M": 0, "5M": 1, "15M": 2, "30M": 3, "1H": 4, "4H": 5, "1D": 6}


def list_timeframes(root: Path = DEFAULT_PARQUET_ROOT) -> list[str]:
    """Return available timeframe subdirectories sorted chronologically."""
    if not root.exists():
        return []
    dirs = [d.name for d in root.iterdir() if d.is_dir()]
    return sorted(dirs, key=lambda name: (_TF_ORDER.get(name.upper(), 99), name))


def list_symbols(timeframe: str, root: Path = DEFAULT_PARQUET_ROOT) -> list[str]:
    """Return available symbol names (without .parquet) for a timeframe."""
    tf_dir = root / timeframe
    if not tf_dir.is_dir():
        return []
    return sorted(p.stem for p in tf_dir.glob("*.parquet") if p.is_file())


def load_ohlcv(
    symbol: str,
    timeframe: str,
    limit: int | None = None,
    start_ts: int | None = None,
    end_ts: int | None = None,
    root: Path = DEFAULT_PARQUET_ROOT,
) -> pd.DataFrame:
    """Load and normalize OHLCV bars from a parquet file.

    Returns a DataFrame sorted ascending by ``timestamp`` (UTC epoch seconds)
    with columns: ``timestamp, datetime, open, high, low, close, volume, spread``.
    """
    path = root / timeframe / f"{symbol}.parquet"
    if not path.is_file():
        raise FileNotFoundError(f"Parquet file not found: {path}")

    df = pq.read_table(path).to_pandas()
    if df.empty:
        return pd.DataFrame(
            columns=["timestamp", "datetime", "open", "high", "low", "close", "volume", "spread"]
        )

    if "timestamp" not in df.columns and "datetime" in df.columns:
        dt = pd.to_datetime(df["datetime"], utc=True)
        df["timestamp"] = (dt.astype("int64") // 10**9).astype("int64")
    else:
        df["timestamp"] = df["timestamp"].astype("int64")

    if "datetime" not in df.columns:
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    else:
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True)

    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype("float64")

    if "volume" in df.columns:
        df["volume"] = df["volume"].astype("float64")
    elif "tick_volume" in df.columns:
        df["volume"] = df["tick_volume"].astype("float64")
    elif "real_volume" in df.columns:
        df["volume"] = df["real_volume"].astype("float64")
    else:
        df["volume"] = 0.0

    df["spread"] = df["spread"].astype("float64") if "spread" in df.columns else 0.0

    df = (
        df[["timestamp", "datetime", "open", "high", "low", "close", "volume", "spread"]]
        .drop_duplicates(subset=["timestamp"], keep="last")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

    if start_ts is not None:
        df = df[df["timestamp"] >= int(start_ts)]
    if end_ts is not None:
        df = df[df["timestamp"] <= int(end_ts)]
    if limit is not None and limit > 0 and len(df) > limit:
        df = df.iloc[-int(limit) :]

    return df.reset_index(drop=True)
