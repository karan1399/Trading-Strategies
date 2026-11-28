"""Point-in-time quote, minimum-lot and cash-flow inputs for the prop research replay."""

from bisect import bisect_right
import math

import numpy as np


class ExecutionTape:
    def __init__(self, quotes, rollover, *, cashflow_coverage, max_quote_age=300,
                 max_spread_bps=50.0, risk_usd=6.0):
        self.quotes = sorted(quotes, key=lambda row: row["available_at"])
        self.times = [int(row["available_at"]) for row in self.quotes]
        if not self.times or len(set(self.times)) != len(self.times):
            raise ValueError("An execution tape requires unique point-in-time quote observations")
        for quote in self.quotes:
            for key in ("spread_bps", "commission_bps", "min_stop_distance", "tick_value_loss", "tick_size",
                        "vol_min", "vol_max", "vol_step"):
                if not math.isfinite(quote[key]) or quote[key] < 0:
                    raise ValueError(f"Invalid execution quote field {key}")
            if (min(quote["tick_value_loss"], quote["tick_size"], quote["vol_min"], quote["vol_step"]) <= 0
                    or quote["vol_min"] > quote["vol_max"]):
                raise ValueError("Lot and tick economics must be positive")
        self.rollover = sorted(rollover, key=lambda row: row["cashflow_ts"])
        self.rollover_times = [int(row["cashflow_ts"]) for row in self.rollover]
        self.cashflow_start, self.cashflow_end = map(int, cashflow_coverage)
        if self.cashflow_start >= self.cashflow_end:
            raise ValueError("Cashflow coverage must have a start and end")
        if len(set(self.rollover_times)) != len(self.rollover_times):
            raise ValueError("Rollover cashflows must have unique timestamps")
        for row in self.rollover:
            if any(not math.isfinite(row[k]) for k in ("buy_bps", "sell_bps")):
                raise ValueError("Rollover debits must be finite")
        if (not all(math.isfinite(v) for v in (max_quote_age, risk_usd, max_spread_bps))
                or max_quote_age < 0 or risk_usd <= 0 or max_spread_bps < 0):
            raise ValueError("Invalid execution tape limits")
        self.max_spread_bps = max_spread_bps
        self.max_quote_age = max_quote_age
        self.risk_usd = risk_usd

    def quote_at(self, ts):
        i = bisect_right(self.times, int(ts)) - 1
        if i >= 0 and int(ts) - self.times[i] <= self.max_quote_age:
            return self.quotes[i]
        return None

    def sizing_terms(self, ts, entry, stop):
        quote = self.quote_at(ts)
        if quote is None or not all(math.isfinite(v) and v > 0 for v in (entry, stop)):
            return None
        spread_price = entry * quote["spread_bps"] / 10_000.0
        stop_floor = max(quote["min_stop_distance"] + spread_price, 5 * spread_price)
        reserve = entry * (quote["spread_bps"] + 2 * quote["commission_bps"] + 15) / 10_000.0
        tick_per_price = quote["tick_value_loss"] / quote["tick_size"]
        gross = stop * tick_per_price
        stressed = (stop + reserve) * tick_per_price
        if (quote["spread_bps"] > self.max_spread_bps or stop < stop_floor
                or stressed * quote["vol_min"] > self.risk_usd):
            return None
        return {"gross_per_lot": gross, "stressed_per_lot": stressed, "hard_risk_cap_usd": self.risk_usd,
                **{field: quote[field] for field in ("vol_min", "vol_max", "vol_step")}}

    def admissible(self, ts, entry, stop):
        return self.sizing_terms(ts, entry, stop) is not None

    def filter_entries(self, signals, bar_ends, atr, sl_atr, minute_ts, minute_open):
        selected = np.array(signals, dtype=np.int8, copy=True)
        for i in np.flatnonzero(selected):
            entry_i = int(np.searchsorted(minute_ts, bar_ends[i])) + 1
            if (entry_i >= len(minute_ts) or minute_ts[entry_i] - bar_ends[i] > 300
                    or not self.admissible(minute_ts[entry_i], minute_open[entry_i], atr[i] * sl_atr)):
                selected[i] = 0
        return selected

    def apply_costs(self, result, stop_distances):
        """Add bid/ask, commissions and observed carry to zero-spread, zero-carry kernel costs."""
        _, entries, exits, sides, _, prices, exit_prices, net, costs, _ = result
        for i, (entry, exit_) in enumerate(zip(entries, exits)):
            quote_in = self.quote_at(entry)
            quote_out = self.quote_at(exit_ + 60)
            if quote_in is None or quote_out is None:
                raise ValueError("A modeled fill lacks a contemporaneous cost observation")
            if entry < self.cashflow_start or exit_ + 60 > self.cashflow_end:
                raise ValueError("A modeled fill lacks complete rollover coverage")
            debit_in = 0.5 * quote_in["spread_bps"] + quote_in["commission_bps"]
            debit_out = 0.5 * quote_out["spread_bps"] + quote_out["commission_bps"]
            lo = bisect_right(self.rollover_times, int(entry))
            hi = bisect_right(self.rollover_times, int(exit_ + 60))
            side = "buy_bps" if sides[i] == 1 else "sell_bps"
            carry = sum(row[side] for row in self.rollover[lo:hi])
            extra_r = ((debit_in + carry) * prices[i] + debit_out * exit_prices[i]) / (10_000 * stop_distances[i])
            net[i] -= extra_r
            costs[i] += extra_r


def replay_lot_risk(trade, target, equity):
    """Report gross P&L risk and stressed reserved risk at a supported broker lot."""
    stressed = trade.get("stressed_per_lot")
    if stressed is None or not math.isfinite(stressed) or stressed <= 0:
        return None
    minimum, step = trade["vol_min"], trade["vol_step"]
    cap = min(target, equity * 0.01, trade["hard_risk_cap_usd"])
    max_lots = min(trade["vol_max"], cap / stressed)
    if max_lots < minimum:
        return None
    lots = minimum + step * math.floor((max_lots - minimum) / step + 1e-10)
    if lots * stressed > cap + 1e-9:
        return None
    return lots * trade["gross_per_lot"], lots * stressed
