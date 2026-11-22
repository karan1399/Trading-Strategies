# Liquidation Heatmap Trading Research System

> Self-contained module (`lhm/`) inside the karan1399/Trading-Strategies monorepo.
> All paths below are relative to this directory.

End-to-end research and execution stack built around **crypto liquidation heatmaps**:

1. **Reverse-engineered** Coinglass's Liquidation Heatmap (Model 1) protocol and algorithm.
2. **Independent reconstruction** of the heatmap from raw Binance data (target: full parity).
3. **Historical data pipeline**: Binance Vision klines / metrics / funding / aggTrades -> compact parquet.
4. **Time-evolving heatmap engine**: level creation, persistence, sweeps, cascade dynamics.
5. **Strategy layer** (in progress): rule-based + ML, minimum 2R risk-reward, heatmaps as price magnets.
6. **Trading terminal** (planned): candles + heatmap overlay, playback, zoom, algorithmic trade marking.

## Current status

| Component | Status |
|---|---|
| Coinglass protocol (signing + encryption) | **Done** — full client in `scripts/cg_client.py` |
| Model 1 algorithm spec | **Done** — confirmed mechanics, see `docs/MODEL1_ALGORITHM.md` |
| Binance data (2020-12 -> now) | **Done** — 5m/15m/1h/4h/1d klines, 5-min OI metrics, funding |
| Tick data (aggTrades) | **In progress** — newest-first daily parquet, resumable |
| Heatmap reconstruction engine | **Working** — structural parity 80% top-level match, cell-level r=0.21 |
| Event-stream calibration | **Open** — event-bar selection rule is the main remaining gap |
| ML / strategy layer | Not started |
| Terminal | Not started |

Parity is measured against a live-captured Coinglass 24h dump
(`research/coinglass/dumps/m1_24h_binance.json`, sandbox-local). Current numbers:
Pearson r = 0.212, cosine = 0.496, top-20 level match = 80%, magnitude ratio ~1.0 at
`flow_k = 0.03`. The mechanics (grid, leverage buckets, weights, removal-on-touch) are
confirmed identical; the gap is *which bars create events* and their long/short split.

## Repository layout

```
scripts/            all runnable code (python + one node deobfuscator)
  cg_sign.py            request `data` param generator (AES+TOTP)
  cg_client.py          signed+decrypted Coinglass API client
  cg_dump_all.py        dump Model 1/2/3 heatmaps for all ranges/symbols
  cg_analyze_m1.py      data-semantics analysis
  cg_increment_rule.py  increment (event) fingerprint extraction
  cg_trace_levels.py    level persistence / removal-cause tracing
  cg_diff_snaps.py      live two-snapshot diff (increment rule)
  lhm_engine.py         reconstruction engine (grid simulation)
  lhm_window.py         windowed simulator + parity validator
  build_events.py       event-stream generator (calibrated heuristic)
  calibrate_model.py    grid-search calibration vs Coinglass dump
  binance_downloader.py Binance Vision bulk downloader
  download_aggtrades.py tick-level aggTrades -> zstd parquet
  build_store.py        zips -> consolidated parquet store
  merge_metrics.py      metrics rebuild from all sources
  deobf_26152.js        webpack-module deobfuscator (Coinglass crypto)
  ...                   one-off RE debug tools
docs/               protocol + algorithm + data + architecture documentation
data/               (not committed) parquet store + raw downloads
research/           (not committed) reverse-engineering evidence + dumps
tools/              (not committed) cloned third-party tooling (rea)
```

## Quickstart

> **Note on paths:** scripts currently use the research sandbox layout
> (`/home/z/my-project/...`). When running from a local clone, replace that prefix
> with your workspace root (portability refactor is queued; tracked in
> docs/ARCHITECTURE.md conventions).

```bash
pip install pycryptodome pandas pyarrow numpy

# 1. dump a fresh Coinglass heatmap for validation
python scripts/cg_dump_all.py 1 24h

# 2. build the local data store from Binance Vision
python scripts/binance_downloader.py 2021-01 2026-11 2026-11-21
python scripts/build_store.py
python scripts/merge_metrics.py

# 3. generate the event stream + run the parity test
python scripts/build_events.py
python scripts/lhm_window.py
python scripts/calibrate_model.py

# 4. tick data (resumable, newest first)
python scripts/download_aggtrades.py
```

## Documentation

- [docs/REVERSE_ENGINEERING.md](docs/REVERSE_ENGINEERING.md) — Coinglass protocol: endpoints, request signing, response decryption.
- [docs/MODEL1_ALGORITHM.md](docs/MODEL1_ALGORITHM.md) — how the Model 1 heatmap is built, confirmed mechanics, open items.
- [docs/DATA.md](docs/DATA.md) — datasets, coverage, sizes, regeneration.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system design and roadmap.

## Security / credentials policy

- No personal credentials are committed. The Coinglass account email/password are used
  at runtime only (env vars / local files excluded by `.gitignore`: `.env`, `data/`,
  `research/`).
- The AES keys / TOTP secret in `scripts/cg_*.py` are Coinglass **frontend** constants
  extracted from their public JavaScript bundle — not user secrets.

## Disclaimer

Research and education only. Not financial advice. Trading leveraged products involves
substantial risk of loss.
