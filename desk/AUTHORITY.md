# AUTHORITY — which document wins

**Every agent and daemon reads this first.** Where two documents disagree, the higher row wins.
If a topic is not listed, the canonical spec wins. If still ambiguous, ask the operator — do not guess.

Ratified: 2026-11-22 · Commit `f166781d` lineage · Supersedes any precedence note in older docs.

---

## 1. The tricouncil

| Agent | Runtime | Mandate | **Execution rights** |
| :--- | :--- | :--- | :--- |
| **Antigravity** | operator host | formulates **and executes** | ✅ **SOLE trade authority** — Delta Exchange + MT5 |
| **Arena.ai** | this sandbox | analysis, repo, tooling, review | ❌ never punches |
| **GLM** | operator host | independent analysis, challenge | ❌ never punches |

**No other party may place, amend, or cancel an order on any venue.** Arena and GLM propose;
Antigravity disposes. A proposal from a non-executor is *evidence*, never a directive.

Any Arena or GLM output that reads like "cancel ticket X" or "enter at Y" is a
**recommendation to Antigravity**. Antigravity is free to decline it and must record why.

## 2. Document precedence

| # | Topic | Authoritative document |
| --: | :--- | :--- |
| 1 | **Live venue state** (orders, fills, balances) | the venue API itself; then `desk/state.json` |
| 2 | **Risk limits, gate definitions, PIT rules** | `desk/CANONICAL_MASTER_SPECIFICATION.md` |
| 3 | **Interlock precondition before any order batch** | `desk/PRE_TRADE_INTERLOCK_RULE.md` (§11B ILK-01) |
| 4 | **Trade formulation method** | `desk/TRADE_FORMULATION_MANUAL.md` |
| 5 | **Round procedure / evidence bundle** | `desk/ROUND_EVIDENCE_CONTRACT.md` |
| 6 | **Cross-agent discussion & decisions** | **`desk/COUNCIL.md`** ← the shared append-only log |
| 7 | **Chronological round history** | `desk/log.md` (append-only; rotated monthly) |
| 8 | **Data coverage & route availability** | `desk/DATA_COVERAGE_AUDIT.md` |
| 9 | **Reverse-engineered API knowledge** | `docs/COINGLASS_API_NOTES.md` |
| 10 | **Installed skills & their mapping to our gates** | `skills/INSTALLED.md` |

### Archived — do not cite as live

- `desk/archive/spec_history/CANONICAL_MASTER_SPECIFICATION_V1.md`
- `desk/archive/spec_history/CANONICAL_MASTER_SPECIFICATION.v2.0.0-backup.md`
- `desk/archive/redundant_manuals/` (superseded copies of the formulation manual)
- `desk/archive/legacy_telemetry/`, `desk/archive/legacy_scripts/`

If an agent finds itself reading a file under `desk/archive/`, it is looking at **history**, not policy.

## 3. Standing hard rules — no agent may override

1. **1.0 % equity cap** per Grade-A trade; 0.5 % for Grade-B. Position size must be computed against
   *total* risk vs Net Liquid Equity, never per-contract alone.
2. **Interlock first.** No order batch without a written interlock assertion; failure = HALT +
   `desk/events/interlock_YYYYMMDD_HHMMSS.json`.
3. **Stop must clear the widest liquidation shelf** across 3d / 7d / 30d ranges. A stop with under
   ~1 ATR of clearance over a material shelf is a PIT-21 stop-death and must not be submitted.
4. **Every claim is labelled** `VERIFIED` (agent observed it) or `UNVERIFIED-BY-AGENT`
   (operator/daemon asserted it). Never present the second as the first.
5. **A verified NO-TRADE is a success**, not a failure. Never force a trade to have an output.
6. **Never commit secrets.** No PATs, no session cookies, no `desk/mt5.json`, no `.env`.
7. **Push only to `karan1399/Trading-Strategies`.** A pre-push hook enforces this.

## 4. If two agents disagree

1. Write both positions to `desk/COUNCIL.md` in the same round, each timestamped and signed.
2. State explicitly **what evidence would settle it** — and fetch that evidence.
3. If evidence cannot settle it, the **more conservative position wins by default**
   (smaller size, wider stop, no new entry).
4. Antigravity decides execution. Its decision and reasoning go in the same COUNCIL entry.
5. Never silently overwrite another agent's entry. COUNCIL.md is append-only; corrections are
   new entries that cite the one they correct.
