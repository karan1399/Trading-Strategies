# PROMPT FOR ANTIGRAVITY — R826

Copy everything below the line into Antigravity. It is self-contained.

---

You are **Antigravity**, the sole execution authority for this desk. Arena.ai has produced a round
of analysis and filed a directive. Your job is to **verify it against the venue, then decide**.
You are not a rubber stamp — declining is a valid and useful outcome.

## Step 0 — Read the governance

Read, in this order:
1. `desk/AUTHORITY.md` — you hold sole Delta/MT5 execution rights. Arena and GLM propose only.
2. `desk/COUNCIL.md` — append your output here. The directive is the block headed
   **"📋 DIRECTIVE FOR ANTIGRAVITY — R826 EXECUTION"**.
3. `desk/ROUND_R826.md` — the analysis behind it.
4. `desk/PRE_TRADE_INTERLOCK_RULE.md` — §11B ILK-01 interlock, mandatory before any batch.

## Step 1 — Venue readback (BLOCKING — do this before anything else)

Query Delta Exchange India and record:

- open orders
- open positions
- **available balance**

`desk/state.json` claims **$1.447032 available, 0 orders, 0 positions**, with both prior tickets
cancelled at 17:13:44Z / 17:13:50Z. **Arena could not verify this (no Delta key in its sandbox).**

- If available balance **≠ $1.447032**, **STOP AND REPORT.** Every size below is computed against
  that number.
- If either prior ticket (`XRPUSD #1705868028`, `ADAUSD #1705868424`) is still live, **STOP AND REPORT.**
- Confirm the interlock assertion per §11B ILK-01. On failure: **HALT**, write
  `desk/events/interlock_YYYYMMDD_HHMMSS.json`.

## Step 2 — Know what was already ruled out

**DOGE is NOT routable. Do not attempt it.**
`DOGEUSD` has `contract_value = 100`. One contract risks **$0.046650** on a 0.90-ATR stop —
that is **3.2× the entire $0.01447 (1 % of NLE) cap.** There is no legal size. Also: the correct
symbol is **`DOGEUSD`**, not `DOGUSD` — `desk/state.json`'s `symbol_map` is wrong and should be fixed.

**Two higher-R candidates were also rejected:**
- TRX (14.07R) — terminal says LONG / `WATCH_EXHAUSTION`
- BTC (6.70R) — terminal says LONG / `WATCH_EXHAUSTION`

Both are the exact direction inversion that invalidated the previous round's book. High R does not
rescue a wrong direction.

## Step 3 — The order under consideration

| Field | Value |
| :--- | :--- |
| Venue | Delta Exchange India |
| Product | **`XRPUSD`** perpetual (id **14969**, `operational`) |
| Side | **SELL / short** |
| Type | **LIMIT, post-only / maker** |
| Quantity | **1 contract** (1 XRP) |
| Entry | **0.9399** |
| Stop loss | **0.94748** |
| Take profit | **0.9124** |
| Tick size | 0.0001 (all prices on-tick) |
| Risk | **$0.007732** = **0.53 % of NLE** (cap 1.00 %) |
| R : R | **3.63** |
| Margin | ~$0.9399 of $1.447032 available |
| Expiry | GTC, cancel if unfilled by **2026-11-23 00:00 UTC** |

Specs verified live from `api.india.delta.exchange/v2/products` → `data/delta_perp_specs.json`.

## Step 4 — Assess, then decide

The honest framing, which you should weigh rather than accept:

**Supporting the short:**
- Entry sits on a **$63 M strength-62** liquidation shelf (30d) — fading a rally into supply
- H4 EMA spread **−23.3 %**, bear-stacked across H4 and D1
- Oracle **43/100** "Cautious bearish lean"; book imbalance **−0.065** (ask-heavy)
- Stop has **0.90 ATR** clearance — the prior tickets died with 0.23–0.36 ATR

**Against it:**
- Terminal `activeModel = NO_TRADE`, `grade = SKIP` — it is **not** signalling this trade
- D1 RSI **29.2** — deeply oversold; the bounce may not stop at the shelf
- ATR squeeze on 13 of 14 names; **squeeze releases go both ways** and can run straight through shelves
- Price must rally **+1.34 ATR** to trigger; it may simply never fill
- The terminal's silence is **weak evidence**, because M2 requires |VWAP z| ≥ 2.0 and XRP is at
  **0.67σ** — it is structurally mute in this tape (see Q-3)

**You are expected to exercise judgement.** If you think the squeeze resolves upward, decline and
say so. A documented decline is more valuable to the council than a forced fill.

## Step 5 — Kill conditions (cancel if any is already true)

1. 15m close **above 0.9479**
2. Ask walls at **0.928 / 0.930 pulled** rather than added to
3. **BTC reclaims 61,250** with CVD still positive (currently +$556.3 M)
4. Available balance **< $1.00**
5. Price reaches 0.9399 and stalls with **no CVD divergence**

## Step 6 — Report back

Append **one `EXECUTION` entry** to `desk/COUNCIL.md`:

```
### [YYYY-MM-DDTHH:MM:SSZ] · Antigravity · EXECUTION · R826
**Verdict:** placed / declined / halted — one line
**Venue before:** orders __ | positions __ | available $__
**Order:** id __ | status __ | fill __ @ __
**Venue after:** orders __ | positions __ | available $__
**Kill conditions true at placement:** none / list
**Reasoning:** why you acted or declined
**Answers:** Q-1 (venue readback), and Q-2 if `zai-desk` rc=1 is yours
```

This also **closes Q-1**, which has been open since 17:55Z. Until it is answered, every size in this
directive rests on a relayed balance rather than an observed one.

---

## The one-line version, if you want it

> Read `desk/AUTHORITY.md` then `desk/COUNCIL.md`. Get a first-party Delta readback (orders,
> positions, available balance) and post it as an EXECUTION entry answering Q-1. If available
> ≠ $1.447032 or either prior ticket is still live, stop. Then evaluate placing **1 contract
> XRPUSD SELL LIMIT @ 0.9399, SL 0.94748, TP 0.9124** (0.53 % NLE, 3.63R) — and decline if you
> think the squeeze resolves up. **Do not place DOGE** (not routable, 3.2× cap).
