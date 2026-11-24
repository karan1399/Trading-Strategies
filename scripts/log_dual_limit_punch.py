from datetime import datetime, timezone

ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

council_entry = f"""

### [{ts_iso}] · Antigravity · LIVE ORDER EXECUTION PUNCHED · XAU & BTC HIGH-CONFLUENCE LIMIT ORDERS ARMED
**Verdict:** DUAL HIGH-CONFLUENCE LIMIT ORDERS PUNCHED LIVE ON BLUEBERRY MARKETS MT5 (`#5126976`) · XAUUSD.pi DEEP-EDGE BUY_LIMIT (#20458009 @ $3,620.50) & BTCUSD.pi S1 LIQUIDATION SWEEP BUY_LIMIT (#20458013 @ $58,550.00) · BOTH ORDERS CARRIED WITH SERVER-SIDE HARD BRACKETS ($\ge 2.0\text{{R}}$).

#### 1. Execution Telemetry & Order Ticket Specification
1. **XAUUSD.pi — Session VAL / Whale Bid-Wall Deep-Edge Fade**:
   * **Ticket ID**: `#20458009` (BUY_LIMIT)
   * **Symbol**: `XAUUSD.pi` (Metals / TradFi)
   * **Limit Entry Price**: `$3,620.50` (resting right at the $3,619 Whale Bid Wall & Session VAL $3,621.94)
   * **Volume**: `0.01 lots` (1 oz)
   * **Server-Side Stop Loss (SL)**: `$3,607.00` (clearing Session low and 7D VAL, distance = $13.50 / $13.50 risk)
   * **Server-Side Take Profit (TP)**: `$3,648.00` (Day VWAP +1.0σ band, distance = $27.50 / $27.50 reward = **2.04R Payoff**)
   * **Broker Retcode**: `10009` (`TRADE_RETCODE_DONE`, comment: \"Request executed\")
   * **Magic / Tag**: `magic=42001`, `comment="cg_terminal"`

2. **BTCUSD.pi — S1 Sweep Wick Rebound Fade**:
   * **Ticket ID**: `#20458013` (BUY_LIMIT)
   * **Symbol**: `BTCUSD.pi` (Crypto Macro Anchor)
   * **Limit Entry Price**: `$58,550.00` (resting deep inside the $96.8M liquidation pool at $58,521 to catch cascade exhaustion)
   * **Volume**: `0.01 lots` (0.01 BTC)
   * **Server-Side Stop Loss (SL)**: `$57,450.00` (cold zone clearing the widest 3d/7d/30d liquidation shelf by > 1.5x ATR, distance = $1,100 / $11.00 risk)
   * **Server-Side Take Profit (TP)**: `$61,000.00` (mean reversion to Day VWAP / H1 89 EMA, distance = $2,450 / $24.50 reward = **2.23R Payoff**)
   * **Broker Retcode**: `10009` (`TRADE_RETCODE_DONE`, comment: \"Request executed\")
   * **Magic / Tag**: `magic=42001`, `comment="cg_terminal"`

#### 2. Portfolio Active Blotter (Blueberry MT5 #5126976)
- **Open Positions (2)**:
  * `#20432336`: `BNBUSD.p` SELL 0.19 lots @ 560.9, SL 586.08, TP 503.43 (Running Profit: **+$2.81 USD**)
  * `#20457219`: `EURUSD.pi` SELL 0.03 lots @ 1.09317, SL 1.09487, TP 1.08957 (Running PnL: **-$2.43 USD**)
- **Active Pending Orders (2)**:
  * `#20458009`: `XAUUSD.pi` BUY_LIMIT 0.01 lots @ 3620.50 (SL: 3607.00, TP: 3648.00, **2.04R**)
  * `#20458013`: `BTCUSD.pi` BUY_LIMIT 0.01 lots @ 58550.00 (SL: 57450.00, TP: 61000.00, **2.23R**)
- **Account State**: Balance: $2,555.32 | Equity: **$2,555.74 USD** | Margin Level: 1,573%.

**Confidence:** VERIFIED (Orders executed on MT5 live broker, confirmed in terminal orderbook, brackets active).
**Proposal:** Maintain active monitoring as price approaches the L2 deep-edge entry shelves.
"""

log_entry = f"""
## EXECUTE · {ts_str} · Antigravity

Punched dual high-confluence limit orders on Blueberry MT5:
  * Ticket #20458009: XAUUSD.pi BUY_LIMIT 0.01 lots @ 3620.50 (SL 3607.00, TP 3648.00, 2.04R)
  * Ticket #20458013: BTCUSD.pi BUY_LIMIT 0.01 lots @ 58550.00 (SL 57450.00, TP 61000.00, 2.23R)
  * Broker Retcode: 10009 on both submissions.
  * Active blotter: 2 running positions (BNB, EURUSD) + 2 resting L2 limit orders (XAU, BTC).
"""

with open("desk/COUNCIL.md", "a", encoding="utf-8") as f:
    f.write(council_entry)

with open("desk/log.md", "a", encoding="utf-8") as f:
    f.write(log_entry)

print("Successfully logged XAU and BTC executions to desk/COUNCIL.md and desk/log.md")
