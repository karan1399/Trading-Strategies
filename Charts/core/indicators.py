"""Extensible technical indicator registry for Charts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class IndicatorSpec:
    """Metadata and compute hook for a chart indicator."""

    id: str
    name: str
    pane: str  # "main" or "oscillator"
    color: str
    fn: Callable[[pd.DataFrame], dict[str, pd.Series]]


INDICATOR_REGISTRY: dict[str, IndicatorSpec] = {}


def register_indicator(ind_id: str, name: str, pane: str = "main", color: str = "#38bdf8"):
    """Register an indicator computation function by ID."""

    def _decorator(fn: Callable[[pd.DataFrame], dict[str, pd.Series]]):
        INDICATOR_REGISTRY[ind_id] = IndicatorSpec(
            id=ind_id, name=name, pane=pane, color=color, fn=fn
        )
        return fn

    return _decorator


def ema_series(series: pd.Series, period: int) -> pd.Series:
    """Compute exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def wilder_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute Wilder Average True Range."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    prev_close = df["close"].astype(float).shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def wilder_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Compute Wilder Relative Strength Index."""
    delta = series.astype(float).diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


@register_indicator("ema_13", "EMA 13", pane="main", color="#38bdf8")
def _ind_ema_13(df: pd.DataFrame) -> dict[str, pd.Series]:
    return {"EMA 13": ema_series(df["close"], 13)}


@register_indicator("ema_89", "EMA 89", pane="main", color="#f59e0b")
def _ind_ema_89(df: pd.DataFrame) -> dict[str, pd.Series]:
    return {"EMA 89": ema_series(df["close"], 89)}


@register_indicator("ema_233", "EMA 233", pane="main", color="#ef4444")
def _ind_ema_233(df: pd.DataFrame) -> dict[str, pd.Series]:
    return {"EMA 233": ema_series(df["close"], 233)}


@register_indicator("bb_20", "Bollinger Bands (20, 2)", pane="main", color="#a78bfa")
def _ind_bb_20(df: pd.DataFrame) -> dict[str, pd.Series]:
    mid = df["close"].rolling(20, min_periods=1).mean()
    std = df["close"].rolling(20, min_periods=1).std(ddof=0).fillna(0.0)
    return {
        "BB Upper": mid + 2.0 * std,
        "BB Mid": mid,
        "BB Lower": mid - 2.0 * std,
    }


@register_indicator("vwap_day", "UTC Day VWAP", pane="main", color="#22d3ee")
def _ind_vwap_day(df: pd.DataFrame) -> dict[str, pd.Series]:
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].replace(0.0, 1.0)
    day_key = df["datetime"].dt.strftime("%Y-%m-%d")
    cum_pv = (tp * vol).groupby(day_key).cumsum()
    cum_v = vol.groupby(day_key).cumsum()
    return {"Day VWAP": cum_pv / cum_v}


@register_indicator("rsi_14", "RSI (14)", pane="oscillator", color="#c084fc")
def _ind_rsi_14(df: pd.DataFrame) -> dict[str, pd.Series]:
    return {"RSI(14)": wilder_rsi(df["close"], 14)}


@register_indicator("sma_50", "SMA 50", pane="main", color="#facc15")
def _ind_sma_50(df: pd.DataFrame) -> dict[str, pd.Series]:
    return {"SMA 50": df["close"].rolling(50, min_periods=1).mean()}


@register_indicator("sma_200", "SMA 200", pane="main", color="#fb923c")
def _ind_sma_200(df: pd.DataFrame) -> dict[str, pd.Series]:
    return {"SMA 200": df["close"].rolling(200, min_periods=1).mean()}


@register_indicator("macd_12_26_9", "MACD (12,26,9)", pane="oscillator", color="#38bdf8")
def _ind_macd(df: pd.DataFrame) -> dict[str, pd.Series]:
    fast = ema_series(df["close"], 12)
    slow = ema_series(df["close"], 26)
    macd_line = fast - slow
    signal_line = ema_series(macd_line, 9)
    return {"MACD": macd_line, "MACD Signal": signal_line}


def list_indicators() -> list[dict[str, str]]:
    """Return metadata list for UI indicator toggles."""
    return [
        {"id": spec.id, "name": spec.name, "pane": spec.pane, "color": spec.color}
        for spec in INDICATOR_REGISTRY.values()
    ]


def compute_indicators(df: pd.DataFrame, indicator_ids: list[str]) -> list[dict]:
    """Compute requested indicators and format them for Lightweight Charts."""
    times = df["timestamp"].tolist()
    outputs: list[dict] = []
    for ind_id in indicator_ids:
        spec = INDICATOR_REGISTRY.get(ind_id)
        if spec is None:
            continue
        series_map = spec.fn(df)
        for label, s in series_map.items():
            points = []
            for t, val in zip(times, s.tolist()):
                if val is not None and np.isfinite(val):
                    points.append({"time": int(t), "value": round(float(val), 6)})
            outputs.append(
                {
                    "id": f"{ind_id}:{label}",
                    "name": label,
                    "pane": spec.pane,
                    "color": spec.color,
                    "data": points,
                }
            )
    return outputs
