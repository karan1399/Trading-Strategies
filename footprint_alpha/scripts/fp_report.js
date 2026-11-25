/* Footprint Auction-Complete ML - Quant Research Report (.docx)
 * English report, Profile A (Times New Roman), R1 cover with IG-1 palette.
 * All numbers are taken verbatim from the pipeline runs (fp_events.json,
 * fp_rr_sweep.json, train log, fp_backtest.json). */
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  ImageRun, PageBreak, Header, Footer, PageNumber, NumberFormat,
  AlignmentType, HeadingLevel, WidthType, BorderStyle, ShadingType,
  TableLayoutType, SectionType, TableOfContents,
} = require("docx");
const fs = require("fs");

const OUT = "/home/z/my-project/download/footprint/Footprint_Auction_ML_Report.docx";
const RES = "/home/z/my-project/footprint/results";

// ---------- palette: IG-1 Ink Gold (finance) ----------
const P = {
  bg: "1A1A1A", accent: "C9A84C",
  cover: { titleColor: "FFFFFF", subtitleColor: "B0B8C0", metaColor: "90989F", footerColor: "687078" },
  table: { headerBg: "C9A84C", headerText: "1A1A1A", innerLine: "DDD5C0", surface: "F5F2E8" },
  primary: "1A1A1A",
};
const NB = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
const noBorders = { top: NB, bottom: NB, left: NB, right: NB };
const allNoBorders = { ...noBorders, insideHorizontal: NB, insideVertical: NB };

// ---------- cover helpers (design-system) ----------
function estimateTextWidth(text, pt) {
  let w = 0;
  for (const ch of text) {
    const code = ch.codePointAt(0);
    const isCJK = (code >= 0x4e00 && code <= 0x9fff) || (code >= 0x3400 && code <= 0x4dbf) ||
      (code >= 0x3000 && code <= 0x303f) || (code >= 0xff00 && code <= 0xffef);
    w += isCJK ? pt * 20 : pt * 11;
  }
  return w;
}
function splitTitleLines(title, maxWidthTw, pt) {
  if (estimateTextWidth(title, pt) <= maxWidthTw) return [title];
  const words = title.split(" ");
  const lines = [];
  let cur = "";
  for (const w of words) {
    const cand = cur ? cur + " " + w : w;
    if (estimateTextWidth(cand, pt) > maxWidthTw && cur) { lines.push(cur); cur = w; }
    else cur = cand;
  }
  if (cur) lines.push(cur);
  if (lines.length > 1 && lines[lines.length - 1].length <= 2) {
    const last = lines.pop();
    lines[lines.length - 1] += " " + last;
  }
  return lines;
}
function calcTitleLayout(title, maxWidthTwips, preferredPt = 40, minPt = 24) {
  let titlePt = preferredPt, lines;
  while (titlePt >= minPt) {
    lines = splitTitleLines(title, maxWidthTwips, titlePt);
    if (lines.length <= 3) break;
    titlePt -= 2;
  }
  if (!lines || lines.length > 3) { lines = splitTitleLines(title, maxWidthTwips, minPt); titlePt = minPt; }
  return { titlePt, titleLines: lines };
}
function calcCoverSpacing(p) {
  const { titleLineCount = 1, titlePt = 36, hasSubtitle = false, hasEnglishLabel = false,
    metaLineCount = 0, fixedHeight = 800, pageHeight = 16838 } = p;
  const SAFETY = 1200;
  const usable = pageHeight - SAFETY;
  const contentH = titleLineCount * (titlePt * 23 + 200) + (hasSubtitle ? 12 * 23 + 600 : 0) +
    (hasEnglishLabel ? 9 * 23 + 600 : 0) + metaLineCount * (10 * 23 + 100) + fixedHeight + 3 * 300;
  const remaining = Math.max(usable - contentH, 400);
  const FOOTER_MIN = 800;
  const rawTop = Math.floor(remaining * 0.45), rawBottom = Math.floor(remaining * 0.45);
  const bottomSpacing = Math.max(rawBottom, FOOTER_MIN);
  const topSpacing = Math.max(rawTop - Math.max(0, FOOTER_MIN - rawBottom), 400);
  return { topSpacing, midSpacing: Math.max(remaining - topSpacing - bottomSpacing, 0), bottomSpacing };
}
function buildCoverR1(config) {
  const C = config.palette;
  const padL = 1200, padR = 800;
  const availableWidth = 11906 - padL - padR - 300;
  const { titlePt, titleLines } = calcTitleLayout(config.title, availableWidth, 40, 24);
  const titleSize = titlePt * 2;
  const spacing = calcCoverSpacing({
    titleLineCount: titleLines.length, titlePt,
    hasSubtitle: !!config.subtitle, hasEnglishLabel: !!config.englishLabel,
    metaLineCount: (config.metaLines || []).length, fixedHeight: 400,
  });
  const accentLeft = { style: BorderStyle.SINGLE, size: 8, color: C.accent, space: 12 };
  const children = [];
  children.push(new Paragraph({ spacing: { before: spacing.topSpacing } }));
  if (config.englishLabel) {
    children.push(new Paragraph({
      indent: { left: padL, right: padR }, spacing: { after: 500 },
      border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: C.accent, space: 8 } },
      children: [new TextRun({ text: config.englishLabel.split("").join("  "), size: 18,
        color: C.accent, font: { ascii: "Calibri" }, characterSpacing: 40 })],
    }));
  }
  for (let i = 0; i < titleLines.length; i++) {
    children.push(new Paragraph({
      indent: { left: padL },
      spacing: { after: i < titleLines.length - 1 ? 100 : 300, line: Math.ceil(titlePt * 23), lineRule: "atLeast" },
      children: [new TextRun({ text: titleLines[i], size: titleSize, bold: true,
        color: C.cover.titleColor, font: { ascii: "Arial" } })],
    }));
  }
  if (config.subtitle) {
    children.push(new Paragraph({
      indent: { left: padL, right: padR }, spacing: { after: 800 },
      children: [new TextRun({ text: config.subtitle, size: 24, color: C.cover.subtitleColor,
        font: { ascii: "Arial" } })],
    }));
  }
  for (const line of (config.metaLines || [])) {
    children.push(new Paragraph({
      indent: { left: padL + 200 }, spacing: { after: 80 },
      border: { left: accentLeft },
      children: [new TextRun({ text: line, size: 24, color: C.cover.metaColor, font: { ascii: "Arial" } })],
    }));
  }
  children.push(new Paragraph({ spacing: { before: spacing.bottomSpacing } }));
  children.push(new Paragraph({
    indent: { left: padL, right: padR },
    border: { top: { style: BorderStyle.SINGLE, size: 2, color: C.accent, space: 8 } },
    spacing: { before: 200 },
    children: [
      new TextRun({ text: config.footerLeft || "", size: 16, color: C.cover.footerColor, font: { ascii: "Arial" } }),
      new TextRun({ text: "                                        " }),
      new TextRun({ text: config.footerRight || "", size: 16, color: C.cover.footerColor, font: { ascii: "Arial" } }),
    ],
  }));
  return [new Table({
    width: { size: 100, type: WidthType.PERCENTAGE },
    layout: TableLayoutType.FIXED,
    borders: allNoBorders,
    rows: [new TableRow({
      height: { value: 16838, rule: "exact" },
      children: [new TableCell({
        shading: { type: ShadingType.CLEAR, fill: C.bg }, borders: noBorders,
        verticalAlign: "top",
        children,
      })],
    })],
  })];
}

// ---------- body helpers (English, Profile A) ----------
const F = { ascii: "Times New Roman" };
function h1(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_1, alignment: AlignmentType.CENTER,
    spacing: { before: 360, after: 160, line: 312 },
    children: [new TextRun({ text, bold: true, size: 32, color: "000000", font: F })],
  });
}
function h2(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 240, after: 120, line: 312 },
    children: [new TextRun({ text, bold: true, size: 30, color: "000000", font: F })],
  });
}
function p(text, opts = {}) {
  const runs = Array.isArray(text) ? text : [new TextRun({ text, size: 24, color: "000000", font: F })];
  return new Paragraph({
    alignment: AlignmentType.LEFT,
    spacing: { line: 312, after: opts.after ?? 120 },
    keepNext: opts.keepNext || false,
    children: runs,
  });
}
function bold(t) { return new TextRun({ text: t, bold: true, size: 24, color: "000000", font: F }); }
function t_(t) { return new TextRun({ text: t, size: 24, color: "000000", font: F }); }
function caption(text) {
  return new Paragraph({
    alignment: AlignmentType.CENTER, keepNext: true,
    spacing: { before: 60, after: 60, line: 312 },
    children: [new TextRun({ text, bold: true, size: 21, color: "000000", font: F })],
  });
}
function figCaption(text) {
  return new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 60, after: 200, line: 312 },
    children: [new TextRun({ text, size: 21, color: "404040", font: F, italics: true })],
  });
}
function cell(content, opts = {}) {
  const runs = String(content).split("\n").map((line, i) =>
    new TextRun({ text: line, size: 21, bold: opts.bold || false,
      color: opts.color || "000000", font: F, break: i > 0 ? 1 : 0 }));
  return new TableCell({
    children: [new Paragraph({
      alignment: opts.align || AlignmentType.LEFT,
      spacing: { line: 312 },
      children: runs,
    })],
    shading: opts.fill ? { type: ShadingType.CLEAR, fill: opts.fill } : undefined,
    margins: { top: 60, bottom: 60, left: 120, right: 120 },
    width: { size: opts.w || 20, type: WidthType.PERCENTAGE },
  });
}
function table(headers, rows, widths, aligns) {
  return new Table({
    width: { size: 100, type: WidthType.PERCENTAGE },
    borders: {
      top: { style: BorderStyle.SINGLE, size: 4, color: P.accent },
      bottom: { style: BorderStyle.SINGLE, size: 4, color: P.accent },
      left: NB, right: NB,
      insideHorizontal: { style: BorderStyle.SINGLE, size: 1, color: P.table.innerLine },
      insideVertical: NB,
    },
    rows: [
      new TableRow({
        tableHeader: true, cantSplit: true,
        children: headers.map((h, i) => cell(h, {
          bold: true, fill: P.table.headerBg, color: P.table.headerText,
          align: aligns ? aligns[i] : AlignmentType.LEFT, w: widths[i],
        })),
      }),
      ...rows.map((r, ri) => new TableRow({
        cantSplit: true,
        children: r.map((c, i) => cell(c, {
          fill: ri % 2 === 0 ? P.table.surface : "FFFFFF",
          align: aligns ? aligns[i] : AlignmentType.LEFT, w: widths[i],
        })),
      })),
    ],
  });
}
function fig(path, w, h, cap) {
  const buf = fs.readFileSync(path);
  return [
    new Paragraph({
      alignment: AlignmentType.CENTER, keepNext: true, spacing: { before: 120 },
      children: [new ImageRun({ data: buf, type: "png", transformation: { width: w, height: h } })],
    }),
    figCaption(cap),
  ];
}

// ---------- content data (verbatim from runs) ----------
const R = AlignmentType.RIGHT, CTR = AlignmentType.CENTER, L = AlignmentType.LEFT;
const valRows = [
  ["5-minute bar coverage", "93,883 bars over 326 days (100.0% kline timestamps matched)", R],
  ["OHLC agreement (p99.9 relative difference)", "<= 4.0e-06 on open/high/low/close", R],
  ["Total volume agreement", "correlation 0.999999; totals 17,876,424 vs 17,876,522 BTC", R],
  ["Taker-buy volume agreement", "correlation 1.000000 (confirms is_buyer_maker semantics)", R],
  ["Downtime handling", "2026-10-27 has 283 bars; pipeline handles gaps natively", R],
];
const baseRows = [
  ["strict 0-print", "4,551 (4.85%)", "2,164 buy / 2,387 sell", "+0.5 (t=+2.1)", "-0.7 (t=-0.7)"],
  ["z2 (top-2 zero)", "3,654 (3.89%)", "1,730 / 1,924", "+0.5 (t=+2.2)", "-1.2 (t=-1.1)"],
  ["weak (<=5% flow)", "6,471 (6.89%)", "3,072 / 3,399", "+0.1 (t=+0.7)", "-1.1 (t=-1.2)"],
  ["mirror (continuation)", "55,124 (58.72%)", "27,382 / 27,742", "-0.2 (t=-3.5)", "-0.5 (t=-1.9)"],
];
const gridRows = [
  ["strict | market | 1x SL | 12-bar", "4,551", "+0.020", "1.04", "-0.94"],
  ["strict | market | 3x SL | 12-bar", "4,551", "+0.000", "1.00", "-0.32"],
  ["strict | limit retest | 3x SL | 48-bar", "2,908 (64% filled)", "-0.010", "0.98", "-0.15"],
  ["z2 | market | 4x SL | RR 4 | 96-bar", "2,652 (IS)", "+0.044", "1.08", "-0.19"],
  ["strict | limit retest | 3x SL | 48-bar (OOS split)", "783 (OOS)", "-0.077", "0.86", "-0.24"],
];
const mlRows = [
  ["Fold 0 (days 60-114)", "801", "640", "-0.16 (n=88 at thr 0.45)"],
  ["Fold 1 (days 114-168)", "1,445", "551", "+0.01 (n=178 at thr 0.45)"],
  ["Fold 2 (days 168-222)", "1,991", "587", "-0.11 (n=75 at thr 0.50)"],
  ["Fold 3 (days 222-276)", "2,587", "486", "-0.16 (n=79 at thr 0.45)"],
  ["Fold 4 - FINAL, untouched (days 276-326)", "3,082", "562", "-0.30 (n=101 at thr 0.47)"],
];
const btRows = [
  ["Trades taken", "874", "292"],
  ["Win rate", "34.4%", "44.5%"],
  ["Mean R (before fees)", "-0.027", "+0.017"],
  ["Fee-adjusted mean R", "-0.292", "-0.126"],
  ["Profit factor", "0.95", "1.04"],
  ["Total P&L at $25 risk", "-$6,389", "-$921"],
  ["Maximum drawdown", "$6,844", "$1,042"],
  ["Bootstrap 95% CI of total P&L", "-$7,200 to -$5,600 (approx.)", "-$1,715 to -$79"],
  ["Probability of profit (bootstrap)", "0%", "4%"],
  ["Deflated Sharpe probability (78 trials)", "0.000", "0.000"],
];
const filesRows = [
  ["fp_download.py", "Binance Vision aggTrades/klines -> compact footprint store (bars / raw-tick edges / 1-USD grid)"],
  ["fp_validate.py", "Cross-validation of the footprint against Binance's own klines"],
  ["fp_events.py", "Event study: 4 variants x 2 entries x 3 stops x 2 horizons, IS/OOS splits, trials counter"],
  ["fp_rr_sweep.py", "RR sweep locating the fee-viable region (breakeven win-rate gap)"],
  ["fp_train.py", "Purged walk-forward LightGBM + isotonic calibration + frozen final fold"],
  ["fp_backtest.py", "Fee-aware event-loop backtest, bootstrap CI, deflated Sharpe"],
  ["fp_watch.py", "Live/replay/selftest watcher: rolling footprint -> ML gate -> order tickets"],
  ["fp_core.py", "Shared library: loaders, event definitions, 35 features, triple-barrier labels"],
];

// ---------- document ----------
const header = new Header({
  children: [new Paragraph({
    alignment: AlignmentType.CENTER,
    children: [new TextRun({ text: "Footprint Auction-Complete ML - Quant Research Report", size: 18, color: "808080", font: F })],
  })],
});
const footerOf = (fmt) => new Footer({
  children: [new Paragraph({
    alignment: AlignmentType.CENTER,
    children: [new TextRun({ children: [PageNumber.CURRENT], size: 18, color: "808080", font: F })],
  })],
});

const bodyChildren = [
  h1("1. Executive Summary"),
  p([t_("This report reverse-engineers the footprint trading hypothesis known as "), bold("auction complete"), t_(": when a 0-print appears at the top of a bar's footprint (zero aggressive buying at the high), the upside auction is considered finished and the trade is a SELL; symmetrically, a 0-print at the low signals BUY. The study covers "), bold("326 days"), t_(" of BTCUSDT USDT-M perpetual trading (2026-01-01 to 2026-11-22, 93,883 five-minute bars) rebuilt tick-by-tick from Binance Vision aggTrades into a full price-level footprint, cross-validated against Binance's own klines (taker-buy volume correlation 1.000000).")]),
  p([t_("The verdict is a quantified "), bold("NO-GO for standalone deployment"), t_(": the raw rule is a coin flip before fees (best configuration +0.044R per trade, profit factor 1.08, not significant after deflation across 75 evaluated configurations) and strictly negative after realistic fees (-0.13R to -0.30R per trade). A walk-forward-purged LightGBM model with 35 order-flow features does extract real signal - it lifts the win rate from 34.4% to 44.5% and halves the loss - but the bootstrap probability of profit on the untouched final fold remains "), bold("4%"), t_(". The binding constraint is structural: at 5-minute granularity the stop distance is small, so a 10 bps round-trip costs 0.19R-1.2R per trade, more than the signal's entire gross edge.")]),
  p([t_("What the desk receives instead of a losing strategy is the "), bold("complete production machinery"), t_(": a validated footprint data pipeline, a 75-configuration event-study engine, a purged walk-forward ML stack, a fee-aware backtester with bootstrap and deflated-Sharpe checks, and a live watcher that closes a footprint bar, grades it with the model and prints an executable order ticket in 59 ms (4,500x real time). Every number in this report is reproducible from the shipped scripts. Section 7 lists the three evidence-backed paths that remain open.")]),

  h1("2. Background and Objectives"),
  p([t_("The trading idea under test originates from order-flow trading practice: a footprint chart displays, for every price level of a bar, the volume traded by aggressive buyers versus aggressive sellers. When the highest traded price of a bar shows "), bold("zero aggressive buy volume"), t_(" (a 0-print at the top), no participant was willing to pay the offer at the extreme; the level was made and held by sellers hitting bids. Practitioners read this as the upside auction being complete, and fade it. The bottom-of-bar 0-print is read symmetrically as a buy signal.")]),
  p([t_("The study had three objectives. First, quantify the hypothesis honestly at desk standard - all variants, multiple horizons, realistic costs, out-of-sample splits, and a deflated-Sharpe adjustment for the number of configurations searched. Second, test whether a machine-learning model grading each event can isolate a profitable subset; the model uses 35 live-computable footprint, bar-shape and context features and is validated with a purged walk-forward scheme whose final fold is never used for any selection decision. Third, package whatever survives as a production system: a live watcher consuming the exchange feed, maintaining a rolling footprint, and printing order tickets sized for both Binance and the desk's MT5 crypto CFD (BTCUSD.p).")]),

  h1("3. Data, Footprint Construction and Validation"),
  h2("3.1 Data"),
  p([t_("Binance Vision daily aggTrades archives for BTCUSDT USDT-M perpetual were downloaded for 2026-01-01 through 2026-11-22 and processed day-by-day into a three-table store: "), bold("bars"), t_(" (5-minute OHLC, aggressive buy/sell volume, trade counts), "), bold("edges"), t_(" (the top and bottom 12 traded price levels of every bar at raw 0.10 tick precision - this is where 0-prints live), and "), bold("grid"), t_(" (the full 1-USD-bucket volume ladder for point-of-control, absorption and imbalance features). One full day of BTCUSDT aggTrades is roughly 2 million trades; peak memory during processing stayed below 1 GB thanks to streamed per-day aggregation.")]),
  h2("3.2 Validation"),
  p([t_("Before any backtest, the footprint was cross-validated against Binance's independently produced 5-minute klines. This step caught and fixed one real bug in our own tooling (a kline column-index error that mislabeled the trade-count column as taker-buy volume) and one exchange quirk (2026-10-27 contains only 283 bars due to exchange downtime; all loaders handle variable day lengths natively). Final validation results:")] , { keepNext: true }),
  caption("Table 1: Data integrity checks (footprint vs Binance klines)"),
  table(["Check", "Result"], valRows, [40, 60], [L, L]),
  p([t_("Aggressive-buy attribution is the foundation of every feature in this study; its exact agreement with Binance's taker-buy volume (correlation 1.000000, p99.9 relative difference below 2.5e-02) confirms the "), bold("is_buyer_maker=True means aggressive SELL"), t_(" interpretation is applied identically to the exchange's own accounting.")]),

  h1("4. Reverse Engineering the Auction-Complete Signal"),
  h2("4.1 Event definitions and base rates"),
  p([t_("Four operationalizations of the hypothesis were tested: "), bold("strict"), t_(" (exact 0-print at the extreme tick), "), bold("z2"), t_(" (zero aggressive counter-flow aggregated over the top two tick levels), "), bold("weak"), t_(" (counter-flow at most 5% of extreme-level volume), and "), bold("mirror"), t_(" - the inverse hypothesis that aggressive flow still present at the extreme predicts continuation rather than reversal. Table 2 reports how often each fires and the side-adjusted forward return after the event; Figure 1 shows the full horizon profile against the 10 bps round-trip fee hurdle.")], { keepNext: true }),
  caption("Table 2: Base rates and side-adjusted forward returns (11 months)"),
  table(["Variant", "Events (rate)", "Buy/Sell side", "h=1 bar (bps, t)", "h=24 bars (bps, t)"],
    baseRows, [22, 20, 20, 19, 19], [L, R, CTR, R, R]),
  ...fig(`${RES}/fig_forward.png`, 560, 286,
    "Figure 1: Side-adjusted forward returns by holding horizon. The only durable effect is a +0.5 bps one-bar reaction; every horizon sits far below the 10 bps fee line."),
  h2("4.2 Triple-barrier results and execution engineering"),
  p([t_("Each event was labeled with a pessimistic triple-barrier trade: entry at the next bar's open (taker) or at a limit retest of the signal-bar extreme (maker), stop at 1-3x the signal-bar range, take-profit at 1.5R, and a 12- or 48-bar time stop. Table 3 summarizes the informative corners of the 48-configuration grid; none reaches positive fee-adjusted expectancy, and the best in-sample corners decay out of sample.")], { keepNext: true }),
  caption("Table 3: Barrier-grid highlights (RR 1.5 unless stated; fees at taker 5 bps/side, maker 2 bps/side)"),
  table(["Configuration", "n", "Mean R (pre-fee)", "PF", "Fee-adjusted R"],
    gridRows, [36, 14, 16, 12, 22], [L, R, R, R, R]),
  p([t_("Two structural findings follow. First, "), bold("the limit-retest entry is adversely selected"), t_(": it fills precisely when the level is revisited, and the 11-month sample prices that adverse selection at -0.01R to -0.06R pre-fee - the maker fee saving is consumed by worse fills. Second, widening the stop reduces the fee burden roughly linearly (a 3x stop cuts the fee to 0.32R, a 4x stop to 0.19R) while the gross edge stays a coin flip; the RR sweep (27 further configurations) located the most liftable base at z2 / market / 4x stop / RR 4 / 96-bar horizon: +0.044R pre-fee, profit factor 1.08 in sample.")]),
  p([t_("A one-bar micro-reaction of "), bold("+0.5 bps (t = +2.1 on 4,548 strict events, replicated at t = +2.2 on the z2 variant)"), t_(" is the only statistically durable pattern the hypothesis produced. It is real, it is replicated across variants, and it is economically irrelevant at taker fees: the move is one-twentieth of the cost of the round trip.")]),

  h1("5. ML Model and Walk-Forward Validation"),
  h2("5.1 Design"),
  p([t_("The ML experiment targets the most fee-viable base configuration (z2 events, market entry, 4x stop, RR 4, 96-bar horizon; 3,654 labeled events, base win rate 37.5%). The model is a LightGBM binary classifier with 35 features: extreme-level micro-structure (buy share and volume at the top/bottom tick, aggregated top-3 flow), footprint shape (bar delta, body and wick fractions, POC position and delta, delta concentration, high/low-zone flow shares), bar context (range versus ATR and median, volume percentile), and regime (8h/24h EMA trend distances, 6/24-bar momentum, 24h range position, session hour). Every feature is computed from closed-bar data only, exactly reproducible at deploy time by the watcher.")]),
  p([t_("Validation uses "), bold("purged walk-forward"), t_(": five folds rolling every 54 days, each trained on all history before the test window minus a 2-day embargo (labels look forward up to 96 bars), with a final untouched fold covering the last 50 days. The probability threshold (0.47) and the isotonic calibration were selected on folds 0-3 only; fold 4 was evaluated once, frozen. This is the standard that makes the final number trustworthy.")], { keepNext: true }),
  caption("Table 4: Walk-forward folds (fee-adjusted mean R per taken trade)"),
  table(["Fold", "Train events", "Test events", "Best threshold fee-adj R"],
    mlRows, [34, 16, 16, 34], [L, R, R, L]),
  h2("5.2 Backtest: raw rule versus ML-filtered"),
  p([t_("Both strategies run through the same event-loop simulator: one position at a time, $25 fixed risk per trade, pessimistic barrier fills, fees charged in R according to entry type. The ML filter takes 16% of out-of-fold events and genuinely improves them - but the improvement is a halving of losses, not a profit.")], { keepNext: true }),
  caption("Table 5: Out-of-fold backtest, raw rule versus ML filter ($25 risk per trade)"),
  table(["Metric", "Raw rule", "ML filtered (p >= 0.47)"],
    btRows, [40, 30, 30], [L, R, R]),
  ...fig(`${RES}/equity_curve.png`, 560, 311,
    "Figure 2: Fee-adjusted cumulative P&L, raw rule (grey) versus ML-filtered (teal). The ML curve is flatter and less drawdown-prone but never crosses into profit."),
  p([t_("Statistical honesty requires the negative framing: the bootstrap over 5,000 resamples of the ML trade sequence puts the 95% confidence interval of total P&L at "), bold("-$1,715 to -$79"), t_(" and the probability of profit at "), bold("4%"), t_("; the deflated Sharpe test against the 78 searched configurations returns a probability of effectively zero. The model has modest discrimination (it moves the win rate by 10 points) - the signal underneath simply does not contain enough information to clear the fee hurdle.")]),

  h1("6. Diagnosis: Why the Signal Cannot Pay for Itself"),
  p([t_("The fee arithmetic dominates everything else in this study. At 5-minute granularity the BTCUSDT bar range is typically 0.1-0.2% of price, so a stop placed at one bar range risks roughly 10-20 bps of price. Binance USDT-M taker fees of 5 bps per side mean the round trip costs 10 bps - the same order as the entire stop distance, which is why the strict 1x-stop configuration pays "), bold("0.94R of fees per trade"), t_(" against a gross edge of +0.02R. Widening to a 4x stop cuts the fee to 0.19R, but the gross edge at the wider setting remains a coin flip (+0.04R), so the trade is still born 0.15R short.")]),
  p([t_("Second, the information content of the 0-print is exhausted within one bar. The h=1 reaction of +0.5 bps decays to zero by 30 minutes and turns mildly negative at two hours. A signal that predicts five minutes of microstructure drift cannot pay 10 bps of fees under any stop/TP geometry tested in this study.")]),
  p([t_("Third, the mirror hypothesis fails symmetrically: fading the 0-print does not work, but going with the remaining aggressive flow at the extreme does not work either (-0.2 to -0.5 bps side-adjusted at all horizons). At 5-minute resolution the extreme-tick flow composition is, for practical purposes, noise around a fair price - consistent with the market being approximately efficient at this horizon and cost structure.")]),

  h1("7. Conclusions and Recommendations"),
  h2("7.1 Verdict"),
  p([t_("The auction-complete 0-print, traded standalone on BTCUSDT at exchange fees, is "), bold("rejected"), t_(": gross edge statistically indistinguishable from zero, negative after fees in all 75 tested configurations, and not rescuable by ML grading (final-fold fee-adjusted expectancy -0.30R per trade; probability of profit 4%). The watcher is shipped for research and dry-run use; the desk should not route live risk to it in this configuration.")]),
  h2("7.2 What to do with the machinery"),
  p([t_("The pipeline is the deliverable that compounds. Its five commands (download, events, train, backtest, watch) test any footprint hypothesis at the same rigor this report applied, in minutes. Three directions have concrete evidential support and are recommended, each with an owner and cadence:")]),
  p([bold("(1) Fee-structure arbitrage (research, this desk, next quarter). "), t_("The +0.5 bps one-bar reaction is real and replicated. It is tradeable only if the round trip costs well under 0.5 bps - maker-with-rebate execution on both sides, or zero-fee/promotional venues. The backtester already models maker fills with adverse selection; re-run it with the venue's actual rebate schedule before any capital is committed. This is a research task, not a deployment task.")]),
  p([bold("(2) Context overlay for the existing profitable desk (highest expected value). "), t_("The karan1399 desk already runs a validated multi-timeframe EMA-rejection system (alpha360) with out-of-sample profit factors of 1.12-1.16. The untested but plausible integration is: when an alpha360 signal fires, wait for the 5-minute footprint to print an auction-complete in the signal direction at the entry zone before committing, or use the print to time the entry inside the zone. This converts a standalone losing signal into an entry-timing filter on a proven base; the alpha360 backtest harness can quantify the lift in one session.")]),
  p([bold("(3) Cross-sectional extension (research, machine time only). "), t_("Every finding here is single-instrument. The same pipeline with SYMBOL=ETHUSDT (and other majors) tests whether the 0-print carries more information in less efficiently traded books. The scripts are symbol-parameterized; no new code is required.")]),
  h2("7.3 Production guardrails"),
  p([t_("If any future configuration passes, deployment discipline applies unchanged from the desk standard: monthly retraining (fp_train.py), a rolling 100-trade profit-factor kill switch at 0.9, one position at a time, closed-bar signals only, and the watcher's journal (watch_log.csv) reconciled weekly against fills. The watcher's order tickets include Binance BTC sizing and MT5 BTCUSD.p lots so the desk's existing manual punching workflow transfers unchanged.")]),

  h1("8. Appendix"),
  h2("8.1 System files"),
  caption("Table 6: Pipeline components (all shipped, reproducible end to end)"),
  table(["File", "Role"], filesRows, [22, 78], [L, L]),
  h2("8.2 Reproduction commands"),
  p([t_("From the footprint_auction_ml folder: python3 fp_download.py full (build the store from Binance Vision), python3 fp_validate.py (integrity), python3 fp_events.py (event grid), python3 fp_rr_sweep.py (fee-viable region), python3 fp_train.py z2 market 4.0 96 4.0 (walk-forward model), python3 fp_backtest.py (fee-aware comparison), python3 fp_watch.py --selftest (aggregator parity and latency), python3 fp_watch.py --replay 2026-11-20 (end-to-end ticket demo), python3 fp_watch.py --live (production feed). Total one-machine reproduction time is under 40 minutes excluding downloads.")]),
  h2("8.3 Watcher benchmark"),
  p([t_("The incremental footprint engine builds 576 bars from 600,000 synthetic trades with exact batch parity (aggregator parity PASS), and the full bar-close pipeline - feature recomputation on the 48-hour window, event detection, LightGBM inference, ticket formatting - averages "), bold("59 ms with a p95 of 65 ms"), t_(" against a 300,000 ms bar budget: a 4,500x real-time margin.")]),
];

const doc = new Document({
  styles: {
    default: {
      document: {
        run: { font: F, size: 24, color: "000000" },
        paragraph: { spacing: { line: 312 } },
      },
      heading1: { run: { font: F, size: 32, bold: true, color: "000000" },
        paragraph: { spacing: { before: 360, after: 160, line: 312 } } },
      heading2: { run: { font: F, size: 30, bold: true, color: "000000" },
        paragraph: { spacing: { before: 240, after: 120, line: 312 } } },
    },
  },
  sections: [
    { // cover - no page number
      properties: { page: { size: { width: 11906, height: 16838 },
        margin: { top: 0, bottom: 0, left: 0, right: 0 } } },
      children: buildCoverR1({
        title: "Footprint Auction ML",
        subtitle: "The 0-print 'auction complete' signal: reverse engineering, ML grading and production tooling for BTCUSDT",
        englishLabel: "QUANT RESEARCH REPORT",
        metaLines: [
          "Instrument: BTCUSDT USDT-M perpetual (Binance)",
          "Sample: 326 days, 2026-01-01 to 2026-11-22, 93,883 bars",
          "Data: tick-level aggTrades footprint, validated vs klines",
          "Prepared for: karan1399 Trading-Strategies desk",
        ],
        footerLeft: "Quant Research Desk",
        footerRight: "2026-11-23",
        palette: P,
      }),
    },
    { // TOC - Roman numerals
      properties: {
        type: SectionType.NEXT_PAGE,
        page: { size: { width: 11906, height: 16838 },
          margin: { top: 1440, bottom: 1440, left: 1701, right: 1417 },
          pageNumbers: { start: 1, formatType: NumberFormat.UPPER_ROMAN } },
      },
      footers: { default: footerOf("roman") },
      children: [
        new Paragraph({
          alignment: AlignmentType.CENTER, spacing: { before: 200, after: 240 },
          children: [new TextRun({ text: "Table of Contents", bold: true, size: 32, color: "000000", font: F })],
        }),
        new TableOfContents("Table of Contents", { hyperlink: true, headingStyleRange: "1-2" }),
        new Paragraph({
          spacing: { before: 120 },
          children: [new TextRun({
            text: "Note: if page numbers show as placeholders, right-click the table of contents and choose Update Field.",
            italics: true, size: 20, color: "808080", font: F })],
        }),
      ],
    },
    { // body - Arabic from 1
      properties: {
        type: SectionType.NEXT_PAGE,
        page: { size: { width: 11906, height: 16838 },
          margin: { top: 1440, bottom: 1440, left: 1701, right: 1417 },
          pageNumbers: { start: 1, formatType: NumberFormat.DECIMAL } },
      },
      headers: { default: header },
      footers: { default: footerOf("arabic") },
      children: bodyChildren,
    },
  ],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(OUT, buf);
  console.log("written", OUT, buf.length, "bytes");
});
