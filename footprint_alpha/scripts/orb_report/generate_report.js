// generate_report.js — ORB Quantitative Audit & Evolved Design report (English, R1 cover, IG-1 palette)
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  ImageRun, PageBreak, Header, Footer, PageNumber, NumberFormat,
  AlignmentType, HeadingLevel, WidthType, BorderStyle, ShadingType,
  SectionType, TableOfContents, TableLayoutType,
} = require("docx");
const fs = require("fs");
const path = require("path");

// ---------------------------------------------------------------- palette (IG-1 Ink Gold, finance)
const P = {
  bg: "1A1A1A", accent: "C9A84C",
  cover: { titleColor: "FFFFFF", subtitleColor: "B0B8C0", metaColor: "90989F", footerColor: "687078" },
  table: { headerBg: "C9A84C", headerText: "1A1A1A", accentLine: "C9A84C", innerLine: "DDD5C0", surface: "F5F2E8" },
};
const BODY = "000000";           // Profile A: pure black
const FONT = { ascii: "Times New Roman", eastAsia: "Times New Roman" };

const NB = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
const noBorders = { top: NB, bottom: NB, left: NB, right: NB };
const allNoBorders = { top: NB, bottom: NB, left: NB, right: NB, insideHorizontal: NB, insideVertical: NB };

function safeText(v, ph) {
  if (v === undefined || v === null || v === "" || String(v) === "NaN" || String(v) === "undefined") {
    return ph || "n/a";
  }
  return String(v);
}

// ---------------------------------------------------------------- text helpers
function h1(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_1, alignment: AlignmentType.CENTER,
    spacing: { before: 360, after: 160, line: 312 },
    children: [new TextRun({ text, bold: true, size: 32, color: BODY, font: FONT })],
  });
}
function h2(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 240, after: 120, line: 312 },
    children: [new TextRun({ text, bold: true, size: 30, color: BODY, font: FONT })],
  });
}
function h3(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_3,
    spacing: { before: 200, after: 100, line: 312 },
    children: [new TextRun({ text, bold: true, size: 28, color: BODY, font: FONT })],
  });
}
// body paragraph; runs = string or array of {t, b} fragments
function para(runs, opts) {
  const o = opts || {};
  const frags = (typeof runs === "string") ? [{ t: runs }] : runs;
  return new Paragraph({
    alignment: AlignmentType.LEFT,
    spacing: { line: 312, after: o.after !== undefined ? o.after : 120, before: o.before || 0 },
    children: frags.map(f => new TextRun({
      text: safeText(f.t), bold: !!f.b, italics: !!f.i, size: 24, color: BODY, font: FONT,
    })),
  });
}
function caption(text) {
  return new Paragraph({
    keepNext: true, alignment: AlignmentType.LEFT,
    spacing: { before: 160, after: 80, line: 312 },
    children: [new TextRun({ text, bold: true, size: 21, color: BODY, font: FONT })],
  });
}
// data table: header = [..], rows = [[..]], widths = [%], boldCol0
function dataTable(header, rows, widths, opts) {
  const o = opts || {};
  const mkCell = (text, isHeader, wPct, first) => new TableCell({
    width: { size: wPct, type: WidthType.PERCENTAGE },
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    shading: isHeader
      ? { type: ShadingType.CLEAR, fill: P.table.headerBg }
      : (o.zebra && o.rowIdx % 2 === 0 ? { type: ShadingType.CLEAR, fill: P.table.surface } : undefined),
    children: [new Paragraph({
      alignment: (o.alignRight && o.alignRight.includes(header.length - 1) && !first && !isHeader)
        ? AlignmentType.RIGHT : AlignmentType.LEFT,
      spacing: { line: 276 },
      children: [new TextRun({
        text: safeText(text), bold: isHeader || (first && o.boldFirst),
        size: 19, color: isHeader ? P.table.headerText : BODY, font: FONT,
      })],
    })],
  });
  const trs = [new TableRow({
    tableHeader: true, cantSplit: true,
    children: header.map((t, i) => mkCell(t, true, widths[i], i === 0)),
  })];
  rows.forEach((r, ri) => {
    o.rowIdx = ri;
    trs.push(new TableRow({
      cantSplit: true,
      children: r.map((t, i) => mkCell(t, false, widths[i], i === 0)),
    }));
  });
  return new Table({
    width: { size: 100, type: WidthType.PERCENTAGE },
    layout: TableLayoutType.FIXED,
    borders: {
      top: { style: BorderStyle.SINGLE, size: 2, color: P.table.accentLine },
      bottom: { style: BorderStyle.SINGLE, size: 2, color: P.table.accentLine },
      left: NB, right: NB,
      insideHorizontal: { style: BorderStyle.SINGLE, size: 1, color: P.table.innerLine },
      insideVertical: NB,
    },
    rows: trs,
  });
}
function figure(file, w, h, capText) {
  const buf = fs.readFileSync(file);
  return [
    new Paragraph({
      alignment: AlignmentType.CENTER, spacing: { before: 120, after: 60 },
      children: [new ImageRun({ data: buf, type: "png", transformation: { width: w, height: h } })],
    }),
    new Paragraph({
      alignment: AlignmentType.CENTER, spacing: { after: 160, line: 312 },
      children: [new TextRun({ text: capText, size: 21, color: "404040", italics: true, font: FONT })],
    }),
  ];
}

// ---------------------------------------------------------------- cover (Recipe R1)
function estimateTextWidth(text, pt) {
  let width = 0;
  for (const ch of text) {
    const code = ch.codePointAt(0);
    const isCJK = (code >= 0x4E00 && code <= 0x9FFF) || (code >= 0x3000 && code <= 0x303F) ||
      (code >= 0xFF00 && code <= 0xFFEF);
    width += isCJK ? pt * 20 : pt * 11;
  }
  return width;
}
function splitTitleLinesMixed(title, maxWidthTwips, pt) {
  const words = title.split(" ");
  const lines = []; let cur = "";
  for (const w of words) {
    const cand = cur ? cur + " " + w : w;
    if (estimateTextWidth(cand, pt) <= maxWidthTwips || !cur) cur = cand;
    else { lines.push(cur); cur = w; }
  }
  if (cur) lines.push(cur);
  if (lines.length > 1 && lines[lines.length - 1].length <= 4) {
    const last = lines.pop(); lines[lines.length - 1] += " " + last;
  }
  return lines;
}
function calcTitleLayoutMixed(title, maxWidthTwips, preferredPt, minPt) {
  let titlePt = preferredPt, lines;
  while (titlePt >= minPt) {
    lines = splitTitleLinesMixed(title, maxWidthTwips, titlePt);
    if (lines.length <= 3 && lines.every(l => estimateTextWidth(l, titlePt) <= maxWidthTwips)) break;
    titlePt -= 2;
  }
  if (!lines) lines = [title];
  return { titlePt, titleLines: lines };
}
function calcCoverSpacing(params) {
  const { titleLineCount = 1, titlePt = 36, hasSubtitle = false, hasEnglishLabel = false,
    metaLineCount = 0, fixedHeight = 800, pageHeight = 16838 } = params;
  const SAFETY = 1200;
  const usableHeight = pageHeight - SAFETY;
  const titleHeight = titleLineCount * (titlePt * 23 + 200);
  const subtitleHeight = hasSubtitle ? (12 * 23 + 600) : 0;
  const englishLabelHeight = hasEnglishLabel ? (9 * 23 + 600) : 0;
  const metaHeight = metaLineCount * (10 * 23 + 100);
  const implicitParaHeight = 3 * 300;
  const contentHeight = titleHeight + subtitleHeight + englishLabelHeight + metaHeight + fixedHeight + implicitParaHeight;
  const remainingSpace = usableHeight - contentHeight;
  const safeRemaining = Math.max(remainingSpace, 400);
  const FOOTER_MIN = 800;
  const rawTop = Math.floor(safeRemaining * 0.45);
  const rawBottom = Math.floor(safeRemaining * 0.45);
  const bottomSpacing = Math.max(rawBottom, FOOTER_MIN);
  const topSpacing = Math.max(rawTop - Math.max(0, FOOTER_MIN - rawBottom), 400);
  const midSpacing = Math.max(safeRemaining - topSpacing - bottomSpacing, 0);
  return { topSpacing, midSpacing, bottomSpacing };
}
function buildCoverR1(config) {
  const C = config.palette;
  const padL = 1200, padR = 800;
  const availableWidth = 11906 - padL - padR - 300;
  const { titlePt, titleLines } = calcTitleLayoutMixed(config.title, availableWidth, 40, 24);
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
      children: [new TextRun({ text: config.englishLabel.split("").join("  "), size: 18, color: C.accent,
        font: { ascii: "Arial", eastAsia: "SimHei" }, characterSpacing: 40 })],
    }));
  }
  for (let i = 0; i < titleLines.length; i++) {
    children.push(new Paragraph({
      indent: { left: padL },
      spacing: { after: i < titleLines.length - 1 ? 100 : 300, line: Math.ceil(titlePt * 23), lineRule: "atLeast" },
      children: [new TextRun({ text: titleLines[i], size: titleSize, bold: true,
        color: C.cover.titleColor, font: { ascii: "Arial", eastAsia: "SimHei" } })],
    }));
  }
  if (config.subtitle) {
    children.push(new Paragraph({
      indent: { left: padL }, spacing: { after: 800 },
      children: [new TextRun({ text: config.subtitle, size: 24, color: C.cover.subtitleColor,
        font: { ascii: "Arial", eastAsia: "Microsoft YaHei" } })],
    }));
  }
  for (const line of (config.metaLines || [])) {
    children.push(new Paragraph({
      indent: { left: padL + 200 }, spacing: { after: 80 },
      border: { left: accentLeft },
      children: [new TextRun({ text: line, size: 24, color: C.cover.metaColor,
        font: { ascii: "Arial", eastAsia: "Microsoft YaHei" } })],
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
        shading: { type: ShadingType.CLEAR, fill: C.bg }, borders: noBorders, verticalAlign: "top",
        children,
      })],
    })],
  })];
}

const ASSETS_DIR = "/home/z/my-project/scripts/orb_report/assets";

// ================================================================ BODY CONTENT
const body = [];

// ------------------------------------------------ 1. Executive summary
body.push(h1("1. Executive Summary and Verdict"));
body.push(para([
  { t: "This report delivers a two-part mandate: an independent red-team audit of the baseline Opening Range Breakout (ORB) research published in " },
  { t: "ORB/orb_engine.py", i: true }, { t: ", " }, { t: "ORB/orb_mtf_engine.py", i: true },
  { t: " and the accompanying research report, and the construction of an evolved engine (" },
  { t: "ORB/orb_evolved.py", i: true },
  { t: ") tested directly on the repository's historical parquet data under a stricter, pre-registered protocol. Every number quoted below was recomputed by our own code against the repository's own artifacts; nothing is taken on trust from either the baseline report or any external review." },
]));
body.push(para([
  { t: "Verdict on the baseline. ", b: true },
  { t: "The engineering is deterministic and reproducible — we re-ran the shipped engine on the shipped data and reproduced the headline result exactly (SP500 15m OR, midpoint stop, EOD exit: out-of-sample +48.23R, profit factor 7.45, t-statistic 1.16). The architecture's core design choice, the one-bar shift in the causal 4H-to-15M merge, is correct and we verified it empirically rather than by code reading alone. However, the statistical claims do not survive contact with the artifacts. The qualification filter selects configurations using out-of-sample variables directly; the best advertised result sits comfortably inside a no-edge bootstrap null; the champion portfolio pools in-sample and out-of-sample trades; the reported p-value is off by roughly a factor of 25; and the cost model silently hard-codes two decimal digits for every instrument, which is 1,000x wrong on two of the four forex pairs that anchor the report's most dramatic conclusion." },
]));
body.push(para([
  { t: "Verdict on the regime question. ", b: true },
  { t: "The audit brief asked whether September–November 2026 over-represents a persistent equity bull market. It does not. The out-of-sample windows of every headline configuration sit inside the November 2026 risk-off drawdown, which hit all four audited asset classes simultaneously (November, measured on 4H closes: SP500 −8.7%, NAS100 −11.8%, BTCUSD −21.8%, XAUUSD −9.4%). The baseline's winning out-of-sample configurations are short-biased crash trades on a single 17-day window, not bull-market flattery. This makes the reported profits more fragile, not less: they depend on one regime event, sampled 10–17 trades per asset." },
]));
body.push(para([
  { t: "Verdict on the evolved engine. ", b: true },
  { t: "Under a pre-registered protocol — parameters selected on in-sample data only, frozen, then evaluated once out-of-sample — the evolved engine qualifies exactly two of five assets. NAS100 with a 30-minute range anchored to the NYSE cash open (14:30 UTC) and a 4H EMA200 gate delivered +21.40R out-of-sample (profit factor 3.63, 13 trades); BTCUSD with the Asian-range variant delivered +4.03R (profit factor 2.10, 61.5% win rate). The two-sleeve basket produced +25.43R with a maximum drawdown of 4.38R and near-zero cross-correlation (−0.16). Three assets failed the in-sample floor and are reported as honest skips rather than re-tuned until they pass. After deflating against the 2,880-configuration search space, no configuration is individually significant — the correct institutional posture is paper-trading with kill criteria, not capital allocation." },
]));

// ------------------------------------------------ 2. Scope & method
body.push(h1("2. Scope, Data and Method"));
body.push(para([
  { t: "The audit covers the repository at commit c56f408e (ORB module, sweep results CSVs and research report) and the historical_parquet directory (15M, 1H, 5M and 4H timeframes; 151 symbols per timeframe). Five target assets spanning four asset classes were analysed in depth: NAS100.p and SP500.p (US equity indices), GER40.p (European index), BTCUSD.pi (crypto) and XAUUSD.pi (precious metal). The actual parquet schema is timestamp, datetime, open, high, low, close, tick_volume, spread, real_volume. Two facts about this schema matter for everything downstream: there is no digits column (contrary to the audit brief's stated schema), and the volume column is tick_volume, not volume." },
]));
body.push(caption("Table 1: Data inventory for the five audited targets (recomputed from parquet)"));
body.push(dataTable(
  ["Symbol", "TF", "Bars", "Digits*", "Window", "Spread (bps, median)"],
  [
    ["NAS100.p", "15M", "5,000", "2", "2026-09-07 to 2026-11-20", "0.40"],
    ["SP500.p", "15M", "5,000", "2", "2026-09-07 to 2026-11-20", "0.44"],
    ["GER40.p", "15M", "5,000", "2", "2026-09-07 to 2026-11-20", "0.32"],
    ["BTCUSD.pi", "15M", "5,000", "2", "2026-10-01 to 2026-11-22", "2.2"],
    ["XAUUSD.pi", "15M", "5,000", "2", "2026-09-07 to 2026-11-20", "0.19"],
    ["(all)", "4H", "5,000", "2", "2023-08 to 2026-11", "0.17-3.9"],
  ],
  [16, 8, 10, 10, 34, 22], { boldFirst: true }
));
body.push(para([
  { t: "*Digits inferred from full-array price precision (atol-only scan). The 15M window gives roughly 55 trading days per asset; a chronological 70/30 split therefore yields about 38 in-sample and 17 out-of-sample days. This is a structural power limitation of the dataset that no engine design can overcome, and it shapes every conclusion in this report. BTCUSD's spread column (median 1,500 points at two decimals = $15 on an ~$80,000 price, 2.2 bps) is realistic for a CFD feed; the FX pairs' stored spread of one point (0.1 pip) is not, as Section 3.3 details." },
]));

// ------------------------------------------------ 3. Audit findings
body.push(h1("3. Independent Audit Findings"));

body.push(h2("3.1 Selection on Out-of-Sample and the Multiple-Testing Haircut"));
body.push(para([
  { t: "The single most consequential finding is that the baseline's 'validated' setups are selected on the test set. In " },
  { t: "orb_sweep.py", i: true },
  { t: ", the qualification filter is (is_pf ≥ 1.25) AND (oos_pf ≥ 1.20) AND (oos_n ≥ 8) AND (oos_r > 0), and rows are then sorted by oos_r. Two of the four filter conditions are out-of-sample variables. Out of 1,060 single-timeframe configurations, exactly 19 pass; the 'out-of-sample' columns of the qualified CSV are therefore an upper tail selected on themselves, not a hold-out evaluation. The multi-timeframe sweep (1,500 configurations, 88 qualified) repeats the pattern." },
]));
body.push(para([
  { t: "We quantified what this selection is worth under a no-edge null. Using the SP500 champion's own per-trade R distribution re-centred to zero mean, we bootstrapped 20 configurations of 10 out-of-sample trades each and recorded the maximum configuration sum over 4,000 draws: the expected maximum is +57.7R (90th percentile +85.6R). The observed best configuration, +48.2R, sits inside this band — no configuration-specific skill is required to explain it. Separately, the champion portfolio's reported Sharpe ratio of 0.196 falls below the expected noise floor for 2,560 trials (0.267), giving a deflated probability of effectively zero. The advertised 'Student's t = 2.91, p < 0.0001' fails three ways at once: the t-statistic is computed on 221 pooled trades that include in-sample trades; the champions were chosen from the out-of-sample-selected qualified list; and the p-value itself is arithmetically wrong (two-sided p for t = 2.91 with 220 degrees of freedom is approximately 0.004 — the '< 0.0001' string is hard-coded in the printing statement of orb_champion_portfolio.py)." },
]));

body.push(h2("3.2 Regime Reality: November 2026 Risk-Off, Not a Bull Flattery"));
body.push(para([
  { t: "The out-of-sample windows of the headline configurations begin 2026-10-29 (indices and gold) and 2026-11-07 (BTC, whose 15M history starts 2026-10-01). These dates fall inside a broad, synchronised drawdown: over November on 4H closes, SP500 lost 8.7%, NAS100 11.8%, BTCUSD 21.8% and XAUUSD 9.4%. The baseline's profitable out-of-sample rows are dominated by short breakouts riding this single event. Two implications follow. First, the usual critique — 'ORB longs were flattered by a rally' — does not apply to this dataset; the correct critique is that the profits are one-regime, one-direction and 10-17 trades deep. Second, the champion ensemble's diversification claim is weaker than advertised because all six sleeves share exposure to the same equity crash; the sleeves are not independent regimes but one regime sampled six ways." },
]));
body.push(...figure(path.join(ASSETS_DIR, "fig_regime.png"), 500, 250,
  "Figure 1: Monthly 4H close-to-close returns, July-November 2026. Every audited class rolls over into the November risk-off window where all out-of-sample evaluations live."));

body.push(h2("3.3 The Cost Model: Missing Digits, 1000x FX Spreads, an Unrealistic Stored Spread"));
body.push(para([
  { t: "The engines read digits from a parquet column that does not exist, falling back to a hard-coded 2 for every instrument. The consequences are asset-specific and asymmetric, which is why this defect is more interesting than a uniform bug. For the indices, BTC and gold (all genuinely two-decimal instruments), the fallback is accidentally correct. For EURUSD and GBPUSD (five decimals) the baseline charges a 1,000x-too-large spread — 22.2x and 18.2x the entire median 15-minute bar range respectively — which makes the pairs untradeable in simulation and produces the report's 'structural forex failure' (-11,469R claimed). For USDJPY and GBPJPY (three decimals) the error is 10x." },
]));
body.push(caption("Table 2: Spread-cost defect map (baseline digits=2 fallback vs inferred true digits)"));
body.push(dataTable(
  ["Instrument", "True digits", "Baseline charge", "Correct charge", "Overcharge", "Wrong charge vs median bar range"],
  [
    ["EURUSD.pi", "5", "0.01000", "0.00001", "1,000x", "22.2x"],
    ["GBPUSD.pi", "5", "0.01000", "0.00001", "1,000x", "18.2x"],
    ["USDJPY.pi", "3", "0.01000", "0.00100", "10x", "0.1x"],
    ["GBPJPY.pi", "3", "0.01000", "0.00100", "10x", "0.1x"],
    ["SP500/NAS100/GER40/BTC/XAU", "2", "0.01 pt", "0.01 pt", "1x (accidental)", "ok"],
  ],
  [22, 12, 16, 16, 14, 20], { boldFirst: true }
));
body.push(para([
  { t: "However — and this correction cuts against every party — the stored FX spread of one point (0.1 pip) is itself not a live-tradable cost. Retail and prop EURUSD spreads average roughly 0.6–1.0 pip. The baseline's accidental one-pip charge is closer to live reality than the 'corrected' 0.1-pip charge. The honest conclusion is that the forex failure verdict is unresolvable from this data: the stored spread is roughly 100x too tight for live trading on the majors, the baseline charge is plausibly realistic, and the -11,469R headline is in any case an aggregate across 260 configurations rather than the P&L of any tradable strategy. What is unambiguous is the engineering defect: a cost model must read its tick size from the instrument, not from a fallback constant. Separately, the stored spread column shows no widening at the opening window (0.88-1.00x of the rest-of-day median), so real open-auction spread expansion is absent from the data and must be added as a stress assumption." },
]));

body.push(h2("3.4 Causality Verification of the Multi-Timeframe Merge"));
body.push(para([
  { t: "The audit brief asked whether the backward merge_asof with a one-bar shift in orb_mtf_engine.py lets future 4H bars leak into 15M decisions. We verified the bar-timestamp convention empirically: aggregating 15M bars forward from each 4H timestamp reproduces the 4H bar's high and low exactly in 326 of 326 overlapping SP500 bars (311 of 312 for BTC), proving the 4H timestamps are bar-open stamps. With open-stamped bars, a 4H bar opening at time T contains information up to T+4h; the shift(1) + backward merge construction makes a 15M bar at time t see only the values of the 4H bar that opened at or before t−4h — i.e. a bar that has fully closed. The construction is causal. We flag two residual edges: the htf_ema200 series uses shift(1).bfill(), which lets the first 4H bar of the dataset see the second bar's EMA (a one-bar lookahead at the dataset's left edge; harmless for 15M data starting in 2026 against 4H data from 2023, but a latent defect for full-history runs), and htf_bullish/bearish fill the first row with False, which is conservative rather than leaking." },
]));

body.push(h2("3.5 Session Anchor: Broker Open at 14:00 UTC versus NYSE Cash Open at 14:30"));
body.push(para([
  { t: "Median 15-minute tick volume on SP500 steps from 8,343 (13:45) to 11,804 (14:00) and median bar range from 5.57 to 7.48 points: this broker's CFD session opens at 14:00 UTC (09:00 New York), thirty minutes before the NYSE cash open at 14:30 UTC (winter). The baseline's session_start_hour=14 therefore builds opening ranges on the broker-session open — internally consistent with the data's own liquidity structure, but a different market event than the classic 09:30 ET ORB that most ORB literature means. This distinction is not cosmetic: in our variant study the 14:30 anchor beat the 14:00 anchor on NAS100 in-sample, while the 14:00 anchor matched the broker data's liquidity step. Both anchors belong in any serious variant sweep, and the engine treats the anchor as a first-class parameter." },
]));

body.push(h2("3.6 Why the Midpoint Stop Works and When It Gets Hunted"));
body.push(para([
  { t: "The baseline report attributes the midpoint stop's dominance to a structural insight; the mechanism is largely arithmetic. Halving the stop distance halves the risk unit, so every price move is booked as roughly twice the R-multiple — the profit factor, being computed in R, scales with it. Against this arithmetic benefit stands a real cost: with a midpoint stop, the trade dies the moment price revisits the opening-range mid. We measured this directly on 15M sessions with a 30-minute opening range: after a buffered breakout, price returns to the range mid before reaching a +2R target in 60–92% of breakout sessions across all five assets and every opening-range-volatility-ratio (ORVR) bucket. The midpoint configuration is therefore a lottery on runner sessions — the minority where price runs immediately — whose wins are then amplified ~2x by the smaller denominator. The baseline's max_orvr ≤ 1.2 filter (skipping over-expanded opens) is doing more real work than the stop placement itself. For live sizing, the practical consequence is a low win rate (~35–40%) with a fat right tail: position sizing must survive long losing streaks, not respectable averages." },
]));
body.push(caption("Table 3: Stop-hunt statistics - share of breakout sessions where price revisits the OR midpoint BEFORE reaching +2R"));
body.push(dataTable(
  ["Asset", "ORVR <= 0.8", "ORVR 0.8-1.2", "ORVR > 1.2", "Sessions (total)"],
  [
    ["SP500.p", "0% (n=1)", "76% (n=17)", "68% (n=34)", "52"],
    ["NAS100.p", "0% (n=1)", "92% (n=12)", "74% (n=38)", "51"],
    ["GER40.p", "50% (n=2)", "60% (n=20)", "78% (n=27)", "49"],
    ["BTCUSD.pi", "100% (n=2)", "76% (n=21)", "68% (n=22)", "45"],
    ["XAUUSD.pi", "75% (n=4)", "86% (n=14)", "70% (n=33)", "51"],
  ],
  [18, 18, 20, 20, 24], { boldFirst: true }
));

body.push(h2("3.7 Where the Research Report Disagrees With Its Own Artifacts"));
body.push(caption("Table 4: Report claims versus artifact recompute"));
body.push(dataTable(
  ["Claim in research report", "Artifact recompute (this audit)", "Verdict"],
  [
    ["126 positive OOS setups of 200 tested, avg OOS PF 3.45, +876.94R (indices 15M)",
     "320 indices-15M rows; 172 positive (53.8%); avg OOS PF 2.49; total +248.23R",
     "Not reproducible from shipped CSVs"],
    ["Forex majors aggregate -11,469R to -13,883R",
     "260 forex rows sum to -25,353.7R OOS",
     "Worse than claimed - and cost-model contaminated"],
    ["Champion portfolio: 221 trades, +168.40R, PF 2.31, WR 39.8%, DD 24.39R, t 2.91",
     "Exact match on all six figures",
     "Reproducible - but pools IS+OOS trades of OOS-selected champions"],
    ["p < 0.0001 for the portfolio t-statistic",
     "Two-sided p ~ 0.004 at df=220; string hard-coded in the print statement",
     "Off by ~25x"],
    ["OOS = hold-out evaluation",
     "Qualification filter uses oos_pf/oos_n/oos_r directly",
     "Selection on the test set"],
  ],
  [34, 42, 24], { boldFirst: false }
));
body.push(para([
  { t: "One credit must be recorded alongside the criticism: the baseline is deterministic. Re-running the shipped engine on the shipped data reproduces the qualified CSV to the second decimal, which is rare in this genre of research and made the audit substantially more reliable. The defects are in statistics and assumptions, not in reproducibility." },
]));

// ------------------------------------------------ 4. Companion AI audit review
body.push(h1("4. Review of the Companion AI Audit (ORB_AUDIT_EVOLVED.md)"));
body.push(para([
  { t: "A second AI's audit of the same baseline was provided for independent review. We verified each of its load-bearing claims against the repository before accepting or rejecting it. Overall assessment: its four defect findings are substantially correct and its honest self-reporting is exemplary; its evolved-engine headline numbers are, however, selected on out-of-sample performance in exactly the way its own audit criticises, and its claimed reproducible artifacts are not present in the shipped repository." },
]));
body.push(h2("4.1 Claims Verified Independently"));
body.push(caption("Table 5: Companion audit claims versus this audit's verification"));
body.push(dataTable(
  ["Companion claim", "This audit's verification", "Agreement"],
  [
    ["Parquet has no digits column; baseline hard-codes 2",
     "Schema checked on every file: column absent; fallback confirmed in code",
     "Confirmed"],
    ["EURUSD/GBPUSD spread overcharged 1,000x (22x median bar range)",
     "Reproduced exactly: 1,000x ratio; 22.2x / 18.2x bar-range multiple",
     "Confirmed"],
    ["OOS window is a broad risk-off drawdown, not a bull market",
     "Monthly 4H returns: Nov 2026 negative across all four audited classes",
     "Confirmed (premise of the audit brief inverted)"],
    ["merge_asof shift(1) is causal",
     "Forward-4h aggregation reproduces 326/326 SP500 4H bars (open-stamp proof)",
     "Confirmed, with stronger evidence"],
    ["Champion t=2.91 does not survive the search haircut; p misreported",
     "DSR probability ~0 against 2,560 trials; p~0.004 not <0.0001",
     "Confirmed"],
    ["Sweep counts: 1,060 single-TF rows, 19 qualified, 88 MTF qualified",
     "Recomputed: 1,060 / 19 / 88",
     "Confirmed"],
    ["Their own compression filter unit bug (prev-day range / ATR-H1 ~ 11x)",
     "Correct diagnosis: daily range vs 1-hour ATR is a unit mismatch; daily normalisation required",
     "Confirmed (and pre-empted in our engine)"],
  ],
  [34, 46, 20], { boldFirst: false }
));
body.push(h2("4.2 Weak Points and Overclaims"));
body.push(para([
  { t: "Four weaknesses materially reduce the companion audit's evidentiary weight. First, its comparative table selects the evolved configuration per asset as 'the A5/A6 stage with the best OOS R' — a post-hoc out-of-sample selection, the identical methodological sin its own Section 1 indicts. Second, its 'monotonic improvement' narrative holds on NAS100 but is contradicted by its own tables on SP500 (every added filter worsens results) and BTC (the full stack flips the OOS result negative). Third, several of its headline configurations rest on 3–6 out-of-sample trades — smaller samples than the baseline results it dismisses at 10–13 trades. Fourth, its 'corrected-cost' experiment charges the stored 0.1-pip spread as if it were live-realistic; as Section 3.3 shows, that figure is roughly 100x too tight, so its finding that GBPUSD turns positive (+13.55R) under corrected costs is as assumption-laden as the baseline's negative verdict — bracketing reality from the opposite side rather than resolving it. We also note its BTC digits inference of 1 is wrong (mixed .25-close precision gives 2; a $150 spread on an $80k BTC would be implausible), although its final 'BTC costs correct' conclusion happens to stand because the baseline's fallback equals the true value." },
]));
body.push(h2("4.3 Artifacts Not Reproducible"));
body.push(para([
  { t: "The companion report references ORB/orb_evolved.py and ORB/results/evolved_ablation.csv as files in the repository and gives reproduction commands against them. Neither file exists in the shipped repository (the ORB directory contains only the baseline module, sweeps, report and results CSVs). Its ablation table therefore cannot be reproduced or falsified from the artifacts, and its evolved-engine numbers should be read as reported results from an unavailable implementation rather than as verified findings. The audit trail it publishes is internally consistent and its honesty about its own bugs (the compression unit mismatch, the deflated-t sigma algebra) is credible, but the absence of shipped code places its quantitative results outside the verification perimeter of this report." },
]));

// ------------------------------------------------ 5. Evolved engine
body.push(h1("5. The Evolved Engine (ORB/orb_evolved.py)"));
body.push(h2("5.1 Design Mapping: Audit Finding to Engine Feature"));
body.push(para([
  { t: "The evolved engine is a single self-contained module (no network, pandas/numpy only) whose every design decision traces to a verified audit finding. It runs in five modes: --selftest (per-asset sanity, causal-entry assertions, digit inference), --sweep (pre-registered in-sample search), --basket (pooled frozen portfolios), --compare (baseline-versus-evolved table) and --validate1h (long-history robustness on 1H bars)." },
]));
body.push(caption("Table 6: Audit findings implemented in the evolved engine"));
body.push(dataTable(
  ["Audit finding", "Engine feature"],
  [
    ["No digits column; baseline fallback wrong on FX",
     "Full-array precision inference with per-class sanity bands; refuse to trade if median spread exceeds 50% of median bar range"],
    ["Baseline charges ~1x spread per round trip",
     "Strict protocol: spread charged on every fill (entry AND exit); stop-distance floor of 3x spread"],
    ["70/30 split applied to the trade list",
     "Chronological split by session days"],
    ["Qualification filter selects on OOS variables",
     "In-sample-only scoring (n >= 12, IS PF >= 1.2, IS t >= 1.0); parameters frozen; OOS evaluated exactly once"],
    ["p-value and significance overstatement",
     "Deflated t reported for every result against the true trial count (2,880 configurations)"],
    ["bfill() latent lookahead in the 4H merge",
     "Causal 4H gate: 15M bar at t sees the last 4H bar with open_time <= t-4h (searchsorted, no bfill)"],
    ["Single opening-range definition",
     "Six variants: OR30/OR60/OR120 after anchor, prior-day high/low, overnight range, Asian range (00:00-08:00)"],
    ["Stored spread lacks open-auction widening",
     "Configurable open-window spread multiplier for stress runs (default off for protocol parity)"],
  ],
  [38, 62], { boldFirst: false }
));
body.push(h2("5.2 The Five Brief Innovations"));
body.push(para([
  { t: "All five innovations from the mission brief are implemented and config-gated. Session VWAP with volume-weighted sigma bands from the anchor supports Rule A (longs only above a rising VWAP, shorts only below a falling one) and Rule B (booking half the position at the +2-sigma band). The Initial Balance classifier labels the first hour as Open-Drive, Open-Test-Drive or Open-Rejection-Reverse; on rejection-reverse days the engine disables standard breakouts and fades the failed move toward the IB midpoint with a stop beyond the IB extreme. Volatility compression uses a correctly normalised ratio — previous-day range divided by the mean of the trailing ten daily ranges — avoiding the unit error the companion audit documented, with an NR4 flag that waives the compression gate on narrowest-range-of-four days. Breakout confirmation requires volume at 1.5x its 20-bar average or a body of at least 65% of the bar's range. The Chandelier exit trails HH(22) − 2.5·ATR14 on closed bars with intrabar fills at the resting-stop level, replacing the rigid end-of-day exit where selected." },
]));
body.push(h2("5.3 Protocol Compliance"));
body.push(para([
  { t: "Every configuration in the sweep obeys the strict protocol: signals evaluate on closed bar i and execute at the open of bar i+1 (asserted in the selftest by checking entry_time > signal_time on every trade); the spread is charged on both fills; the 70/30 split is chronological over session days (38/17 for the indices and gold, 37/16 for BTC); and the reported metrics per split are trade count N, net R, profit factor, win rate, average R, maximum drawdown in R and the Student t-statistic. The selftest passes on all five assets with digits correctly inferred (all two-decimal; BTC spread $15, gold $0.08, indices $0.34-1.22 per 0.01 point)." },
]));

// ------------------------------------------------ 6. Results
body.push(h1("6. Results Under the Pre-Registered Protocol"));
body.push(h2("6.1 Frozen Configurations and Out-of-Sample Performance"));
body.push(para([
  { t: "The sweep evaluated 704 configurations per US asset (384 for GER40 and BTC, which have a single anchor), scored exclusively on the 38-day in-sample window, and froze the highest-scoring configuration per asset subject to the floor (n ≥ 12, IS PF ≥ 1.2, IS t ≥ 1.0). Two of five assets cleared the floor. The three that failed — SP500, GER40 and XAUUSD — are reported as honest skips: no parameter combination in 2,880 tried produced an in-sample profit factor above 1.2 with at least twelve trades. This is itself a finding: under correctly inferred costs and a day-based split, most of the baseline's headline assets show no in-sample-selectable ORB edge over this window." },
]));
body.push(caption("Table 7: Frozen configurations - in-sample and out-of-sample performance (spread charged on both fills)"));
body.push(dataTable(
  ["Asset", "Frozen setup", "IS: n / R / PF", "OOS n", "OOS R", "OOS PF", "OOS WR", "OOS DD", "OOS t", "Defl. t"],
  [
    ["NAS100.p", "OR30@14:30, 4H EMA200, mid stop, EOD", "30 / +17.33 / 2.03", "13", "+21.40", "3.63", "38.5%", "3.08R", "1.31", "-2.31"],
    ["SP500.p", "no IS-qualified config", "-", "-", "-", "-", "-", "-", "-", "-"],
    ["GER40.p", "no IS-qualified config", "-", "-", "-", "-", "-", "-", "-", "-"],
    ["BTCUSD.pi", "ASIAN@14:00, mid stop, EOD", "29 / +8.76 / 1.91", "13", "+4.03", "2.10", "61.5%", "1.05R", "1.07", "-2.38"],
    ["XAUUSD.pi", "no IS-qualified config", "-", "-", "-", "-", "-", "-", "-", "-"],
  ],
  [12, 24, 16, 6, 8, 8, 8, 8, 7, 9], { boldFirst: true }
));
body.push(h2("6.2 Alpha Basket: Pooled Out-of-Sample Statistics"));
body.push(para([
  { t: "Pooling the two frozen sleeves gives the pre-registered basket: 26 out-of-sample trades, +25.43R, profit factor 3.16, win rate 50.0%, average R per trade 0.978, maximum drawdown 4.38R, t-statistic 1.52. The daily-R correlation between the sleeves is −0.16 — genuinely diversifying, unlike the baseline's six sleeves that all lean on the same equity crash. Deflating against the full 2,880-configuration search gives a deflated t of −2.48: the basket's headline numbers are promising but not statistically established, and that is the sentence that must accompany any allocation discussion." },
]));
body.push(...figure(path.join(ASSETS_DIR, "fig_basket_equity.png"), 500, 260,
  "Figure 2: Pre-registered basket equity over the out-of-sample window (frozen configs, no re-fitting)."));
body.push(h2("6.3 Opening-Range Variant Scoreboard"));
body.push(para([
  { t: "The audit brief's central design question — which opening-range definition to trade — has an asset-specific answer, not a universal one. The scoreboard below aggregates the in-sample sweep by variant (all filter combinations pooled). Fixed-window ranges are not dominant: NAS100's winner is the 30-minute range at the NYSE cash-open anchor, BTC's winner is the Asian session range broken at the New York liquidity window, and the prior-day and overnight variants never win a freeze. Median in-sample profit factors are below 1.0 almost everywhere — the variant library matters because it lets each asset reveal which range it actually trends out of, not because any variant is generally superior." },
]));
body.push(caption("Table 8: In-sample scoreboard by opening-range variant (all filter combinations pooled; best = highest-scoring frozen candidate)"));
body.push(dataTable(
  ["Asset", "Best variant (frozen)", "Best IS PF", "Configs IS-PF>1", "Runner-up variant", "Verdict"],
  [
    ["NAS100.p", "OR30 @ 14:30 + 4H trend", "2.83 (OR60@14:00 best raw, n-floor excluded)", "57 of 128 (OR60)", "OR120 / ON", "15m-window ORs dominate"],
    ["BTCUSD.pi", "ASIAN range broken @ 14:00", "1.94", "7 of 64 (ASIAN)", "ON (1.72)", "Session-range variants beat fixed windows"],
    ["SP500.p", "none qualified", "2.15 (OR60 best, below floor)", "11 of 128 (OR60)", "-", "No selectable edge"],
    ["GER40.p", "none qualified", "1.45 (OR30 best, below floor)", "9 of 64 (OR30)", "PD_HL (2.23, small n)", "No selectable edge"],
    ["XAUUSD.pi", "none qualified", "1.20 (OR30, marginal)", "1 of 128", "-", "No selectable edge"],
  ],
  [13, 22, 20, 17, 14, 14], { boldFirst: true }
));
body.push(h2("6.4 Long-History Validation on 1H Bars"));
body.push(para([
  { t: "The 15M out-of-sample windows cover 16-17 days — too short to trust on their own, whatever they show. As a regime-robustness check we re-ran the frozen rules unchanged on 1H bars (four times coarser, ten months of history, January-November 2026), with anchors snapped to the hourly grid and no re-fitting. Both sleeves degrade to near-nothing: NAS100 in-sample profit factor 0.93 over 153 days (OOS 1.18 over 66 days, t = 0.40); BTC in-sample 1.19 over 146 days (OOS 1.09 over 63 days, t = 0.22). The honest reading is that the 15M out-of-sample profits are window-specific and bar-granularity-specific; they do not generalise to the longer history at coarser granularity. Any deployment decision must treat the two frozen configurations as hypotheses to be paper-traded, not as validated systems." },
]));
body.push(caption("Table 9: Frozen rules on 1H bars, 70/30 by days, same costs"));
body.push(dataTable(
  ["Asset", "Window", "IS n / PF / t", "OOS n / PF / t", "OOS DD"],
  [
    ["NAS100.p", "2026-01-20 to 2026-11-20 (IS 153d / OOS 66d)", "100 / 0.93 / -0.29", "43 / 1.18 / 0.40", "7.12R"],
    ["BTCUSD.pi", "2026-04-28 to 2026-11-22 (IS 146d / OOS 63d)", "113 / 1.19 / 0.54", "47 / 1.09 / 0.22", "8.91R"],
  ],
  [12, 34, 22, 20, 12], { boldFirst: true }
));
body.push(h2("6.5 Comparative Performance versus Baseline"));
body.push(para([
  { t: "The mission-required comparison follows, pairing each asset's published baseline champion (from the shipped qualified CSVs, as advertised in the research report) with the evolved engine's frozen configuration. The comparison must be read with its asymmetries in mind: the baseline numbers were selected on out-of-sample performance and costed with one fill's spread (and, on the indices, accidentally-correct tick sizes), while the evolved numbers were frozen in-sample and costed on both fills. Where the evolved engine reports no qualified configuration, that honesty is itself the comparative result: under in-sample selection the baseline's SP500, GER40 and gold champions do not re-qualify at all." },
]));
body.push(caption("Table 10: Comparative performance (baseline as published; evolved under pre-registered strict protocol)"));
body.push(dataTable(
  ["Asset", "Setup", "Baseline OOS PF", "Evolved OOS PF", "Baseline OOS R", "Evolved OOS R", "Max DD", "t-stat"],
  [
    ["NAS100.p", "15M ORB, 4H trend gate (both)", "3.03", "3.63", "+16.46", "+21.40", "3.08R", "1.31"],
    ["SP500.p", "15m OR, midpoint, EOD", "7.45", "not qualified (IS)", "+48.23", "-", "-", "-"],
    ["GER40.p", "15m OR, opposite, 3R", "4.73", "not qualified (IS)", "+15.09", "-", "-", "-"],
    ["BTCUSD.pi", "Asian range vs 1H-OR+4H trend", "3.43", "2.10", "+12.73", "+4.03", "1.05R", "1.07"],
    ["XAUUSD.pi", "Arch1 4H strong trend", "3.44", "not qualified (IS)", "+20.26", "-", "-", "-"],
    ["Basket", "two frozen sleeves", "-", "3.16", "-", "+25.43", "4.38R", "1.52"],
  ],
  [12, 21, 12, 13, 12, 13, 9, 8], { boldFirst: true }
));
body.push(para([
  { t: "The two rows where both engines trade tell a consistent story. On NAS100 the evolved configuration beats the published baseline on every metric while being derived without touching the test set — the one genuinely encouraging cell in the table. On BTC the evolved result is more modest (+4.03R versus +12.73R) but comes with a 61.5% win rate and a 1.05R drawdown, a far better risk profile than the opposite-stop 3R configuration it replaces. Everywhere else the baseline's advertised edge does not survive in-sample selection under corrected accounting, which is the quantitative content of this audit." },
]));

// ------------------------------------------------ 7. Rulebook
body.push(h1("7. Institutional Execution Rulebook (MT5 and Delta Exchange)"));
body.push(h2("7.1 Deployment Decision and Sizing"));
body.push(para([
  { t: "Do not allocate the baseline champion portfolio on this evidence: its significance claim fails a multiple-testing correction, its trades pool in-sample and out-of-sample data, and its cost model contains an instrument-dependent defect. The only configurations eligible for capital are the two frozen sleeves that survived in-sample pre-registration — and even they carry negative deflated t-statistics, so the correct posture is a paper-trading or minimum-size proving period with pre-committed kill criteria, not a production allocation. Sizing: 0.25–0.5% of equity risked per trade (1R), one position per sleeve, maximum two concurrent positions. The basket's measured 4.38R drawdown implies roughly a 1.1–2.2% equity drawdown at that sizing — tolerable — but the 1H validation shows the same rules can bleed 7–9R over longer windows, so the kill criteria in Section 7.4 are not optional." },
]));
body.push(h2("7.2 NAS100 Sleeve Rulebook (MT5 CFD)"));
body.push(caption("Table 11: NAS100 frozen configuration - execution parameters"));
body.push(dataTable(
  ["Parameter", "Value", "Execution note"],
  [
    ["Opening range", "14:30-15:00 UTC (first 30 min from NYSE cash open)", "Do NOT use the broker 14:00 session open; the 14:30 anchor outperformed in-sample"],
    ["Trend gate", "4H close > 4H EMA200 for longs; < for shorts", "Compute on closed 4H bars only (the last bar that closed at or before the 15M signal bar)"],
    ["Signal", "15M bar closes beyond OR high + 0.05 x ATR14 (or low -), in bar direction", "Closed-bar only; never enter on a wick"],
    ["Entry", "Market order at the open of the next 15M bar", "Accept ~1 bar of latency; do not chase"],
    ["Stop", "OR midpoint; skip the trade if stop distance < 3 x live spread", "Reject sessions whose opening range is too narrow to pay the spread"],
    ["Exit", "Close at 22:30 UTC (session end), 1 trade/day max", "No breakeven lock, no trailing for this configuration"],
    ["Cost check at 14:30", "Skip the session if live spread > 3x the stored median (0.40 bps ~ 1.2 index points)", "The data's stored spread does not widen at the open; live spreads do"],
  ],
  [20, 38, 42], { boldFirst: true }
));
body.push(h2("7.3 BTC Sleeve Rulebook (Delta Exchange perpetual or MT5 CFD)"));
body.push(caption("Table 12: BTCUSD frozen configuration - execution parameters"));
body.push(dataTable(
  ["Parameter", "Value", "Execution note"],
  [
    ["Opening range", "Asian session 00:00-08:00 UTC high/low", "The frozen winner; treat 08:00-14:00 as a no-trade window"],
    ["Signal window", "Breakout signals from 14:00 UTC (New York liquidity) until 20:00", "Matches the data's 14:00 volume step"],
    ["Signal", "15M close beyond Asian high/low + 0.05 x ATR14, in bar direction", "Both directions allowed (no trend gate won in-sample)"],
    ["Stop", "Asian-range midpoint; floor 3x spread", "BTC spread median $15; skip if live spread exceeds ~$45"],
    ["Exit", "EOD close 22:00 UTC; 1 trade/day max", "Chandelier trail lost to EOD in-sample; keep it simple"],
    ["Platform notes", "Delta Exchange: use post-only entries where possible, mind funding windows and the ~2.2 bps median spread; MT5 CFD: verify tick size is $0.01", "Charge yourself both fills in the journal, as the engine does"],
  ],
  [20, 38, 42], { boldFirst: true }
));
body.push(h2("7.4 Risk Budget, Kill Criteria and Review Cadence"));
body.push(para([
  { t: "Pre-register the evaluation before the first live trade: record expected per-sleeve trade counts (~13 per 17 trading days for NAS100; ~13 for BTC), expected win rates (38.5% and 61.5%), and the basket drawdown expectation (4.38R). Kill criteria: halt a sleeve if its rolling 40-trade profit factor falls below 1.0, or if cumulative R falls below −8R; halt the basket if the pooled 40-trade profit factor falls below 1.2. Review cadence: weekly journal reconciliation against the engine's trade log (both-fill spread accounting, entry at next-bar open), and a full re-validation whenever the broker's contract specifications change (tick size, session times, spread structure). Before any size increase, re-derive both sleeves on at least two years of 15M history — the current 55-day window is the binding constraint on every conclusion in this report, and the 4H files already carry three years of daily structure to aggregate against." },
]));

// ------------------------------------------------ 8. Reproduction
body.push(h1("8. Reproduction and Artifacts"));
body.push(para([
  { t: "All figures in this report derive from code committed to the repository. The audit scripts (scripts/orb_audit.py, scripts/orb_audit2.py) recompute the data forensics, baseline verification, regime context, causality test and stop-hunt statistics. The evolved engine is ORB/orb_evolved.py; its results are written under ORB/results/ (evolved_frozen.json, evolved_oos_trades.csv, evolved_basket_trades.csv, evolved_basket_equity.csv, evolved_compare.csv, evolved_1h_validation.csv, evolved_sweep_rows_*.csv, evolved_variant_summary.csv). Reproduction commands:" },
]));
body.push(para([
  { t: "python3 ORB/orb_evolved.py --selftest      # engine sanity, digit inference, causal assertions", i: true },
], { after: 40 }));
body.push(para([
  { t: "python3 ORB/orb_evolved.py --sweep         # pre-registered IS-only search, freeze, single OOS pass", i: true },
], { after: 40 }));
body.push(para([
  { t: "python3 ORB/orb_evolved.py --basket        # pooled frozen-sleeve OOS statistics", i: true },
], { after: 40 }));
body.push(para([
  { t: "python3 ORB/orb_evolved.py --compare       # baseline vs evolved comparative table", i: true },
], { after: 40 }));
body.push(para([
  { t: "python3 ORB/orb_evolved.py --validate1h    # frozen rules on 1H bars (no re-fitting)", i: true },
]));
body.push(para([
  { t: "Two data caveats bind every result: the 15M history covers roughly 55 trading days per asset, and the stored spread column embeds no open-auction widening. Both are properties of the data, not of the engines; the first limits statistical power, the second means live spread behaviour must be monitored at execution time as specified in the rulebook." },
]));

// ------------------------------------------------ Appendix
body.push(h1("Appendix A. Frozen Configuration Definitions"));
body.push(para([
  { t: "Exact parameter sets as stored in ORB/results/evolved_frozen.json (engine defaults otherwise). NAS100.p: variant OR30; anchor 14:30; htf_trend=true (4H EMA200 causal gate); vwap_gate=false; vwap_scaleout=false; volume_confirm=false; orvr_max=null; sl_mode=midpoint; exit_mode=eod; be_lock=false; buffer_atr=0.05; spread_charge=both; stop_floor_spread=3.0; session_hours=8; signal_window_hours=6; max_trades_per_day=1. BTCUSD.pi: identical except variant=ASIAN, anchor=14:00, htf_trend=false. Trials per asset: 704 (NAS100), 384 (BTC); total across the five-asset sweep: 2,880. The three assets without a frozen configuration failed the floor with best in-sample profit factors of 2.15 (SP500, n below floor), 1.45 (GER40) and 1.20 (XAUUSD, marginal)." },
]));
body.push(para([
  { t: "Baseline champion definitions used in Table 10, as published: NAS100 — 1H opening range, 15M execution, 4H EMA200 gate, midpoint stop, EOD exit (OOS PF 3.03, +16.46R, 13 trades); SP500 — 15m OR, midpoint, EOD (OOS PF 7.45, +48.23R, 10 trades); GER40 — 15m OR, opposite stop, fixed 3R (OOS PF 4.73, +15.09R, 11 trades); BTCUSD — 1H OR, 15M execution, 4H trend, opposite stop, fixed 3R (OOS PF 3.43, +12.73R, 12 trades); XAUUSD — Arch1 4H strong trend (OOS PF 3.44, +20.26R). These rows are quoted as advertised; Section 3.1 documents why they should not be read as hold-out results." },
]));

// ================================================================ ASSEMBLY
const coverConfig = {
  title: "ORB Engine Quantitative Audit and Evolved Design",
  subtitle: "Independent red-team review of the multi-asset ORB research, and a pre-registered rebuild",
  englishLabel: "QUANTITATIVE AUDIT",
  metaLines: [
    "Repository: karan1399/Trading-Strategies (ORB module, commit c56f408e)",
    "Data: historical_parquet - 15M/1H/5M/4H, 151 symbols, Jan 2023 - Nov 2026",
    "Scope: NAS100 / SP500 / GER40 / BTCUSD / XAUUSD - four asset classes",
    "Date: 2026-11-24",
  ],
  footerLeft: "Senior Quant PM / Algo Engineering",
  footerRight: "ORB_AUDIT_EVOLVED_II",
  palette: P,
};

const pgSize = { width: 11906, height: 16838 };
const pgMargin = { top: 1440, bottom: 1440, left: 1701, right: 1417 };

function pageFooter() {
  return new Footer({
    children: [new Paragraph({
      alignment: AlignmentType.CENTER,
      children: [new TextRun({ children: [PageNumber.CURRENT], size: 18, color: "808080", font: FONT })],
    })],
  });
}
function pageHeader() {
  return new Header({
    children: [new Paragraph({
      alignment: AlignmentType.CENTER,
      border: { bottom: { style: BorderStyle.SINGLE, size: 2, color: P.table.innerLine, space: 4 } },
      children: [new TextRun({ text: "ORB Engine Quantitative Audit and Evolved Design", size: 18, color: "808080", font: FONT })],
    })],
  });
}

const doc = new Document({
  styles: {
    default: {
      document: {
        run: { font: FONT, size: 24, color: BODY },
        paragraph: { spacing: { line: 312 } },
      },
      heading1: {
        run: { font: FONT, size: 32, bold: true, color: BODY },
        paragraph: { spacing: { before: 360, after: 160, line: 312 }, outlineLevel: 0 },
      },
      heading2: {
        run: { font: FONT, size: 30, bold: true, color: BODY },
        paragraph: { spacing: { before: 240, after: 120, line: 312 }, outlineLevel: 1 },
      },
      heading3: {
        run: { font: FONT, size: 28, bold: true, color: BODY },
        paragraph: { spacing: { before: 200, after: 100, line: 312 }, outlineLevel: 2 },
      },
    },
  },
  sections: [
    { // Section 1: cover - margin 0, no footer/header
      properties: { page: { size: pgSize, margin: { top: 0, bottom: 0, left: 0, right: 0 } } },
      children: buildCoverR1(coverConfig),
    },
    { // Section 2: TOC - Roman numerals, no visible footer needed
      properties: {
        type: SectionType.NEXT_PAGE,
        page: { size: pgSize, margin: pgMargin, pageNumbers: { start: 1, formatType: NumberFormat.UPPER_ROMAN } },
      },
      children: [
        new Paragraph({
          alignment: AlignmentType.CENTER, spacing: { before: 480, after: 360 },
          children: [new TextRun({ text: "Table of Contents", bold: true, size: 32, font: FONT, color: BODY })],
        }),
        new TableOfContents("Table of Contents", { hyperlink: true, headingStyleRange: "1-2" }),
        new Paragraph({
          spacing: { before: 200 },
          children: [new TextRun({
            text: "Note: This Table of Contents is generated via field codes. To ensure page number accuracy after editing, please right-click the TOC and select \"Update Field.\"",
            italics: true, size: 18, color: "888888", font: FONT,
          })],
        }),
        // No trailing PageBreak: the body section below is SectionType.NEXT_PAGE
        // (double break would create a blank page).
      ],
    },
    { // Section 3: body - Arabic from 1
      properties: {
        type: SectionType.NEXT_PAGE,
        page: { size: pgSize, margin: pgMargin, pageNumbers: { start: 1, formatType: NumberFormat.DECIMAL } },
      },
      headers: { default: pageHeader() },
      footers: { default: pageFooter() },
      children: body,
    },
  ],
});

Packer.toBuffer(doc).then(buf => {
  const out = "/home/z/my-project/download/ORB_Quantitative_Audit_and_Evolved_Design.docx";
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, buf);
  console.log("WROTE", out, buf.length, "bytes");
});




