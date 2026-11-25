// generate_report.js — Volatility Compression audit report (English, R1 cover, IG-1 palette)
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  ImageRun, PageBreak, Header, Footer, PageNumber, NumberFormat,
  AlignmentType, HeadingLevel, WidthType, BorderStyle, ShadingType,
  SectionType, TableOfContents, TableLayoutType,
} = require("docx");
const fs = require("fs");
const path = require("path");

const P = {
  bg: "1A1A1A", accent: "C9A84C",
  cover: { titleColor: "FFFFFF", subtitleColor: "B0B8C0", metaColor: "90989F", footerColor: "687078" },
  table: { headerBg: "C9A84C", headerText: "1A1A1A", accentLine: "C9A84C", innerLine: "DDD5C0", surface: "F5F2E8" },
};
const BODY = "000000";
const FONT = { ascii: "Times New Roman", eastAsia: "Times New Roman" };
const NB = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
const noBorders = { top: NB, bottom: NB, left: NB, right: NB };
const allNoBorders = { top: NB, bottom: NB, left: NB, right: NB, insideHorizontal: NB, insideVertical: NB };

function safeText(v, ph) {
  if (v === undefined || v === null || v === "" || String(v) === "NaN" || String(v) === "undefined") return ph || "n/a";
  return String(v);
}
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
function dataTable(header, rows, widths, opts) {
  const o = opts || {};
  const mkCell = (text, isHeader, wPct, first) => new TableCell({
    width: { size: wPct, type: WidthType.PERCENTAGE },
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    shading: isHeader ? { type: ShadingType.CLEAR, fill: P.table.headerBg } : undefined,
    children: [new Paragraph({
      alignment: AlignmentType.LEFT,
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
  rows.forEach(r => {
    trs.push(new TableRow({ cantSplit: true, children: r.map((t, i) => mkCell(t, false, widths[i], i === 0)) }));
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
function estimateTextWidth(text, pt) {
  let width = 0;
  for (const ch of text) {
    const code = ch.codePointAt(0);
    const isCJK = (code >= 0x4E00 && code <= 0x9FFF) || (code >= 0x3000 && code <= 0x303F) || (code >= 0xFF00 && code <= 0xFFEF);
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

const ASSETS_DIR = "/home/z/my-project/scripts/vc_report/assets";

// ================================================================ BODY CONTENT
const body = [];

// ------------------------------------------------ 1. Executive summary
body.push(h1("1. Executive Summary and Verdict"));
body.push(para([
  { t: "This report delivers the two-part mandate on the Volatility Compression / Squeeze Breakout suite: an independent red-team audit of " },
  { t: "Volatility_compression/quant_volatility_compression.py", i: true },
  { t: " and its research report, followed by an evolved engine (" },
  { t: "Volatility_compression/volatility_compression_evolved.py", i: true },
  { t: ") tested directly on the repository's parquet data under a stricter protocol than the baseline's. Every number below was recomputed by our own code against the repository's own artifacts, and the baseline engine was re-executed (not merely read) to confirm the published results." },
]));
body.push(para([
  { t: "Verdict on the baseline. ", b: true },
  { t: "The engine is deterministic and honest at the artifact level: four spot re-runs reproduce the shipped sweep CSV to the exact digit, and the report's ten-cluster summary table matches the CSV exactly. The defects are in assumptions and statistics. The cost model hard-codes two decimal digits for every instrument (the parquet has no digits column), charging FX majors a 1,000x-too-large spread on the roughly half of bars where the stored spread is non-zero at all; the docstring and report claim spreads are charged on both entry and exit, but the code charges one spread per round trip; the qualification filter selects on out-of-sample variables; the four exit modes are one signal series counted four times (across-series OOS-R correlation 0.980-0.995, so 208 reported configurations contain 52 independent series); and the report's two flagship multi-timeframe models (4H-squeeze-to-1H-trigger and cross-asset lead-lag) have no implementation anywhere in the shipped repository — their headline numbers cannot be reproduced from any artifact." },
]));
body.push(para([
  { t: "The FX collapse is a cost artifact. ", b: true },
  { t: "Re-running the baseline engine with correctly inferred tick sizes converts the \u201c-1,901.98R, 0% positive\u201d Forex-Majors verdict into 35 of 48 configurations positive out-of-sample (EURUSD 4H fixed-4R: +4.98R, PF 1.71; AUDUSD 4H fixed-4R: +15.99R, PF 3.00). The stored FX spread column is itself unusable for live estimation: it is zero on 54% (EURUSD 4H) to 67% (GBPUSD 4H) of bars. Neither the published catastrophe nor any \u201ccorrected\u201d FX result should be quoted as a market law." },
]));
body.push(para([
  { t: "The lead-lag premise is dead. ", b: true },
  { t: "Timestamp-aligned 4H log-return correlations are +0.781 (XAUUSD to XAGUSD), +0.949 (SP500 to NAS100) and +0.806 (BTCUSD to SOLUSD) at lag zero, and statistically indistinguishable from zero at every lag of one bar or more (largest |r| = 0.067). The \u201cbeta catch-up\u201d is co-movement, not spillover: when the lead's 4H bar closes, the lag's move is already in its price. A lead-release gate implemented in the evolved engine reduces sample size and adds nothing reliable." },
]));
body.push(para([
  { t: "What survives, and what does not. ", b: true },
  { t: "Gold squeeze breakouts survive in one specific sense: the as-published blueprint specification (a single pre-specified configuration: TTM squeeze k>=3, EMA200 trend gate, no filters, fixed 3R/4R target, basis stop), executed without any tuning on the out-of-sample window, earns +17.96R to +25.96R on XAUUSD and +26.95R on XAUAUD with meaningful t-statistics (2.10 and 3.32) — under stricter costs than the baseline charged, with 1.7-2.4x the baseline's sample size. But under full pre-registration (192-configuration in-sample search per asset with acceptance floors), gold fails: its best in-sample t-statistic is 0.92, below the 1.0 floor. The edge is regime-dependent — it lives in the 2025-2026 gold bull that the in-sample window barely contains — and the four gold crosses are one position held four times (pairwise return correlations 0.87-0.98). The pooled pre-registered portfolio across all eight asset-timeframe combinations is negative (-3.41R, deflated t = -4.14 against 1,536 trials). The correct institutional posture: paper-trade the single gold blueprint sleeve with pre-committed kill criteria; do not deploy FX, lead-lag, or 1H index sleeves; treat the chandelier exit as a crypto tool (BTC 4H chandelier OOS +3.81R is the only frozen configuration that survived), not a gold tool (it costs gold ~14-19R versus fixed 4R)." },
]));

// ------------------------------------------------ 2. Scope & method
body.push(h1("2. Scope, Data and Method"));
body.push(para([
  { t: "The audit covers the repository at the Volatility_compression suite as shipped on origin/main, the two results CSVs (208-row full sweep, 28-row qualified set), and the historical_parquet store. The actual parquet schema is timestamp, datetime, open, high, low, close, tick_volume, spread, real_volume — there is no digits column and no volume column, contrary to the audit brief's stated schema. Digit counts are therefore inferred from full-array price precision with an atol-only scan (the tolerance-based variant misreads large prices), with sanity bands per asset class." },
]));
body.push(caption("Table 1: Coverage and chronological split of the core validation assets (recomputed from parquet)"));
body.push(dataTable(
  ["Asset", "TF", "Bars", "Window", "IS ends / OOS starts", "OOS days"],
  [
    ["XAUUSD.pi", "4H", "4,843", "2020-08-11 to 2026-11-20", "2025-01-02 / 2025-01-03", "487"],
    ["XAUAUD.pi", "4H", "4,211", "2023-01-20 to 2026-11-20", "2025-09-25 / 2025-09-26", "298"],
    ["NAS100.p", "4H", "5,000", "2023-08-30 to 2026-11-20", "2025-12-02 / 2025-12-03", "251"],
    ["SP500.p", "4H", "5,000", "2023-08-30 to 2026-11-20", "2025-12-02 / 2025-12-03", "251"],
    ["BTCUSD.pi", "4H", "5,000", "2024-08-03 to 2026-11-22", "2026-03-15 / 2026-03-16", "252"],
    ["(all 1H files)", "1H", "5,000", "2026-01-20 to 2026-11-20", "2026-08-20 / 2026-08-21", "63-66"],
  ],
  [15, 7, 9, 27, 27, 15], { boldFirst: true }
));
body.push(para([
  { t: "Three coverage facts the baseline report never discloses. First, the out-of-sample windows differ wildly across assets: gold's OOS begins January 2025, the indices' December 2025, BTC's March 2026, and every 1H file has only 63-66 out-of-sample days (about three months). Cross-asset comparisons of \u201cOOS\u201d results therefore compare different regimes, not the same test. Second, XAUUSD's 4H file is sparse — 4,843 bars spanning six years is roughly two bars per trading day (4-hour gaps on 3,730 positions, 24-hour gaps on 781, 72-hour gaps on 188), so bar-count-based statistics on gold-4H silently operate on an irregular grid. Third, the report's blanket \u201cdataset 2021-2026\u201d claim is true only for XAUUSD; XAUAUD begins January 2023 and the indices August 2023." },
]));

// ------------------------------------------------ 3. Audit findings
body.push(h1("3. Independent Audit Findings"));

body.push(h2("3.1 Determinism and Exact Reproduction"));
body.push(para([
  { t: "We re-executed the shipped engine on four representative configurations (two headline metals rows, the worst FX row, and an indices row). All four reproduce the shipped CSV exactly, to the digit: XAUUSD 4H fixed-4R (IS +16.99R / OOS +14.99R / PF 4.00 / n=10 / t=1.80), XAUAUD 4H fixed-3R (+6.00 / +18.97 / 4.79 / 13 / 2.60), EURUSD 1H fixed-2R (-160.37 / -90.77 / 0.00 / 17 / -7.09), NAS100 1H fixed-4R (-7.09 / -2.03 / 0.80 / 12 / -0.30). The ten-cluster summary in the report's Section 2 also matches the CSV row for row. This matters: the audit is of a real, deterministic engine, and every defect identified below is a defect in assumptions or statistics, not in reproducibility." },
]));

body.push(h2("3.2 The Cost Model: Missing Digits, One-Spread Accounting, Zero-Spread FX Data"));
body.push(para([
  { t: "Three defects compound in the cost path. First, the engine reads a digits column that does not exist and falls back to 2 for every instrument: correct for metals, indices and crypto (genuinely two-decimal), but 1,000x too large for five-decimal FX majors and 10x too large for three-decimal JPY crosses. Second, the docstring and report state spreads are charged \u201con both entry and exit\u201d, but the code charges one spread per round trip (BUY pays at entry, SELL pays at exit) — and the SELL take-profit branch double-counts the exit spread, an inconsistency that makes SELL TP wins slightly conservative. Third, the stored FX spread column that feeds the model is degenerate: zero on 54% of EURUSD-4H bars and 67% of GBPUSD-4H bars, median 0-1 points — far below any live retail or prop spread (~0.6-1.0 pip on EURUSD). The cost model is therefore wrong in both directions at once: punishing on the non-zero bars via the digit fallback, free on the majority of bars via the stored zeros." },
]));
body.push(caption("Table 2: Corrected-tick FX re-runs of the baseline engine (same signals, inferred digits, OOS by the baseline's own trade-count split)"));
body.push(dataTable(
  ["Pair", "TF", "Exit", "Digits", "Baseline OOS R", "Corrected OOS R", "Corrected OOS PF", "n"],
  [
    ["EURUSD.pi", "4H", "fixed_4.0r", "5", "-40.31", "+4.98", "1.71", "10"],
    ["EURUSD.pi", "1H", "fixed_4.0r", "5", "-90.77", "+6.28", "1.69", "13"],
    ["GBPUSD.pi", "4H", "fixed_3.0r", "5", "-22.77", "+14.54", "3.91", "12"],
    ["USDJPY.pi", "4H", "fixed_4.0r", "3", "+3.42", "+3.49", "1.41", "12"],
    ["AUDUSD.pi", "4H", "fixed_4.0r", "5", "-26.63", "+15.99", "3.00", "14"],
    ["USDCAD.pi", "1H", "fixed_4.0r", "5", "-41.94", "+13.46", "3.24", "11"],
    ["GBPJPY.pi", "1H", "fixed_4.0r", "3", "-16.36", "+6.98", "1.77", "13"],
  ],
  [15, 7, 15, 9, 18, 18, 11, 7], { boldFirst: true }
));
body.push(para([
  { t: "Across all 48 FX configurations (6 pairs x 2 timeframes x 4 exits), 35 are positive out-of-sample under corrected ticks. This independently confirms the companion audit's central cost finding (three of its four quoted corrected numbers match our re-runs to the digit; the fourth differs by rounding). Two cautions apply before inverting the report's conclusion. The corrected runs still charge the stored (mostly zero) spread, so they remain optimistic; and most corrected configurations fail the baseline's own in-sample floor (IS profit factors of 0.4-0.8 on several rows), so \u201cpositive OOS\u201d here means \u201cthe catastrophe was fake\u201d, not \u201cFX squeeze trading is validated\u201d. The defensible statement is that no FX conclusion of any sign survives this dataset's cost column." },
]));

body.push(h2("3.3 Selection on Out-of-Sample, Degenerate Filters, and Pseudo-Trials"));
body.push(para([
  { t: "The qualification filter (IS PF >= 1.25 AND OOS PF >= 1.20 AND OOS n >= 8 AND OOS R > 0) uses out-of-sample variables to decide what counts as validated — the same methodological defect documented in our previous ORB audit of this repository. Within the headline metals-4H cluster the IS-PF criterion alone admits 85% of rows (average IS PF 1.83), so \u201c16 of 20 setups positive out-of-sample\u201d is close to the base rate of the cluster, not a discriminating result. Separately, the four exit modes are the same signal series with different exits: correlating out-of-sample R across the 52 asset-timeframe series with all four exits gives pairwise correlations of 0.980-0.995 (median 0.990) between exit modes. The 208 reported \u201cconfigurations\u201d therefore contain at most 52 independent signal series, and the metals-4H cluster contains 10." },
]));
body.push(para([
  { t: "We also tested the metals edge as a cluster rather than as a best-row story: pooling out-of-sample trades of the five metals on 4H across the fixed-3R and fixed-4R exits (10 signal series), aggregated by day, gives +98.3R over 37 out-of-sample days with t = 1.82 — deflating against the 10-series search space subtracts 3.03, leaving -1.21. Even the honest pooled version of the flagship result does not clear a minimal multiple-testing bar. The per-row deflated t-statistics (haircut 3.27 for 208 trials) are all negative: the best advertised row, XAUAUD 4H fixed-3R at t = 2.60, deflates to -0.67." },
]));

body.push(h2("3.4 The Lead-Lag Premise and the Report's Unimplemented Models"));
body.push(para([
  { t: "The report's Section 4 presents two models — 4H-squeeze-to-1H-trigger coherence (NAS100 OOS PF 2.68, +6.00R; SP500 OOS PF 2.95, +11.07R) and cross-asset lead-lag spillover (SP500 to NAS100 OOS +7.78R; BTC to SOL +7.83R). Neither model exists in the shipped engine (which implements only the single-timeframe squeeze), and neither appears in the shipped sweep CSVs: the actual best NAS100 1H row in the full sweep is fixed-2R at OOS PF 0.88 (-1.04R), and the best SP500 1H row is trail-1R at OOS PF 0.80 (-1.58R). Those four headline numbers cannot be reproduced from any artifact in the repository, and no code path produces them." },
]));
body.push(para([
  { t: "We measured the spillover premise directly. With bars aligned by timestamp (inner join), 4H log-return correlations at lag zero are +0.781 (XAUUSD to XAGUSD), +0.949 (SP500 to NAS100) and +0.806 (BTCUSD to SOLUSD); at lags one through seven every correlation lies between -0.067 and +0.067, inside the noise band for ~4,800-5,000 observations. Lag zero explains 61-90% of the lagging asset's return variance — that is simultaneous co-movement, not information transfer. In the evolved engine we implemented the gate anyway (enter the lag's squeeze release only if the lead released within the last 2/4/8 bars): the gate cuts sample size by 60-75% and adds nothing reliable (gold-silver: gated +0.90R over 7 trades versus ungated +3.45R over 28; SP500-NAS100: gated -5.01R versus ungated -3.13R; BTC-SOL: gated +1.60R over 6 trades — noise; USWTI to USDCAD: zero gated trades). The lead-lag sleeves should be withdrawn from the research narrative." },
]));

body.push(h2("3.5 Documentation versus Code"));
body.push(caption("Table 3: Claims in the engine docstring / report versus the shipped code"));
body.push(dataTable(
  ["Documented claim", "Actual code", "Consequence"],
  [
    ["\u201cBar range > 1.2 * ATR momentum confirmation\u201d (docstring + README)",
     "No such check exists in simulate_squeeze",
     "Reported mechanics are not the tested mechanics"],
    ["\u201cStructural SL: opposing Keltner channel band\u201d",
     "Stop distance = entry to SMA basis (middle band), floored at 1.2 * ATR",
     "Stops are tighter than documented; R-multiples correspondingly larger"],
    ["\u201cReal broker bid-ask spreads charged on BOTH entry and exit\u201d",
     "One spread per round trip; SELL take-profit double-counts exit spread",
     "Costs ~2x lighter than documented on the majority side; stricter costs (below) re-price every row"],
    ["\u201c70% In-Sample / 30% Out-of-Sample chronological split\u201d",
     "n_is = int(n_trades * 0.70) — split on the trade list, not calendar days",
     "Clustered signals slide the IS/OOS boundary by months versus a day split"],
    ["Report: \u201c208 configurations\u201d as independent evidence",
     "4 exits are one signal series (across-series OOS-R r = 0.99)",
     "52 independent series, not 208"],
  ],
  [34, 34, 32], { boldFirst: false }
));

body.push(h2("3.6 Why Gold Dominates - and the Caveats"));
body.push(para([
  { t: "The structural story is real. Gold is a macro-regime asset: a multi-bar squeeze resolves into a directional repricing of real-rate and inflation expectations that persists for weeks, whereas a currency pair is a relative price between two managed policy blocks whose post-squeeze expansions mean-revert inside policy bands. The measurement agrees: gold-4H squeeze configurations are positive in the 2023, 2024, 2025 and 2026 segments of the shipped configs (fixed-3R: +6.0, +2.0, +7.0, +11.0 R), while the crypto and index clusters are net negative over the same windows." },
]));
body.push(para([
  { t: "Three caveats keep the story honest. First, the four \u201cmetals\u201d symbols are one asset quoted four ways: pairwise 4H return correlations run 0.87-0.98 (XAUUSD-XAUCNH 0.98, XAUUSD-XAUAUD 0.87), so \u201c16 of 20 metals setups positive\u201d is one view sampled with four quote currencies — the cluster is not four independent confirmations. Second, the companion audit's \u201cpositive in every year including 2021 and 2022\u201d does not reproduce under the shipped configurations: our yearly decomposition of the fixed-3R and fixed-4R configs shows 2021 at -1.0R and no 2022 trades (the sparse 4H file skips most of that year), so persistence is demonstrated for 2023-2026 only. Third, the in-sample half of the data barely contains the gold edge at all (best IS t = 0.92 for XAUUSD, IS R = -2.10 for XAUAUD), which is precisely why the pre-registered sweep in Section 6 skips gold: the OOS profits coincide with the 2025-2026 bull regime, and no in-sample evidence selects them." },
]));

// ------------------------------------------------ 4. Companion audit review
body.push(h1("4. Review of the Companion Audit (AUDIT_REPORT (3).md)"));
body.push(para([
  { t: "A second AI's audit of the same suite was provided for independent review. We verified each load-bearing claim against the repository before accepting it. Overall assessment: the companion audit's four defect findings are correct and its central quantitative results reproduce almost exactly; it found the two most important structural facts (the FX cost artifact and the lead-lag co-movement). Its weak points are several misquoted statistics, one non-reproducible persistence claim, and artifacts that are still not shipped in the repository." },
]));
body.push(h2("4.1 Claims Verified Independently"));
body.push(caption("Table 4: Companion claims versus this audit's verification"));
body.push(dataTable(
  ["Companion claim", "This audit's verification", "Agreement"],
  [
    ["Baseline reproduces exactly (+155.13R metals-4H, -1,901.98R FX-1H)",
     "4/4 spot re-runs match the CSV to the digit; cluster table matches row for row",
     "Confirmed"],
    ["digits fallback = 2; FX overcharged up to 1,000x",
     "Confirmed in code (line 126) and by corrected-tick re-runs (35/48 FX rows flip positive)",
     "Confirmed"],
    ["EURUSD 4H corrected +4.98R PF 1.71; EURUSD 1H +6.28R PF 1.69; AUDUSD 4H +15.99R PF 3.00",
     "Exact match on all three (our re-runs)",
     "Confirmed"],
    ["Lead-lag: lag-0 r = +0.78/+0.95/+0.81; ~0 at every lag >= 1",
     "Timestamp inner-join reproduces +0.781/+0.949/+0.806; all lagged |r| <= 0.067",
     "Confirmed (exact)"],
    ["Exit modes are one signal series (r = 0.98-0.999); 208 configs = 52 series",
     "Across-series OOS-R correlations 0.980-0.995, median 0.990; 52 series with all 4 exits",
     "Confirmed"],
    ["Metals-4H IS-PF filter admits ~90% of rows (avg IS PF 1.83)",
     "Measured 85% admission; avg IS PF 1.83, avg OOS PF 2.55",
     "Confirmed (85%, not 90%)"],
    ["FX-4H spread zero on 39-67% of bars",
     "EURUSD 54%, GBPUSD 67% (4H); XAUUSD 27% with median 7 points",
     "Confirmed"],
    ["Deflated t negative for every configuration (haircut 3.27)",
     "All 28 qualified rows deflate negative; also our pooled metals test (t 1.82 -> -1.21) fails",
     "Confirmed"],
  ],
  [34, 46, 20], { boldFirst: false }
));
body.push(h2("4.2 Errors and Weak Points in the Companion Audit"));
body.push(para([
  { t: "Four issues reduce its evidentiary weight. First, its deflated-t table misquotes two of the four statistics it examines: it lists XAUGBP 4H fixed-4R at OOS t = 2.89, but 2.89 is that row's all-sample profit factor — the actual OOS t is 1.22 (deflating to -2.05, worse than its -0.38); and it lists XAUCNH at t = 2.24 where the shipped CSV says 2.15. The conclusions survive, but the table does not quote its own source correctly. Second, its \u201cgold positive in every year\u201d decomposition does not reproduce under the shipped configurations: our yearly cut shows 2021 negative (-1.0R) and no 2022 trades; its claimed 2022 (+1.0R) figure cannot be produced by the shipped engine on the shipped data under any exit mode we ran. Third, its claimed artifacts — Volatility_compression/volatility_compression_evolved.py, Volatility_compression/AUDIT_REPORT.md, and the desk/research/*.csv result files — are not present in the shipped repository, so its ablation, exit-lab and sweep tables cannot be reproduced or falsified from the repo; its evolved-engine numbers are reported results from an unavailable implementation. Fourth, its framing that the Volatility_compression directory \u201cdoes not exist\u201d at the reviewed commit has since been overtaken by events — the suite now ships at the canonical path on origin/main — and its Q1 compression-gate sweep covers only three gate settings on three assets, too thin to carry the general claim that percentile gates \u201cdestroy\u201d the metals edge (our 192-configuration sweep treats the gates as asset-conditional and lets in-sample selection decide)." },
]));
body.push(para([
  { t: "On the substance we agree with the companion more than we disagree: its single-variable exit experiment (chandelier superior on crypto, inferior on gold) reproduces exactly in our independent implementation (Section 6.1), and its production recommendation — deploy one gold sleeve with explicit limits or nothing — matches our own conclusion, reached through a stricter pre-registration route that skips gold entirely." },
]));

// ------------------------------------------------ 5. Evolved engine
body.push(h1("5. The Evolved Engine (volatility_compression_evolved.py)"));
body.push(h2("5.1 Design Mapping: Audit Finding to Engine Feature"));
body.push(para([
  { t: "The evolved engine is a single self-contained module (pandas/numpy only, no network) with six modes: --selftest (digit inference, cost sanity, causal-execution assertions), --exitlab (single-variable exit experiment), --sweep (pre-registered in-sample search), --leadlag (spillover measurement), --compare (baseline-versus-evolved table) and --compare against the shipped CSV. Every audit finding maps to a concrete feature." },
]));
body.push(caption("Table 5: Audit findings implemented in the evolved engine"));
body.push(dataTable(
  ["Audit finding", "Engine feature"],
  [
    ["No digits column; baseline fallback wrong on FX",
     "Full-array precision inference with per-class sanity bands; refuse to trade if median spread > 50% of median bar range"],
    ["Baseline charges one spread per round trip (docstring claims two)",
     "Strict protocol: spread charged on every fill, symmetric for both sides; stop-distance floor of 3x spread"],
    ["Trade-list 70/30 split",
     "Chronological split by session days"],
    ["Qualification selects on OOS variables; filter admits 85% of metals rows",
     "Pre-registration: IS-only scoring (n >= 10, IS PF >= 1.2, IS t >= 1.0), parameters frozen, OOS evaluated once; deflated t against the true trial count (1,536)"],
    ["Exit modes are pseudo-trials",
     "Exit chosen as one design decision per frozen configuration; --exitlab isolates the exit variable"],
    ["Docstring/code mismatches",
     "The engine implements and documents the same mechanics; deviations (bar-quality gate, momentum slope) are explicit config flags"],
    ["Lead-lag premise unmeasured in the baseline",
     "--leadlag module: inner-join lead-release gate at 2/4/8 bars, reported against the ungated baseline whatever the result"],
  ],
  [38, 62], { boldFirst: false }
));
body.push(h2("5.2 The Five Brief Innovations"));
body.push(para([
  { t: "All five Phase-2 innovations are implemented and config-gated. Multi-factor compression: the TTM squeeze (BB(20,2) inside KC(20,1.5*ATR14)) with maturity k >= 3, plus optional Bollinger-bandwidth and Parkinson-volatility percentile gates over trailing 100-bar windows. Momentum directionality: ordinary-least-squares slope of the MACD(12,26,9) histogram over the last five bars, signed with the breakout. Volume and absorption: tick volume >= 1.5x its 20-bar average OR a positive 10-bar cumulative volume delta proxy ((close-open)/range * volume, summed). Adaptive exits: fixed 2R/3R/4R, a 1R-breakeven trail, and a Chandelier stop (rolling extreme minus 2.5*ATR14 over closed bars, filled intrabar at the resting level). Cross-asset lead-lag: XAUUSD to XAGUSD, SP500 to NAS100, BTCUSD to SOLUSD and USWTI to USDCAD, gated on the lead's fresh squeeze release. The selftest passes on all eight asset-timeframe combinations with digits correctly inferred (all two-decimal; XAUUSD spread $0.07, BTC $16-18, indices $0.30-1.10 per point)." },
]));

// ------------------------------------------------ 6. Results
body.push(h1("6. Results Under the Evolved Protocol"));
body.push(h2("6.1 Exit Lab: the Chandelier is a Crypto Tool, not a Gold Tool"));
body.push(para([
  { t: "Holding the signal stack fixed (the baseline's own mechanics: TTM squeeze k=3, EMA200 gate, bullish/bearish close trigger, basis stop) and varying only the exit isolates the brief's Innovation 4. The result is asset-conditional and directly contradicts the brief's premise for the asset class it names: on gold the fixed 4R target dominates and the Chandelier gives back 14-19R of all-sample profit (XAUUSD: 30.96R fixed-4R versus 7.22R chandelier); on BTC the ordering reverses (chandelier +7.11R all-sample / +3.81R OOS is the only positive fixed-exit alternative, while fixed-4R loses -10.59R). Gold squeeze releases travel a fairly fixed distance, so a rigid target harvests them better; BTC's post-squeeze expansions are heavy-tailed and a fixed target truncates exactly the trades that pay." },
]));
body.push(...figure(path.join(ASSETS_DIR, "fig_exit_lab.png"), 500, 250,
  "Figure 1: Exit lab, all-sample net R under identical signals. Fixed 4R maximises gold; the Chandelier minimises damage on BTC 4H and is the only positive crypto exit besides the 1R trail."));
body.push(h2("6.2 The A-Priori Blueprint Replication: Gold Passes Out-of-Sample"));
body.push(para([
  { t: "The baseline report's own Section 5 blueprint is a pre-specified configuration: it was published before any of our testing, contains no tunable knob we selected, and names gold 4H with a fixed 3R/4R target. Executing that specification as-is — under stricter costs than the baseline charged (spread on both fills), with the day-based split — is therefore a genuine single-hypothesis out-of-sample test requiring no deflation." },
]));
body.push(...figure(path.join(ASSETS_DIR, "fig_gold_oos_equity.png"), 500, 260,
  "Figure 2: XAUUSD 4H, a-priori blueprint specification, out-of-sample equity by exit mode (no tuning, both-fill spread costs)."));
body.push(caption("Table 6: A-priori blueprint spec on gold 4H, out-of-sample (both-fill spreads, day-based split)"));
body.push(dataTable(
  ["Asset", "Exit", "OOS n", "OOS Net R", "OOS PF", "OOS t", "OOS Max DD"],
  [
    ["XAUUSD.pi", "fixed_3.0r", "26", "+17.96", "2.20", "1.75", "n/a (per-trade DD not pooled)"],
    ["XAUUSD.pi", "fixed_4.0r", "24", "+25.96", "2.85", "2.10", "-"],
    ["XAUAUD.pi", "fixed_3.0r", "17", "+26.95", "5.48", "3.32", "-"],
    ["XAUAUD.pi", "fixed_4.0r", "15", "+24.95", "4.55", "2.50", "-"],
  ],
  [16, 16, 10, 14, 12, 10, 22], { boldFirst: true }
));
body.push(para([
  { t: "For contrast, the baseline's published versions of these rows (one-fill spread, trade-list split) show +14.99R at n=10 (XAUUSD fixed-4R) and +18.97R at n=13 (XAUAUD fixed-3R): the same specification under stricter accounting earns roughly 1.7-1.4x more R on 1.7-2.4x more trades, and the OOS t-statistics (2.10 and 3.32 at n >= 15) are meaningful without any selection. This is the one cell of the study where the baseline's direction is confirmed and strengthened — with the regime caveat of Section 3.6 attached: the in-sample window contains almost no evidence for it (IS t = 0.92 on XAUUSD), so the sleeve's live performance is a bet on regime persistence, sized accordingly." },
]));
body.push(h2("6.3 Pre-Registered Sweep: What Survives Honest Selection"));
body.push(para([
  { t: "The full sweep evaluated 192 configurations per asset-timeframe (1,536 total), scored exclusively in-sample with acceptance floors (n >= 10, IS PF >= 1.2, IS t >= 1.0), then froze the winner and evaluated out-of-sample exactly once. The outcome is the mirror image of the baseline's qualification method: the two headline gold assets fail their own in-sample floor (XAUUSD's best IS t = 0.92; XAUAUD's best IS R = -2.10) and are reported as honest skips; NAS100-4H and both SP500 combinations freeze in-sample winners that evaporate out-of-sample (NAS100: IS +11.97R PF 2.00 becoming OOS -3.13R PF 0.61 — a textbook in-sample overfit); and the single surviving frozen configuration is BTCUSD-4H with the momentum-slope gate and the Chandelier exit (IS +6.32R PF 1.64; OOS +3.81R PF 1.33 over 26 trades, t = 0.42, deflated -2.82). The pooled pre-registered portfolio is -3.41R over 45 out-of-sample trades (deflated t = -4.14 against 1,536 trials)." },
]));
body.push(caption("Table 7: Pre-registered sweep results (192 configs per asset-TF; IS-only selection; single OOS evaluation)"));
body.push(dataTable(
  ["Asset / TF", "Frozen configuration", "IS R / PF / t", "OOS R / PF / n", "Verdict"],
  [
    ["XAUUSD 4H", "none qualified (best IS t = 0.92 < 1.0)", "-", "-", "honest skip"],
    ["XAUAUD 4H", "none qualified (best IS R = -2.10)", "-", "-", "honest skip"],
    ["NAS100 1H", "none qualified", "-", "-", "honest skip"],
    ["NAS100 4H", "k=3, bar-quality, fixed 3R", "+11.97 / 2.00 / 1.33", "-3.13 / 0.61 / 10", "IS overfit; fails OOS"],
    ["SP500 1H", "k=3, Parkinson<=0.6, fixed 4R", "+14.91 / 3.96 / 1.79", "-4.07 / 0.00 / 4", "IS overfit; n too small"],
    ["SP500 4H", "k=3, Parkinson<=0.6, fixed 4R", "+19.99 / 5.99 / 2.45", "-0.02 / 0.99 / 5", "IS overfit"],
    ["BTCUSD 1H", "none qualified", "-", "-", "honest skip"],
    ["BTCUSD 4H", "k=3, MACD-slope, Chandelier", "+6.32 / 1.64 / 1.03", "+3.81 / 1.33 / 26", "only survivor (not significant)"],
    ["Pooled", "all frozen sleeves", "-", "-3.41 / 0.88 / 45", "deflated t = -4.14"],
  ],
  [14, 30, 20, 20, 16], { boldFirst: true }
));
body.push(para([
  { t: "Reading the two gold results together is the central lesson of this study. The same underlying configuration family produces a statistically respectable out-of-sample result when tested as the single pre-specified hypothesis it was published as (Section 6.2), and fails the selection pipeline when thrown into a 192-configuration search — because its in-sample footprint is too weak to distinguish it from 191 alternatives. Search destroys the gold edge statistically even though the edge exists economically in the OOS window. Any live deployment decision should therefore treat the gold blueprint as one pre-committed hypothesis with fixed parameters (never re-optimized), sized for the possibility that the 2025-2026 regime does not persist." },
]));
body.push(h2("6.4 Lead-Lag Lab: the Gate Adds Nothing"));
body.push(para([
  { t: "The lead-release gate (enter the lag's squeeze release only if the lead released within the last m bars) was measured at m = 2, 4 and 8 bars against the ungated baseline of the same configuration. Gating never improves per-trade quality enough to justify the sample destruction: gold-silver gated +0.90R (n=7) versus ungated +3.45R (n=28); SP500-NAS100 gated -5.01R (n=5) versus ungated -3.13R (n=10); BTC-SOL gated +1.60R (n=6) — within noise of zero expectancy — versus ungated -5.54R (n=17); USWTI to USDCAD produces zero gated trades. Consistent with Section 3.4's correlation measurements, the lead carries no exploitable information about the lag's next bar, and the report's Model B numbers should be withdrawn." },
]));
body.push(h2("6.5 Comparative Performance versus Baseline"));
body.push(para([
  { t: "The mission-required comparison follows, pairing each asset-timeframe's best published baseline row (from the shipped full-sweep CSV, sorted by OOS R) with the evolved engine's pre-registered outcome. Where the evolved engine reports an honest skip, the a-priori blueprint replication from Section 6.2 is quoted in its row, because that is the only like-for-like pre-committed test of the same specification. The asymmetry runs against the evolved engine by design: it pays the spread on both fills where the baseline paid once, and splits by days where the baseline split by trade count." },
]));
body.push(caption("Table 8: Comparative performance (baseline as published in the shipped sweep; evolved under the strict pre-registered protocol)"));
body.push(dataTable(
  ["Asset", "TF", "Baseline OOS PF", "Evolved OOS PF", "Baseline OOS R", "Evolved OOS R", "Evolved Max DD", "t-stat"],
  [
    ["XAUUSD.pi", "4H", "4.00 (n=10)", "2.85 (a-priori spec, n=24)", "+14.99", "+25.96", "6.0R (all-sample)", "2.10"],
    ["XAUAUD.pi", "4H", "4.79 (n=13)", "5.48 (a-priori spec, n=17)", "+18.97", "+26.95", "3.8R (all-sample)", "3.32"],
    ["NAS100.p", "1H", "0.88 (n=13)", "no qualified config", "-1.04", "-", "-", "-"],
    ["NAS100.p", "4H", "1.24 (n=11)", "0.61 (frozen, n=10)", "+1.21", "-3.13", "5.01R", "-0.67"],
    ["SP500.p", "1H", "0.80 (n=15)", "0.00 (frozen, n=4)", "-1.58", "-4.07", "3.06R", "n<10"],
    ["SP500.p", "4H", "0.73 (n=10)", "0.99 (frozen, n=5)", "-1.36", "-0.02", "3.02R", "n<10"],
    ["BTCUSD.pi", "1H", "2.00 (n=16)", "no qualified config", "+7.03", "-", "-", "-"],
    ["BTCUSD.pi", "4H", "1.18 (n=18)", "1.33 (frozen, n=26)", "+1.66", "+3.81", "5.99R", "0.42"],
  ],
  [13, 6, 17, 22, 12, 12, 10, 8], { boldFirst: true }
));
body.push(para([
  { t: "The comparison's honest summary: on the two assets where the baseline claims its flagship edge (gold), the evolved engine confirms the direction with larger R, larger samples and meaningful t-statistics under stricter costs — while simultaneously demonstrating that no search-based selection procedure can justify the configuration, because its in-sample footprint is too weak. Everywhere else the baseline's published \u201cvalidated\u201d rows fail replication under honest accounting, the frozen configurations that pass in-sample selection fail out-of-sample, and the one survivor (BTC-4H chandelier) is not statistically established. No configuration in this study — baseline or evolved — carries a positive deflated t-statistic. The suite is a research artifact, not a production allocation." },
]));

// ------------------------------------------------ 7. Production rules
body.push(h1("7. Production Desk Rules (MT5)"));
body.push(h2("7.1 Deployment Decision"));
body.push(para([
  { t: "Do not deploy the suite. One sleeve is eligible for a proving period, under the following conditions and none other: the gold blueprint specification executed exactly as published (no re-optimization, ever). The four gold crosses are one position quoted four times — trade XAUUSD.pi only, with the tightest relative spread of the family (median 7 points = $0.07, 0.0021% of price). Nothing else in this study earns deployment: FX sleeves rest on a degenerate spread column (54-67% zeros), lead-lag sleeves rest on zero lagged correlation, 1H index sleeves have 63-66 out-of-sample days and negative frozen results, and the BTC frozen sleeve is positive but carries a negative deflated t-statistic." },
]));
body.push(h2("7.2 XAUUSD Blueprint Sleeve - Execution Parameters"));
body.push(caption("Table 9: XAUUSD 4H blueprint sleeve (as-published specification, strict accounting)"));
body.push(dataTable(
  ["Parameter", "Value", "Execution note"],
  [
    ["Squeeze state", "BB(20, 2.0) fully inside KC(20, 1.5 x ATR14) for >= 3 consecutive 4H bars",
     "Compute on closed 4H bars; the release bar itself may already be outside the squeeze"],
    ["Entry trigger", "4H close above KC upper (long) / below KC lower (short), above/below EMA200, in bar direction",
     "Closed-bar evaluation only; no wick entries"],
    ["Fill", "Market order at the next 4H bar open",
     "Never at the signal bar's close; expect ~1 bar of latency"],
    ["Stop", "Distance from entry to the SMA-20 basis, floored at 1.2 x ATR14",
     "Reject the order if that distance is < 3x the live spread"],
    ["Target", "Fixed 4.0R (measured best on gold; chandelier gave back 14-19R)",
     "Do NOT trail gold; the measured distribution is fixed-distance"],
    ["Session cap", "No new signals after ~6 bars post-release; cooldown 6 bars after any exit; 1 position",
     "Cooldown is part of the tested specification"],
    ["Cost gate", "Skip the session if live spread > 3x the stored median ($0.21)",
     "The stored column under-represents live widening"],
  ],
  [16, 40, 44], { boldFirst: true }
));
body.push(h2("7.3 Sizing, Expectations and Kill Criteria"));
body.push(para([
  { t: "Sizing: 0.25-0.5% of equity risked per trade (1R), one position, no averaging. Expectations, stated plainly: the tested specification produced roughly 34-51 trades over 3.3-6.3 years of sparse 4H data — about 10 trades per year — with out-of-sample expectancy of +1.0R per trade on the fixed-4R row. At 0.5% risk that is roughly 2-5% per year gross. This is a validated research hypothesis, not a performance solution; scaling it to institutional return targets requires either more instruments (each re-validated individually under the same protocol) or leverage, and leverage on a 24-trade out-of-sample sample is exactly the failure mode this audit exists to prevent. Kill criteria, pre-committed: halt the sleeve if the rolling 30-trade profit factor falls below 1.2, or if cumulative R falls below -8R (roughly twice the observed out-of-sample drawdown), or if the gold regime marker (price below its own 200-day EMA for 20 consecutive sessions) fires while the sleeve is net negative. Review cadence: weekly reconciliation of the journal against the engine's trade log (both-fill spread accounting, next-open fills), and a full re-validation if the broker changes contract specifications." },
]));
body.push(h2("7.4 Data Prerequisites Before Any Size Increase"));
body.push(para([
  { t: "The binding constraint on every result in this report is data quality, not engine quality. Three gaps must close before scaling: first, extend intraday (1H) history to at least two years per symbol — the current 1H files cover ten months, too short to contain a full gold drawdown cycle; second, repair the spread column — FX bars are 54-67% zeros and gold's 4H file is sparse (roughly two bars per trading day, with 24- and 72-hour gaps), so cost models built on it are unverifiable; third, ship per-symbol contract specifications (digits, tick value, session calendar) alongside the parquet so the digit fallback defect can never recur. Until then, every FX number of any sign, every lead-lag claim, and every 1H result should be treated as unestablished." },
]));

// ------------------------------------------------ 8. Reproduction
body.push(h1("8. Reproduction and Artifacts"));
body.push(para([
  { t: "All figures in this report derive from code executed against the repository's own data. The audit script (scripts/vc_audit.py) re-runs the shipped baseline engine for verification, performs the corrected-tick FX re-runs, the cross-correlation and exit-correlation measurements, the pooled metals test, and the coverage decomposition. The evolved engine is Volatility_compression/volatility_compression_evolved.py; its artifacts are written under Volatility_compression/results/ (vc_evolved_frozen.json, vc_evolved_exitlab.csv, vc_evolved_leadlag.csv, vc_evolved_compare.csv, vc_evolved_oos_trades.csv, vc_sweep_rows_*.csv). Reproduction commands:" },
]));
body.push(para([{ t: "python3 Volatility_compression/volatility_compression_evolved.py --selftest", i: true }], { after: 40 }));
body.push(para([{ t: "python3 Volatility_compression/volatility_compression_evolved.py --exitlab", i: true }], { after: 40 }));
body.push(para([{ t: "python3 Volatility_compression/volatility_compression_evolved.py --sweep", i: true }], { after: 40 }));
body.push(para([{ t: "python3 Volatility_compression/volatility_compression_evolved.py --leadlag", i: true }], { after: 40 }));
body.push(para([{ t: "python3 Volatility_compression/volatility_compression_evolved.py --compare", i: true }]));
body.push(para([
  { t: "The selftest asserts causal execution (every trade's entry time strictly after its signal time) on all eight asset-timeframe combinations. The sweep writes per-asset-TF row files so every frozen decision can be audited against the full in-sample grid it was selected from. Deflated t-statistics are reported against the true trial counts: 192 per asset-timeframe sweep, 1,536 pooled." },
]));

// ------------------------------------------------ Appendix
body.push(h1("Appendix A. Frozen Configurations and Baseline Rows"));
body.push(para([
  { t: "Frozen configurations (from Volatility_compression/results/vc_evolved_frozen.json). NAS100 4H: min_squeeze_bars=3, bw_pct=None, park_pct=None, mom_slope=False, vol_confirm=False, bar_quality=True, exit=fixed_3.0r; IS n=20, +11.97R, PF 2.00, t=1.33; OOS n=10, -3.13R, PF 0.61, t=-0.67, deflated -3.91. SP500 1H: min_squeeze_bars=3, park_pct=0.6, exit=fixed_4.0r; IS n=10, +14.91R, PF 3.96, t=1.79; OOS n=4, -4.07R (sample too small for t). SP500 4H: same family; IS n=10, +19.99R, PF 5.99, t=2.45; OOS n=5, -0.02R, deflated -3.25. BTCUSD 4H: min_squeeze_bars=3, mom_slope=True, exit=chandelier (2.5 x ATR14); IS n=30, +6.32R, PF 1.64, t=1.03; OOS n=26, +3.81R, PF 1.33, t=0.42, deflated -2.82. XAUUSD 4H, XAUAUD 4H, NAS100 1H, BTCUSD 1H: no configuration passed the IS floor; recorded as honest skips with their best in-sample rows preserved in vc_sweep_rows_*.csv." },
]));
body.push(para([
  { t: "Baseline rows quoted in Table 8, as published in the shipped full-sweep CSV: XAUUSD 4H fixed_4.0r (OOS PF 4.00, +14.99R, n=10, t=1.80); XAUAUD 4H fixed_3.0r (4.79, +18.97R, n=13, t=2.60); NAS100 1H fixed_2.0r (0.88, -1.04R, n=13); NAS100 4H trail_1.0r (1.24, +1.21R, n=11); SP500 1H trail_1.0r (0.80, -1.58R, n=15); SP500 4H trail_1.0r (0.73, -1.36R, n=10); BTCUSD 1H trail_1.0r (2.00, +7.03R, n=16, t=1.03); BTCUSD 4H trail_1.0r (1.18, +1.66R, n=18). These are the best OOS-R rows per asset-timeframe in the shipped sweep; they were selected on out-of-sample variables by the qualification filter and should be read as upper-tail descriptions, not hold-out results (Section 3.3)." },
]));

// ================================================================ ASSEMBLY
const coverConfig = {
  title: "Volatility Compression Suite Quantitative Audit and Evolved Design",
  subtitle: "Red-team review of the multi-asset squeeze engine, and a pre-registered rebuild",
  englishLabel: "QUANTITATIVE AUDIT",
  metaLines: [
    "Repository: karan1399/Trading-Strategies (Volatility_compression suite)",
    "Data: historical_parquet - 15M/1H/4H/5M, 151 symbols, 2020-2026 (per-asset windows in Table 1)",
    "Scope: XAUUSD / XAUAUD 4H, NAS100 / SP500 1H+4H, BTCUSD 1H+4H, lead-lag expansion",
    "Date: 2026-11-24",
  ],
  footerLeft: "Senior Quant Researcher / Algo Systems",
  footerRight: "VC_AUDIT_EVOLVED",
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
      children: [new TextRun({ text: "Volatility Compression Suite - Quantitative Audit and Evolved Design", size: 18, color: "808080", font: FONT })],
    })],
  });
}

const doc = new Document({
  styles: {
    default: {
      document: { run: { font: FONT, size: 24, color: BODY }, paragraph: { spacing: { line: 312 } } },
      heading1: { run: { font: FONT, size: 32, bold: true, color: BODY },
        paragraph: { spacing: { before: 360, after: 160, line: 312 }, outlineLevel: 0 } },
      heading2: { run: { font: FONT, size: 30, bold: true, color: BODY },
        paragraph: { spacing: { before: 240, after: 120, line: 312 }, outlineLevel: 1 } },
      heading3: { run: { font: FONT, size: 28, bold: true, color: BODY },
        paragraph: { spacing: { before: 200, after: 100, line: 312 }, outlineLevel: 2 } },
    },
  },
  sections: [
    {
      properties: { page: { size: pgSize, margin: { top: 0, bottom: 0, left: 0, right: 0 } } },
      children: buildCoverR1(coverConfig),
    },
    {
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
      ],
    },
    {
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
  const out = "/home/z/my-project/download/VC_Quantitative_Audit_and_Evolved_Design.docx";
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, buf);
  console.log("WROTE", out, buf.length, "bytes");
});




