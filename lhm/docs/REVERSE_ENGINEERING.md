# Coinglass Protocol — Reverse Engineering Notes

Target: `https://www.coinglass.com/LiquidationHeatMap/BTC` (Model 1/2/3), API host
`fapi.coinglass.com`. All findings extracted from the public frontend webpack bundle
(main chunk module `26152` = crypto module; deobfuscated via `scripts/deobf_26152.js`,
see `research/decoded_calls.json` for the decoded literal table).

## 1. Request signing (`data` parameter)

Every API GET carries a `data` query parameter:

```
payload = f"{unix_seconds},{totp6('I65VU7K5ZQL7WB4E', step=30)}"
data    = base64( AES-256-ECB-PKCS7( payload, key="1f68efd73f8d4921acc0dead41dd39bc") )
```

- Key is the 32-char ASCII string used as raw AES-256 key bytes.
- TOTP is standard RFC 6238 (HMAC-SHA1, base32 secret, 30s step, 6 digits).
- Required headers: `encryption: true`, `cache-ts-v2: <ms>`, `language: en`,
  plus browser UA/Origin/Referer.

Implementation: `scripts/cg_sign.py` (`make_data_param()`).

## 2. Response decryption

Responses with a `v` response header are encrypted: `{"success":true,"data":"<b64>"}`.

Key derivation for the per-response layer:

```
v   -> seed string
      v=0   cache-ts-v2 request header
      v=2   headers.time
      v=1   URL-derived (function `ea` on the path)
      v=55  "170b070da9654622"
      v=66  "d6537d845a964081"
      v=77  "863f08689c97435b"
k16 = base64(seed)[:16]                      # AES-128 key
dyn = inflate( AES-128-ECB-Dec( b64(headers.user), k16 ) )   # per-response key
data= inflate( AES-128-ECB-Dec( b64(body.data),  dyn ) )     # JSON payload
```

- `inflate` = raw deflate / zlib / gzip (try all wbits).
- Plain responses (no `v` header) are returned as-is.
- Login flow: `POST capi.coinglass.com/coin-community/api/user/login`
  (form: `mailAddress`, `password`) -> `accessToken`; then
  `POST fapi.coinglass.com/coin-community/api/user/web/session` with header
  `obe: <accessToken>` to establish session cookies.

Implementation: `scripts/cg_client.py` (`decrypt_response`, `cg_get`, `cg_login`).

## 3. Heatmap endpoints

Base `https://fapi.coinglass.com`:

| Model | aggregate (coin) | per-exchange (pair) |
|---|---|---|
| 1 | `/api/index/aggregate/liqHeatMap` | `/api/index/v2/liqHeatMap` |
| 2 | `/api/index/v3/aggregate/liqHeatMap` | `/api/index/v5/liqHeatMap` |
| 3 | `/api/index/v4/aggregate/liqHeatMap` | `/api/index/v6/liqHeatMap` |

Common params: `merge=true`, `symbol=BTC` (coin mode) or `symbol=Binance_BTCUSDT`
(pair mode).

Model 1/2 per-range `interval`/`limit`:

| range | interval | limit |
|---|---|---|
| 12h | 5 | 144 |
| 24h | 5 | 288 |
| 48h | 15 | 192 |
| 3d | 15 | 288 |
| 7d | 30 | 336 |
| 14d | 30 | 672 |
| 30d | h2 | 372 |
| 90d | h6 | 360 |
| 180d | h12 | 360 |
| 1y | h24 | 360 |
| 2y | d1 | 720 |

Model 3 instead takes `range` (`12h`...`720d`) and `cp=false`.

## 4. Response payload schema (Model 1)

```json
{
  "updateTime": 1732290000000,
  "rangeLow": 56000, "rangeHigh": 64000,
  "y": ["56000", ...],
  "prices": [[ts, open, high, low, close, vol], ...],
  "liq": [[col, row, value], ...],
  "instrument": { "symbol": "BTC", "exchangeNames": [...], ... }
}
```

- Grid: ~300 rows spanning `[rangeLow, rangeHigh]`, uniform spacing.
- `liq[c, r]` = notional estimated to be liquidated if price reaches `y[r]`,
  as of column `c` (cumulative live levels; not new flow).
- Value units: USD (matches candle volume scale on the same request).

## 5. Method notes

- Webpack chunk map: `research/chunks.txt`; heatmap page chunks `chunks/chunk_*.js`.
- Module 26152 body: `research/mod26152_full.txt`; decoded literal map:
  `research/decoded_calls.json`.
- Two live snapshots diffed 5 minutes apart (`cg_diff_snaps.py wait`) gave the
  increment rule (see MODEL1_ALGORITHM.md).
- Live captures are saved under `research/coinglass/dumps/` (m1/m2/m3 x range x
  agg/binance).
