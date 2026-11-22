/**
 * CURATED UNIVERSE — 61 assets, the terminal's complete and ONLY universe.
 *
 * User directive 2026-11-12: "Pick top 30 crypto of all times and 30 tradifi
 * assets I shared and if anything is missing, then add that too. Ensure those
 * assets have each and every data point available on all tabs. No gaps."
 *
 * Composition (terminal rank order = palette/grid order):
 *   - 30 crypto  : USDT-M PERPETUAL on Binance, "of all times" = proven
 *                  large-cap / high-liquidity veterans (VET takes TON's slot:
 *                  TONUSDT perp entered SETTLING on 2026-11-12 and cannot be
 *                  traded or quoted).
 *   - 28 TradFi  : USDT-M TRADIFI_PERPETUAL on Binance under the PLAIN name
 *                  (NVDAUSDT, SOXLUSDT, XAUUSDT, CLUSDT, ANTHROPICUSDT...).
 *                  24 of them also have a Binance SPOT "B-token" leg
 *                  (NVDABUSDT) and XAU has XAUTUSDT (tokenized gold).
 *                  Verified 2026-11-12: all 28 TRADING with full Coinglass
 *                  surface (kline / orderFlow / liqlevels / bs_usd / lto).
 *   - 3 classic  : indices/oil with NO Binance perp — S&P 500 (SPX500),
 *                  Brent (UKO), Nasdaq-100 (NDX100). Yahoo classic feed +
 *                  tradeXYZ/Bitget perp venues via perpmap/micro; tabs beyond
 *                  Overview show an honest explainer, never fake data.
 *
 * METHODOLOGY (single- vs multi-exchange — final decision):
 *   Overview + Footprint = SINGLE EXCHANGE (Binance, the execution venue —
 *     demo orders are punched on Binance futures testnet, so the auction,
 *     EMA/RSI/VWAP and per-price order-flow must be the venue's own truth).
 *   Large Orders / Liq Levels / Book Walls / CVD = MULTI EXCHANGE
 *     (aggregates with per-venue breakdown; Binance is always the lead).
 *   Alerts = all venues, scanner iterates every Binance-feed asset.
 *
 * SYMBOL RESOLUTION RULES (hard):
 *   - Terminal syms for perps are the Binance perp BASE (NVDA, not NVDAB;
 *     SKHY, not SKHYNIX; 1000PEPE, not PEPE). Aliases exist for search only.
 *   - Never resolve curated TradFi syms via Coinglass keyword discovery:
 *     keyword=MUB returns the MUBARAK memecoin (verified). Instrument keys
 *     are always constructed explicitly (Binance_MUBUSDT etc.) or pinned.
 *
 * `punchable` = symbol is TRADING on testnet.binancefuture.com (demo order
 * execution). Verified 2026-11-12 — re-verify after testnet resets.
 */

export type CuratedKind = "crypto-perp" | "tradfi-perp" | "classic";

export interface CuratedAsset {
  /** Terminal symbol — perp base (BTC, NVDA, 1000PEPE) or classic key (SPX500). */
  sym: string;
  /** Binance USD-M futures pair (SYM + "USDT"). Undefined for classic. */
  pair?: string;
  /** Binance SPOT pair — B-token leg (NVDABUSDT) or XAUTUSDT for gold. */
  spotPair?: string;
  kind: CuratedKind;
  category: "Crypto" | "TradFi";
  name: string;
  /** Sub-category label shown in the palette. */
  sub: string;
  /** Alternate names from the user's table / common usage (search only). */
  aliases?: string[];
  /** Demo-punchable on Binance futures TESTNET. */
  punchable: boolean;
  /** "user" = in the user's shared list; "fill" = added to complete 30+30. */
  origin: "user" | "fill";
}

export const CURATED: readonly CuratedAsset[] = [
  // ─── CRYPTO 30 (Binance USDT-M PERPETUAL) ─────────────────────────────
  { sym: "BTC", pair: "BTCUSDT", kind: "crypto-perp", category: "Crypto", name: "Bitcoin", sub: "Store of Value", aliases: ["XBT"], punchable: true, origin: "user" },
  { sym: "ETH", pair: "ETHUSDT", kind: "crypto-perp", category: "Crypto", name: "Ethereum", sub: "Smart Contract L1", punchable: true, origin: "user" },
  { sym: "BNB", pair: "BNBUSDT", kind: "crypto-perp", category: "Crypto", name: "BNB", sub: "Exchange Token", punchable: true, origin: "user" },
  { sym: "SOL", pair: "SOLUSDT", kind: "crypto-perp", category: "Crypto", name: "Solana", sub: "Smart Contract L1", punchable: true, origin: "user" },
  { sym: "XRP", pair: "XRPUSDT", kind: "crypto-perp", category: "Crypto", name: "XRP", sub: "Payments", punchable: true, origin: "user" },
  { sym: "ADA", pair: "ADAUSDT", kind: "crypto-perp", category: "Crypto", name: "Cardano", sub: "Smart Contract L1", punchable: true, origin: "user" },
  { sym: "DOGE", pair: "DOGEUSDT", kind: "crypto-perp", category: "Crypto", name: "Dogecoin", sub: "Meme", punchable: true, origin: "user" },
  { sym: "TRX", pair: "TRXUSDT", kind: "crypto-perp", category: "Crypto", name: "TRON", sub: "Smart Contract L1", punchable: true, origin: "user" },
  { sym: "LINK", pair: "LINKUSDT", kind: "crypto-perp", category: "Crypto", name: "Chainlink", sub: "Oracle", punchable: true, origin: "user" },
  { sym: "LTC", pair: "LTCUSDT", kind: "crypto-perp", category: "Crypto", name: "Litecoin", sub: "Payments", punchable: true, origin: "user" },
  { sym: "AVAX", pair: "AVAXUSDT", kind: "crypto-perp", category: "Crypto", name: "Avalanche", sub: "Smart Contract L1", punchable: true, origin: "user" },
  { sym: "DOT", pair: "DOTUSDT", kind: "crypto-perp", category: "Crypto", name: "Polkadot", sub: "Interoperability", punchable: true, origin: "user" },
  { sym: "NEAR", pair: "NEARUSDT", kind: "crypto-perp", category: "Crypto", name: "NEAR Protocol", sub: "Smart Contract L1", punchable: true, origin: "user" },
  { sym: "UNI", pair: "UNIUSDT", kind: "crypto-perp", category: "Crypto", name: "Uniswap", sub: "DeFi DEX", punchable: true, origin: "user" },
  { sym: "ATOM", pair: "ATOMUSDT", kind: "crypto-perp", category: "Crypto", name: "Cosmos", sub: "Interoperability", punchable: true, origin: "user" },
  { sym: "XLM", pair: "XLMUSDT", kind: "crypto-perp", category: "Crypto", name: "Stellar", sub: "Payments", punchable: true, origin: "user" },
  { sym: "ETC", pair: "ETCUSDT", kind: "crypto-perp", category: "Crypto", name: "Ethereum Classic", sub: "Smart Contract L1", punchable: true, origin: "user" },
  { sym: "FIL", pair: "FILUSDT", kind: "crypto-perp", category: "Crypto", name: "Filecoin", sub: "Storage", punchable: true, origin: "user" },
  { sym: "ICP", pair: "ICPUSDT", kind: "crypto-perp", category: "Crypto", name: "Internet Computer", sub: "Smart Contract L1", punchable: false, origin: "user" },
  { sym: "HBAR", pair: "HBARUSDT", kind: "crypto-perp", category: "Crypto", name: "Hedera", sub: "Enterprise L1", punchable: true, origin: "user" },
  { sym: "APT", pair: "APTUSDT", kind: "crypto-perp", category: "Crypto", name: "Aptos", sub: "Smart Contract L1", punchable: true, origin: "user" },
  { sym: "ARB", pair: "ARBUSDT", kind: "crypto-perp", category: "Crypto", name: "Arbitrum", sub: "Ethereum L2", punchable: true, origin: "user" },
  { sym: "OP", pair: "OPUSDT", kind: "crypto-perp", category: "Crypto", name: "Optimism", sub: "Ethereum L2", punchable: true, origin: "user" },
  { sym: "SUI", pair: "SUIUSDT", kind: "crypto-perp", category: "Crypto", name: "Sui", sub: "Smart Contract L1", punchable: true, origin: "user" },
  { sym: "SEI", pair: "SEIUSDT", kind: "crypto-perp", category: "Crypto", name: "Sei", sub: "Smart Contract L1", punchable: true, origin: "user" },
  { sym: "INJ", pair: "INJUSDT", kind: "crypto-perp", category: "Crypto", name: "Injective", sub: "DeFi L1", punchable: true, origin: "user" },
  { sym: "TIA", pair: "TIAUSDT", kind: "crypto-perp", category: "Crypto", name: "Celestia", sub: "Modular DA", punchable: true, origin: "user" },
  { sym: "VET", pair: "VETUSDT", kind: "crypto-perp", category: "Crypto", name: "VeChain", sub: "Supply Chain", punchable: true, origin: "fill" },
  { sym: "1000PEPE", pair: "1000PEPEUSDT", kind: "crypto-perp", category: "Crypto", name: "Pepe (1000x perp)", sub: "Meme", aliases: ["PEPE"], punchable: true, origin: "user" },
  { sym: "1000SHIB", pair: "1000SHIBUSDT", kind: "crypto-perp", category: "Crypto", name: "Shiba Inu (1000x perp)", sub: "Meme", aliases: ["SHIB"], punchable: true, origin: "user" },

  // ─── TRADFI 28 (Binance USDT-M TRADIFI_PERPETUAL, plain names) ────────
  { sym: "XAU", pair: "XAUUSDT", spotPair: "XAUTUSDT", kind: "tradfi-perp", category: "TradFi", name: "Gold", sub: "Metal", aliases: ["GOLD", "XAUT", "PAXG"], punchable: true, origin: "user" },
  { sym: "NVDA", pair: "NVDAUSDT", spotPair: "NVDABUSDT", kind: "tradfi-perp", category: "TradFi", name: "NVIDIA", sub: "Semiconductors", punchable: true, origin: "user" },
  { sym: "SNDK", pair: "SNDKUSDT", spotPair: "SNDKBUSDT", kind: "tradfi-perp", category: "TradFi", name: "SanDisk", sub: "Semiconductors", punchable: false, origin: "user" },
  { sym: "SOXL", pair: "SOXLUSDT", spotPair: "SOXLBUSDT", kind: "tradfi-perp", category: "TradFi", name: "Direxion Semis 3x", sub: "Leveraged ETF", punchable: true, origin: "user" },
  { sym: "SPCX", pair: "SPCXUSDT", spotPair: "SPCXBUSDT", kind: "tradfi-perp", category: "TradFi", name: "SpaceX (pre-IPO)", sub: "Pre-IPO", punchable: true, origin: "user" },
  { sym: "GOOGL", pair: "GOOGLUSDT", spotPair: "GOOGLBUSDT", kind: "tradfi-perp", category: "TradFi", name: "Alphabet", sub: "Internet", aliases: ["GOOG"], punchable: true, origin: "user" },
  { sym: "MU", pair: "MUUSDT", spotPair: "MUBUSDT", kind: "tradfi-perp", category: "TradFi", name: "Micron", sub: "Semiconductors", punchable: false, origin: "user" },
  { sym: "SKHY", pair: "SKHYUSDT", spotPair: "SKHYBUSDT", kind: "tradfi-perp", category: "TradFi", name: "SK Hynix", sub: "Semiconductors", aliases: ["SKHYNIX", "SKHX"], punchable: false, origin: "user" },
  { sym: "XAG", pair: "XAGUSDT", kind: "tradfi-perp", category: "TradFi", name: "Silver", sub: "Metal", aliases: ["SILVER"], punchable: true, origin: "user" },
  { sym: "CL", pair: "CLUSDT", kind: "tradfi-perp", category: "TradFi", name: "Crude Oil WTI", sub: "Energy", aliases: ["WTI", "USO"], punchable: false, origin: "user" },
  { sym: "INTC", pair: "INTCUSDT", spotPair: "INTCBUSDT", kind: "tradfi-perp", category: "TradFi", name: "Intel", sub: "Semiconductors", punchable: true, origin: "user" },
  { sym: "CRCL", pair: "CRCLUSDT", spotPair: "CRCLBUSDT", kind: "tradfi-perp", category: "TradFi", name: "Circle", sub: "FinTech", punchable: true, origin: "user" },
  { sym: "MSTR", pair: "MSTRUSDT", spotPair: "MSTRBUSDT", kind: "tradfi-perp", category: "TradFi", name: "Strategy (MicroStrategy)", sub: "Bitcoin Proxy", punchable: true, origin: "user" },
  { sym: "QQQ", pair: "QQQUSDT", spotPair: "QQQBUSDT", kind: "tradfi-perp", category: "TradFi", name: "Invesco QQQ Trust", sub: "Index ETF", punchable: true, origin: "user" },
  { sym: "ANTHROPIC", pair: "ANTHROPICUSDT", kind: "tradfi-perp", category: "TradFi", name: "Anthropic (pre-IPO)", sub: "Pre-IPO", punchable: false, origin: "user" },
  { sym: "AAPL", pair: "AAPLUSDT", spotPair: "AAPLBUSDT", kind: "tradfi-perp", category: "TradFi", name: "Apple", sub: "Hardware", punchable: false, origin: "user" },
  { sym: "TSLA", pair: "TSLAUSDT", spotPair: "TSLABUSDT", kind: "tradfi-perp", category: "TradFi", name: "Tesla", sub: "EV / Auto", punchable: true, origin: "user" },
  { sym: "DRAM", pair: "DRAMUSDT", spotPair: "DRAMBUSDT", kind: "tradfi-perp", category: "TradFi", name: "DRAM Index", sub: "Semiconductors", punchable: false, origin: "user" },
  { sym: "KORU", pair: "KORUUSDT", spotPair: "KORUBUSDT", kind: "tradfi-perp", category: "TradFi", name: "Koru", sub: "Specialty", punchable: true, origin: "user" },
  { sym: "META", pair: "METAUSDT", spotPair: "METABUSDT", kind: "tradfi-perp", category: "TradFi", name: "Meta Platforms", sub: "Internet", punchable: true, origin: "user" },
  { sym: "NBIS", pair: "NBISUSDT", spotPair: "NBISBUSDT", kind: "tradfi-perp", category: "TradFi", name: "Nebius", sub: "AI Infra", punchable: false, origin: "user" },
  { sym: "MSFT", pair: "MSFTUSDT", spotPair: "MSFTBUSDT", kind: "tradfi-perp", category: "TradFi", name: "Microsoft", sub: "Software", punchable: false, origin: "fill" },
  { sym: "AMZN", pair: "AMZNUSDT", spotPair: "AMZNBUSDT", kind: "tradfi-perp", category: "TradFi", name: "Amazon", sub: "E-Commerce", punchable: true, origin: "fill" },
  { sym: "AMD", pair: "AMDUSDT", spotPair: "AMDBUSDT", kind: "tradfi-perp", category: "TradFi", name: "AMD", sub: "Semiconductors", punchable: false, origin: "fill" },
  { sym: "COIN", pair: "COINUSDT", spotPair: "COINBUSDT", kind: "tradfi-perp", category: "TradFi", name: "Coinbase", sub: "Crypto Exchange", punchable: true, origin: "fill" },
  { sym: "NFLX", pair: "NFLXUSDT", spotPair: "NFLXBUSDT", kind: "tradfi-perp", category: "TradFi", name: "Netflix", sub: "Media", punchable: false, origin: "fill" },
  { sym: "AVGO", pair: "AVGOUSDT", spotPair: "AVGOBUSDT", kind: "tradfi-perp", category: "TradFi", name: "Broadcom", sub: "Semiconductors", punchable: false, origin: "fill" },
  { sym: "TSM", pair: "TSMUSDT", spotPair: "TSMBUSDT", kind: "tradfi-perp", category: "TradFi", name: "TSMC", sub: "Semiconductors", punchable: false, origin: "fill" },


  // ─── MUTUAL DELTA & BINANCE EXPANSION (173 assets) ──────────────────────
  { sym: "INIT", pair: "INITUSDT", kind: "crypto-perp", category: "Crypto", name: "INIT Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "JASMY", pair: "JASMYUSDT", kind: "crypto-perp", category: "Crypto", name: "JasmyCoin", sub: "IoT Data Democracy", punchable: true, origin: "fill" },
  { sym: "XPL", pair: "XPLUSDT", kind: "crypto-perp", category: "Crypto", name: "XPL Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "LDO", pair: "LDOUSDT", kind: "crypto-perp", category: "Crypto", name: "Lido DAO", sub: "Liquid Staking", punchable: true, origin: "fill" },
  { sym: "AIXBT", pair: "AIXBTUSDT", kind: "crypto-perp", category: "Crypto", name: "aixbt by Virtuals", sub: "AI Agent Alpha", punchable: true, origin: "fill" },
  { sym: "PROVE", pair: "PROVEUSDT", kind: "crypto-perp", category: "Crypto", name: "PROVE Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "RED", pair: "REDUSDT", kind: "crypto-perp", category: "Crypto", name: "RED Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "TNSR", pair: "TNSRUSDT", kind: "crypto-perp", category: "Crypto", name: "TNSR Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "COOKIE", pair: "COOKIEUSDT", kind: "crypto-perp", category: "Crypto", name: "COOKIE Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "AIOT", pair: "AIOTUSDT", kind: "crypto-perp", category: "Crypto", name: "AIoT Network", sub: "AI + IoT", punchable: true, origin: "fill" },
  { sym: "GRIFFAIN", pair: "GRIFFAINUSDT", kind: "crypto-perp", category: "Crypto", name: "GRIFFAIN Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "LIGHT", pair: "LIGHTUSDT", kind: "crypto-perp", category: "Crypto", name: "LIGHT Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "MANTA", pair: "MANTAUSDT", kind: "crypto-perp", category: "Crypto", name: "Manta Network", sub: "Modular L2", punchable: true, origin: "fill" },
  { sym: "ENJ", pair: "ENJUSDT", kind: "crypto-perp", category: "Crypto", name: "ENJ Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "USUAL", pair: "USUALUSDT", kind: "crypto-perp", category: "Crypto", name: "USUAL Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "SOON", pair: "SOONUSDT", kind: "crypto-perp", category: "Crypto", name: "SOON Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "ZEC", pair: "ZECUSDT", kind: "crypto-perp", category: "Crypto", name: "Zcash", sub: "Privacy L1", punchable: true, origin: "fill" },
  { sym: "AUCTION", pair: "AUCTIONUSDT", kind: "crypto-perp", category: "Crypto", name: "AUCTION Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "ONDO", pair: "ONDOUSDT", kind: "crypto-perp", category: "Crypto", name: "Ondo Finance", sub: "RWA / Treasuries", punchable: true, origin: "fill" },
  { sym: "EDEN", pair: "EDENUSDT", kind: "crypto-perp", category: "Crypto", name: "EDEN Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "BERA", pair: "BERAUSDT", kind: "crypto-perp", category: "Crypto", name: "Berachain", sub: "PoL L1", punchable: true, origin: "fill" },
  { sym: "DYDX", pair: "DYDXUSDT", kind: "crypto-perp", category: "Crypto", name: "dYdX", sub: "DeFi AppChain", punchable: true, origin: "fill" },
  { sym: "GRAM", pair: "GRAMUSDT", kind: "crypto-perp", category: "Crypto", name: "GRAM Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "RUNE", pair: "RUNEUSDT", kind: "crypto-perp", category: "Crypto", name: "THORChain", sub: "Cross-Chain DEX", punchable: true, origin: "fill" },
  { sym: "1MBABYDOGE", pair: "1MBABYDOGEUSDT", kind: "crypto-perp", category: "Crypto", name: "1MBABYDOGE Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "GALA", pair: "GALAUSDT", kind: "crypto-perp", category: "Crypto", name: "Gala Games", sub: "Gaming & Entertainment", punchable: true, origin: "fill" },
  { sym: "ZRO", pair: "ZROUSDT", kind: "crypto-perp", category: "Crypto", name: "ZRO Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "ARIA", pair: "ARIAUSDT", kind: "crypto-perp", category: "Crypto", name: "ARIA Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "BTW", pair: "BTWUSDT", kind: "crypto-perp", category: "Crypto", name: "BTW Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "XAUT", pair: "XAUTUSDT", kind: "crypto-perp", category: "Crypto", name: "Tether Gold Perp", sub: "Tokenized Gold", punchable: true, origin: "fill" },
  { sym: "ZORA", pair: "ZORAUSDT", kind: "crypto-perp", category: "Crypto", name: "ZORA Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "ALLO", pair: "ALLOUSDT", kind: "crypto-perp", category: "Crypto", name: "ALLO Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "VELVET", pair: "VELVETUSDT", kind: "crypto-perp", category: "Crypto", name: "VELVET Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "IN", pair: "INUSDT", kind: "crypto-perp", category: "Crypto", name: "IN Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "BCH", pair: "BCHUSDT", kind: "crypto-perp", category: "Crypto", name: "Bitcoin Cash", sub: "Payments L1", punchable: true, origin: "fill" },
  { sym: "NEIRO", pair: "NEIROUSDT", kind: "crypto-perp", category: "Crypto", name: "NEIRO Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "SWARMS", pair: "SWARMSUSDT", kind: "crypto-perp", category: "Crypto", name: "SWARMS Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "BLUR", pair: "BLURUSDT", kind: "crypto-perp", category: "Crypto", name: "BLUR Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "AAVE", pair: "AAVEUSDT", kind: "crypto-perp", category: "Crypto", name: "Aave", sub: "DeFi Lending", punchable: true, origin: "fill" },
  { sym: "1000BONK", pair: "1000BONKUSDT", kind: "crypto-perp", category: "Crypto", name: "Bonk (1000x perp)", sub: "Solana Meme", punchable: true, origin: "fill" },
  { sym: "SKYAI", pair: "SKYAIUSDT", kind: "crypto-perp", category: "Crypto", name: "SKYAI Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "DASH", pair: "DASHUSDT", kind: "crypto-perp", category: "Crypto", name: "DASH Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "WLD", pair: "WLDUSDT", kind: "crypto-perp", category: "Crypto", name: "Worldcoin", sub: "Proof of Personhood", punchable: true, origin: "fill" },
  { sym: "PENGU", pair: "PENGUUSDT", kind: "crypto-perp", category: "Crypto", name: "Pudgy Penguins", sub: "NFT Ecosystem", punchable: true, origin: "fill" },
  { sym: "SAHARA", pair: "SAHARAUSDT", kind: "crypto-perp", category: "Crypto", name: "SAHARA Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "SOLV", pair: "SOLVUSDT", kind: "crypto-perp", category: "Crypto", name: "SOLV Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "TAC", pair: "TACUSDT", kind: "crypto-perp", category: "Crypto", name: "TAC Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "DUSK", pair: "DUSKUSDT", kind: "crypto-perp", category: "Crypto", name: "DUSK Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "TRUMP", pair: "TRUMPUSDT", kind: "crypto-perp", category: "Crypto", name: "Official Trump", sub: "Political Meme", punchable: true, origin: "fill" },
  { sym: "POL", pair: "POLUSDT", kind: "crypto-perp", category: "Crypto", name: "Polygon Ecosystem", sub: "Ethereum L2", punchable: true, origin: "fill" },
  { sym: "IOTA", pair: "IOTAUSDT", kind: "crypto-perp", category: "Crypto", name: "IOTA", sub: "IoT DLT", punchable: true, origin: "fill" },
  { sym: "BASED", pair: "BASEDUSDT", kind: "crypto-perp", category: "Crypto", name: "BASED Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "ORDER", pair: "ORDERUSDT", kind: "crypto-perp", category: "Crypto", name: "ORDER Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "BR", pair: "BRUSDT", kind: "crypto-perp", category: "Crypto", name: "BR Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "XAN", pair: "XANUSDT", kind: "crypto-perp", category: "Crypto", name: "XAN Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "POPCAT", pair: "POPCATUSDT", kind: "crypto-perp", category: "Crypto", name: "Popcat", sub: "Solana Meme", punchable: true, origin: "fill" },
  { sym: "WCT", pair: "WCTUSDT", kind: "crypto-perp", category: "Crypto", name: "WCT Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "BLESS", pair: "BLESSUSDT", kind: "crypto-perp", category: "Crypto", name: "BLESS Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "CHIP", pair: "CHIPUSDT", kind: "crypto-perp", category: "Crypto", name: "CHIP Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "GIGGLE", pair: "GIGGLEUSDT", kind: "crypto-perp", category: "Crypto", name: "Giggle Academy", sub: "Charity Meme", punchable: true, origin: "fill" },
  { sym: "VIRTUAL", pair: "VIRTUALUSDT", kind: "crypto-perp", category: "Crypto", name: "Virtuals Protocol", sub: "AI Agents", punchable: true, origin: "fill" },
  { sym: "NMR", pair: "NMRUSDT", kind: "crypto-perp", category: "Crypto", name: "NMR Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "M", pair: "MUSDT", kind: "crypto-perp", category: "Crypto", name: "M Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "RSR", pair: "RSRUSDT", kind: "crypto-perp", category: "Crypto", name: "Reserve Rights", sub: "Asset-Backed Currency", punchable: true, origin: "fill" },
  { sym: "AXS", pair: "AXSUSDT", kind: "crypto-perp", category: "Crypto", name: "Axie Infinity", sub: "Gaming", punchable: true, origin: "fill" },
  { sym: "STX", pair: "STXUSDT", kind: "crypto-perp", category: "Crypto", name: "Stacks", sub: "Bitcoin L2", punchable: true, origin: "fill" },
  { sym: "ZEN", pair: "ZENUSDT", kind: "crypto-perp", category: "Crypto", name: "ZEN Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "DOOD", pair: "DOODUSDT", kind: "crypto-perp", category: "Crypto", name: "DOOD Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "QNT", pair: "QNTUSDT", kind: "crypto-perp", category: "Crypto", name: "QNT Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "MANA", pair: "MANAUSDT", kind: "crypto-perp", category: "Crypto", name: "Decentraland", sub: "Metaverse", punchable: true, origin: "fill" },
  { sym: "LISTA", pair: "LISTAUSDT", kind: "crypto-perp", category: "Crypto", name: "LISTA Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "AIO", pair: "AIOUSDT", kind: "crypto-perp", category: "Crypto", name: "AIO Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "SUSHI", pair: "SUSHIUSDT", kind: "crypto-perp", category: "Crypto", name: "SushiSwap", sub: "DeFi DEX", punchable: true, origin: "fill" },
  { sym: "WIF", pair: "WIFUSDT", kind: "crypto-perp", category: "Crypto", name: "dogwifhat", sub: "Solana Meme", punchable: true, origin: "fill" },
  { sym: "CROSS", pair: "CROSSUSDT", kind: "crypto-perp", category: "Crypto", name: "CROSS Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "LAYER", pair: "LAYERUSDT", kind: "crypto-perp", category: "Crypto", name: "LAYER Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "TLM", pair: "TLMUSDT", kind: "crypto-perp", category: "Crypto", name: "TLM Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "MMT", pair: "MMTUSDT", kind: "crypto-perp", category: "Crypto", name: "MMT Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "DEEP", pair: "DEEPUSDT", kind: "crypto-perp", category: "Crypto", name: "DEEP Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "S", pair: "SUSDT", kind: "crypto-perp", category: "Crypto", name: "Sonic Network", sub: "Smart Contract L1", punchable: true, origin: "fill" },
  { sym: "BILL", pair: "BILLUSDT", kind: "crypto-perp", category: "Crypto", name: "BILL Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "PUMP", pair: "PUMPUSDT", kind: "crypto-perp", category: "Crypto", name: "PUMP Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "VVV", pair: "VVVUSDT", kind: "crypto-perp", category: "Crypto", name: "VVV Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "ZK", pair: "ZKUSDT", kind: "crypto-perp", category: "Crypto", name: "zkSync", sub: "ZK-Rollup L2", punchable: true, origin: "fill" },
  { sym: "DOGS", pair: "DOGSUSDT", kind: "crypto-perp", category: "Crypto", name: "Dogs Community", sub: "TON Meme", punchable: true, origin: "fill" },
  { sym: "OPN", pair: "OPNUSDT", kind: "crypto-perp", category: "Crypto", name: "OPN Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "HYPE", pair: "HYPEUSDT", kind: "crypto-perp", category: "Crypto", name: "HYPE Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "ME", pair: "MEUSDT", kind: "crypto-perp", category: "Crypto", name: "ME Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "H", pair: "HUSDT", kind: "crypto-perp", category: "Crypto", name: "H Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "MOVE", pair: "MOVEUSDT", kind: "crypto-perp", category: "Crypto", name: "Movement", sub: "Move-EVM L2", punchable: true, origin: "fill" },
  { sym: "EVAA", pair: "EVAAUSDT", kind: "crypto-perp", category: "Crypto", name: "EVAA Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "AIGENSYN", pair: "AIGENSYNUSDT", kind: "crypto-perp", category: "Crypto", name: "AIGENSYN Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "PARTI", pair: "PARTIUSDT", kind: "crypto-perp", category: "Crypto", name: "PARTI Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "SPX", pair: "SPXUSDT", kind: "crypto-perp", category: "Crypto", name: "SPX Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "ENA", pair: "ENAUSDT", kind: "crypto-perp", category: "Crypto", name: "Ethena", sub: "Synthetic Dollar", punchable: true, origin: "fill" },
  { sym: "JUP", pair: "JUPUSDT", kind: "crypto-perp", category: "Crypto", name: "Jupiter", sub: "Solana DEX Aggregator", punchable: true, origin: "fill" },
  { sym: "KSM", pair: "KSMUSDT", kind: "crypto-perp", category: "Crypto", name: "Kusama", sub: "Polkadot Canary", punchable: true, origin: "fill" },
  { sym: "AVAAI", pair: "AVAAIUSDT", kind: "crypto-perp", category: "Crypto", name: "AVAAI Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "AKE", pair: "AKEUSDT", kind: "crypto-perp", category: "Crypto", name: "AKE Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "MUBARAK", pair: "MUBARAKUSDT", kind: "crypto-perp", category: "Crypto", name: "MUBARAK Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "HIVE", pair: "HIVEUSDT", kind: "crypto-perp", category: "Crypto", name: "HIVE Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "TST", pair: "TSTUSDT", kind: "crypto-perp", category: "Crypto", name: "TST Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "CAP", pair: "CAPUSDT", kind: "crypto-perp", category: "Crypto", name: "CAP Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "MASK", pair: "MASKUSDT", kind: "crypto-perp", category: "Crypto", name: "MASK Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "ESPORTS", pair: "ESPORTSUSDT", kind: "crypto-perp", category: "Crypto", name: "ESPORTS Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "RAVE", pair: "RAVEUSDT", kind: "crypto-perp", category: "Crypto", name: "RAVE Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "IO", pair: "IOUSDT", kind: "crypto-perp", category: "Crypto", name: "io.net", sub: "Decentralized GPU Cloud", punchable: true, origin: "fill" },
  { sym: "COAI", pair: "COAIUSDT", kind: "crypto-perp", category: "Crypto", name: "COAI Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "MEME", pair: "MEMEUSDT", kind: "crypto-perp", category: "Crypto", name: "Memecoin (Memeland)", sub: "Meme Ecosystem", punchable: true, origin: "fill" },
  { sym: "IOST", pair: "IOSTUSDT", kind: "crypto-perp", category: "Crypto", name: "IOST Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "NOT", pair: "NOTUSDT", kind: "crypto-perp", category: "Crypto", name: "Notcoin", sub: "TON Gaming", punchable: true, origin: "fill" },
  { sym: "PEOPLE", pair: "PEOPLEUSDT", kind: "crypto-perp", category: "Crypto", name: "ConstitutionDAO", sub: "DAO / Meme", punchable: true, origin: "fill" },
  { sym: "PAXG", pair: "PAXGUSDT", kind: "crypto-perp", category: "Crypto", name: "Paxos Gold Perp", sub: "Tokenized Gold", punchable: true, origin: "fill" },
  { sym: "SAGA", pair: "SAGAUSDT", kind: "crypto-perp", category: "Crypto", name: "SAGA Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "BIO", pair: "BIOUSDT", kind: "crypto-perp", category: "Crypto", name: "BIO Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "PNUT", pair: "PNUTUSDT", kind: "crypto-perp", category: "Crypto", name: "Peanut the Squirrel", sub: "Meme", punchable: true, origin: "fill" },
  { sym: "PIEVERSE", pair: "PIEVERSEUSDT", kind: "crypto-perp", category: "Crypto", name: "PIEVERSE Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "GTC", pair: "GTCUSDT", kind: "crypto-perp", category: "Crypto", name: "GTC Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "BMT", pair: "BMTUSDT", kind: "crypto-perp", category: "Crypto", name: "BMT Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "MELANIA", pair: "MELANIAUSDT", kind: "crypto-perp", category: "Crypto", name: "Melania Meme", sub: "Political Meme", punchable: true, origin: "fill" },
  { sym: "BANK", pair: "BANKUSDT", kind: "crypto-perp", category: "Crypto", name: "BANK Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "RARE", pair: "RAREUSDT", kind: "crypto-perp", category: "Crypto", name: "RARE Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "PIPPIN", pair: "PIPPINUSDT", kind: "crypto-perp", category: "Crypto", name: "PIPPIN Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "VANA", pair: "VANAUSDT", kind: "crypto-perp", category: "Crypto", name: "VANA Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "ORDI", pair: "ORDIUSDT", kind: "crypto-perp", category: "Crypto", name: "Ordinals", sub: "BRC-20 Inscription", punchable: true, origin: "fill" },
  { sym: "MON", pair: "MONUSDT", kind: "crypto-perp", category: "Crypto", name: "MON Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "GLM", pair: "GLMUSDT", kind: "crypto-perp", category: "Crypto", name: "GLM Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "1000FLOKI", pair: "1000FLOKIUSDT", kind: "crypto-perp", category: "Crypto", name: "Floki (1000x perp)", sub: "Meme", punchable: true, origin: "fill" },
  { sym: "JCT", pair: "JCTUSDT", kind: "crypto-perp", category: "Crypto", name: "JCT Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "TAO", pair: "TAOUSDT", kind: "crypto-perp", category: "Crypto", name: "Bittensor", sub: "Decentralized AI", punchable: true, origin: "fill" },
  { sym: "BEAT", pair: "BEATUSDT", kind: "crypto-perp", category: "Crypto", name: "BEAT Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "ARC", pair: "ARCUSDT", kind: "crypto-perp", category: "Crypto", name: "ARC Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "AIN", pair: "AINUSDT", kind: "crypto-perp", category: "Crypto", name: "AIN Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "STBL", pair: "STBLUSDT", kind: "crypto-perp", category: "Crypto", name: "STBL Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "WLFI", pair: "WLFIUSDT", kind: "crypto-perp", category: "Crypto", name: "WLFI Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "API3", pair: "API3USDT", kind: "crypto-perp", category: "Crypto", name: "API3 Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "XAI", pair: "XAIUSDT", kind: "crypto-perp", category: "Crypto", name: "XAI Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "XMR", pair: "XMRUSDT", kind: "crypto-perp", category: "Crypto", name: "XMR Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "FRAX", pair: "FRAXUSDT", kind: "crypto-perp", category: "Crypto", name: "FRAX Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "GOAT", pair: "GOATUSDT", kind: "crypto-perp", category: "Crypto", name: "Goatseus Maximus", sub: "AI Meme", punchable: true, origin: "fill" },
  { sym: "SUN", pair: "SUNUSDT", kind: "crypto-perp", category: "Crypto", name: "SUN Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "FF", pair: "FFUSDT", kind: "crypto-perp", category: "Crypto", name: "FF Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "AR", pair: "ARUSDT", kind: "crypto-perp", category: "Crypto", name: "AR Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "JTO", pair: "JTOUSDT", kind: "crypto-perp", category: "Crypto", name: "JTO Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "MOODENG", pair: "MOODENGUSDT", kind: "crypto-perp", category: "Crypto", name: "Moo Deng", sub: "Meme", punchable: true, origin: "fill" },
  { sym: "ASTER", pair: "ASTERUSDT", kind: "crypto-perp", category: "Crypto", name: "ASTER Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "KAITO", pair: "KAITOUSDT", kind: "crypto-perp", category: "Crypto", name: "Kaito AI", sub: "AI Search & Attention", punchable: true, origin: "fill" },
  { sym: "MOVR", pair: "MOVRUSDT", kind: "crypto-perp", category: "Crypto", name: "Moonriver", sub: "Kusama EVM", punchable: true, origin: "fill" },
  { sym: "1000SATS", pair: "1000SATSUSDT", kind: "crypto-perp", category: "Crypto", name: "Sats (1000x Ordinals)", sub: "BRC-20 Inscription", punchable: true, origin: "fill" },
  { sym: "LIT", pair: "LITUSDT", kind: "crypto-perp", category: "Crypto", name: "LIT Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "STRK", pair: "STRKUSDT", kind: "crypto-perp", category: "Crypto", name: "Starknet", sub: "ZK-Rollup L2", punchable: true, origin: "fill" },
  { sym: "HANA", pair: "HANAUSDT", kind: "crypto-perp", category: "Crypto", name: "HANA Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "TOWNS", pair: "TOWNSUSDT", kind: "crypto-perp", category: "Crypto", name: "TOWNS Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "SKL", pair: "SKLUSDT", kind: "crypto-perp", category: "Crypto", name: "SKL Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "IRYS", pair: "IRYSUSDT", kind: "crypto-perp", category: "Crypto", name: "IRYS Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "SIGN", pair: "SIGNUSDT", kind: "crypto-perp", category: "Crypto", name: "SIGN Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "SOPH", pair: "SOPHUSDT", kind: "crypto-perp", category: "Crypto", name: "SOPH Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "KITE", pair: "KITEUSDT", kind: "crypto-perp", category: "Crypto", name: "KITE Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "EIGEN", pair: "EIGENUSDT", kind: "crypto-perp", category: "Crypto", name: "EigenLayer", sub: "Restaking Protocol", punchable: true, origin: "fill" },
  { sym: "FARTCOIN", pair: "FARTCOINUSDT", kind: "crypto-perp", category: "Crypto", name: "Fartcoin", sub: "AI Meme", punchable: true, origin: "fill" },
  { sym: "ARK", pair: "ARKUSDT", kind: "crypto-perp", category: "Crypto", name: "Ark", sub: "Interoperability", punchable: true, origin: "fill" },
  { sym: "PENDLE", pair: "PENDLEUSDT", kind: "crypto-perp", category: "Crypto", name: "Pendle", sub: "Yield Tokenization", punchable: true, origin: "fill" },
  { sym: "ACT", pair: "ACTUSDT", kind: "crypto-perp", category: "Crypto", name: "Act I : The AI Prophecy", sub: "AI Agent Collective", punchable: true, origin: "fill" },
  { sym: "ETHFI", pair: "ETHFIUSDT", kind: "crypto-perp", category: "Crypto", name: "Ether.fi", sub: "Liquid Restaking", punchable: true, origin: "fill" },
  { sym: "ALT", pair: "ALTUSDT", kind: "crypto-perp", category: "Crypto", name: "AltLayer", sub: "Restaked Rollups", punchable: true, origin: "fill" },
  { sym: "RLC", pair: "RLCUSDT", kind: "crypto-perp", category: "Crypto", name: "RLC Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "SONIC", pair: "SONICUSDT", kind: "crypto-perp", category: "Crypto", name: "Sonic (Fantom)", sub: "Smart Contract L1", punchable: true, origin: "fill" },
  { sym: "RIVER", pair: "RIVERUSDT", kind: "crypto-perp", category: "Crypto", name: "RIVER Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },
  { sym: "CAKE", pair: "CAKEUSDT", kind: "crypto-perp", category: "Crypto", name: "PancakeSwap", sub: "DeFi DEX", punchable: true, origin: "fill" },
  { sym: "SIREN", pair: "SIRENUSDT", kind: "crypto-perp", category: "Crypto", name: "SIREN Token", sub: "DeFi / Altcoin", punchable: true, origin: "fill" },

  // ─── CLASSIC 3 (no Binance perp — Yahoo/tradeXYZ venues) ──────────────
  { sym: "SPX500", kind: "classic", category: "TradFi", name: "S&P 500", sub: "Index", aliases: ["SP500", "SPY", "GSPC"], punchable: false, origin: "user" },
  { sym: "UKO", kind: "classic", category: "TradFi", name: "Brent Oil", sub: "Energy", aliases: ["BRENTOIL", "BZ", "BRENT"], punchable: false, origin: "user" },
  { sym: "NDX100", kind: "classic", category: "TradFi", name: "Nasdaq-100", sub: "Index", aliases: ["XYZ100", "NDX"], punchable: false, origin: "user" },
] as const;

export const CURATED_BY_SYM: ReadonlyMap<string, CuratedAsset> = new Map(
  CURATED.map((a) => [a.sym.toUpperCase(), a]),
);

/** Resolve a user-typed name (alias included) to a curated asset. */
export function resolveCurated(input: string): CuratedAsset | undefined {
  const q = input.trim().toUpperCase();
  const direct = CURATED_BY_SYM.get(q);
  if (direct) return direct;
  return CURATED.find(
    (a) =>
      a.aliases?.some((x) => x.toUpperCase() === q) ||
      a.pair?.toUpperCase() === q ||
      a.spotPair?.toUpperCase() === q ||
      a.name.toUpperCase() === q,
  );
}

/** A curated perp — like CuratedAsset but the Binance USD-M pair is guaranteed. */
export interface CuratedPerp extends CuratedAsset {
  pair: string;
}

/** Perp-kind assets (58): everything with a Binance USD-M pair. */
export function curatedPerps(): CuratedPerp[] {
  return CURATED.filter((a): a is CuratedPerp => a.kind !== "classic" && typeof a.pair === "string");
}

/** Classic-kind assets (3). */
export function curatedClassic(): CuratedAsset[] {
  return CURATED.filter((a) => a.kind === "classic");
}

export const CURATED_TOTAL = CURATED.length; // 231 — never hardcode this number in docs
