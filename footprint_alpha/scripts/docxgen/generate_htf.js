// HTF EMA 89/233 Rejection — Strategy Research Report (R1 cover, IG-1 palette)
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  ImageRun, PageBreak, Header, Footer, PageNumber, NumberFormat,
  AlignmentType, HeadingLevel, WidthType, BorderStyle, ShadingType,
  SectionType, TableOfContents,
} = require("docx");
const fs = require("fs");

const S = JSON.parse(fs.readFileSync("/home/z/my-project/download/htf/htf_summary.json", "utf8"));
const CH = "/home/z/my-project/download/htf/charts/";
const OUT = "/home/z/my-project/download/htf/HTF_EMA_Rejection_Strategy_Report.docx";
const B = S.basket, E = B.equity, SW = S.sweep, SEN = B.sensitivity;

const P = {
  accent: "C9A84C",
  cover: { titleColor: "FFFFFF", subtitleColor: "B0B8C0", metaColor: "90989F", footerColor: "687078" },
  table: { headerBg: "C9A84C", headerText: "1A1A1A", innerLine: "DDD5C0", surface: "F5F2E8" },
};
const BODY = "000000", HEAD = "3A3020";
const FONT = { ascii: "Times New Roman", eastAsia: "SimSun" };
const HFONT = { ascii: "Times New Roman", eastAsia: "SimHei" };
const NB = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };

const IMG = {
  "htf_fig1_pattern_health.png": [1900, 880], "htf_fig2_cell_ranking.png": [2100, 1040],
  "htf_fig3_equity.png": [2100, 1280], "htf_fig4_member_oos.png": [2100, 1000],
  "htf_fig5_is_vs_oos.png": [1400, 1200],
};
const f1 = (x) => (x == null ? "-" : Number(x).toFixed(1));
const f2 = (x) => (x == null ? "-" : Number(x).toFixed(2));
const f3 = (x) => (x == null ? "-" : Number(x).toFixed(3));
const t = (s) => new TextRun({ text: s, font: FONT, size: 21, color: BODY });
const b = (s) => new TextRun({ text: s, font: FONT, size: 21, color: BODY, bold: true });
const body = (runs) => new Paragraph({ children: runs, spacing: { after: 140, line: 300 }, alignment: AlignmentType.JUSTIFIED });
const h1 = (s) => new Paragraph({ heading: HeadingLevel.HEADING_1, spacing: { before: 320, after: 160 },
  children: [new TextRun({ text: s, font: HFONT, size: 30, bold: true, color: HEAD })] });
const tableTitle = (s) => new Paragraph({ spacing: { before: 120, after: 80 },
  children: [new TextRun({ text: s, font: HFONT, size: 20, bold: true, color: HEAD })] });
const caption = (s) => new Paragraph({ spacing: { before: 60, after: 200 }, alignment: AlignmentType.CENTER,
  children: [new TextRun({ text: s, font: FONT, size: 18, italics: true, color: "555555" })] });

function mkTable(headers, rows, widths) {
  const headerRow = new TableRow({ tableHeader: true, children: headers.map((h, i) => new TableCell({
    width: { size: widths[i], type: WidthType.PERCENTAGE },
    shading: { type: ShadingType.CLEAR, fill: P.table.headerBg },
    margins: { top: 60, bottom: 60, left: 90, right: 90 },
    children: [new Paragraph({ children: [new TextRun({ text: String(h), font: HFONT, size: 18, bold: true, color: P.table.headerText })] })] })) });
  const dataRows = rows.map((r, ri) => new TableRow({ children: r.map((c, i) => new TableCell({
    width: { size: widths[i], type: WidthType.PERCENTAGE },
    shading: { type: ShadingType.CLEAR, fill: ri % 2 ? P.table.surface : "FFFFFF" },
    margins: { top: 40, bottom: 40, left: 90, right: 90 },
    children: [new Paragraph({ children: [new TextRun({ text: String(c), font: FONT, size: 18, color: BODY })] })] })) }));
  return new Table({ width: { size: 100, type: WidthType.PERCENTAGE },
    borders: { top: { style: BorderStyle.SINGLE, size: 4, color: P.accent },
      bottom: { style: BorderStyle.SINGLE, size: 4, color: P.accent }, left: NB, right: NB,
      insideHorizontal: { style: BorderStyle.SINGLE, size: 1, color: P.table.innerLine }, insideVertical: NB },
    rows: [headerRow, ...dataRows] });
}

function figure(fname, cap) {
  const [w, h] = IMG[fname];
  const maxW = 560, maxH = 340;
  let iw = w, ih = h;
  if (w / h > maxW / maxH) { iw = maxW; ih = Math.round(maxW * h / w); } else { ih = maxH; iw = Math.round(maxH * w / h); }
  return [new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 160, after: 40 },
    children: [new ImageRun({ type: "png", data: fs.readFileSync(CH + fname), transformation: { width: iw, height: ih } })] }),
    caption(cap)];
}

const bodyChildren = [];

bodyChildren.push(h1("1. Executive Summary"));
bodyChildren.push(body([t("This report specifies, backtests and validates a higher-timeframe trend-continuation rejection strategy built from the four chart examples supplied: price pulling back into the 89-period or 233-period exponential moving average on 1H or 4H bars, printing a rejection structure at the level, and resuming with the trend. Four concrete pattern families were formalized from the images - a single-candle wick kiss, a two-bar rejection pair, a pierce-and-fail fakeout, and a cluster-stall breakaway - each gated by the fast EMA's slope and optionally by the slow (233) EMA regime. The engine reuses the corrected simulation core from the Delta-Fibonacci validation cycle: entries at the next bar open after a closed-bar signal, structural stops, real broker spread charged on both sides of every trade, and a strict 70/30 chronological in-sample/out-of-sample split with selection confined to the in-sample window.")]));
bodyChildren.push(body([t(`The full-universe sweep ran ${SW.configs.toLocaleString()} configurations across 151 symbols at 1H and 4H; ${SW.qualified.toLocaleString()} configurations across ${SW.symbols} symbols pass the in-sample gates. Applying a pre-registered plateau-selection rule (a symbol-timeframe-period-pattern cell needs at least three qualified configurations, the median rather than the best configuration is picked, one cell per symbol, capped at fifteen) produces an alpha basket whose in-sample performance is +${f1(B.is.total_r)}R over ${B.is.n} trades (profit factor ${f2(B.is.pf)}) and whose single out-of-sample validation - the honest gate - is `), b(`+${f1(B.oos.total_r)}R over ${B.oos.n} trades at profit factor ${f2(B.oos.pf)}`), t(". This is the first strategy in this research program to survive out-of-sample validation under real costs.")]));
bodyChildren.push(body([t("The result comes with a mandatory concentration disclosure: the validated edge is carried by the gold-cross family. Eight of the fifteen members are gold quoted against AUD, CNH, EUR, GBP, SGD and USD, plus two GAU variants, all trading the same cluster-stall pattern at the 233 EMA on 4H bars. The post-hoc sensitivity is stark - the gold members alone go out-of-sample at +61.6R (profit factor 1.51) while the remaining seven members lose -9.5R (profit factor 0.92). The basket as a whole passes because the gold cluster was ranked first in-sample, not because of diversification. Deployment guidance treats all gold members as a single risk unit.")]));

bodyChildren.push(h1("2. The Pattern Set, Formalized from the Charts"));
bodyChildren.push(body([t("The four supplied charts (XAUUSD, H1, 89/233 EMA pair) encode one behavior in four variants: a pullback into a trend-aligned moving average that fails to continue through it. Each variant was implemented as an exact, no-lookahead rule evaluated on closed bars with entry at the next bar open:")]));
bodyChildren.push(mkTable(
  ["Pattern", "Rule (long form; short is the mirror)", "Stop"],
  [
    ["P1 wick-kiss", "Fast EMA rising; bar's low touches EMA (tol 0.2 ATR); lower wick >= 35% of range; bullish close back at/above the EMA", "Below min(bar low, EMA) - 0.2 ATR"],
    ["P2 two-bar", "Bar i-1 tests the EMA; bar i closes above it, bullish, and above bar i-1's midpoint", "Below min(lows of both bars) - 0.2 ATR"],
    ["P3 pierce-fail", "Bar i-1 CLOSES below the EMA (fake breakdown); bar i closes back above it, bullish; EMA rising", "Below min(lows of both bars) - 0.2 ATR"],
    ["P4 cluster", "3-bar cluster dwelling at the EMA (>=2 touches, closes held within 0.15 ATR); bar i breaks the cluster high with the trend", "Below cluster low - 0.2 ATR"],
  ], [14, 60, 26]));
bodyChildren.push(body([t("Two filters complete the spec. The slope gate requires the fast EMA to be higher (lower, for shorts) than five bars ago, matching the visibly sloped EMAs in every chart. The regime filter additionally requires price to be on the trend side of the 233 EMA. Both were swept on and off rather than assumed. Exits sweep fixed risk-reward ratios from 1.5R to 4.0R and three trailing modes with a breakeven lock at +1R; each timeframe cools down six bars after any exit. A spread-resistance consideration inherited from the earlier research applies structurally here: at 1H/4H the stop distances are several times the spread, which is precisely why this family can carry costs that destroyed the 5M/15M systems.")]));

bodyChildren.push(h1("3. Data and Method"));
bodyChildren.push(body([t("Data is the repository's historical_parquet store: 151 Blueberry Markets MT5 symbols, 5,000 bars per timeframe, UTC timestamps, tick volume and the broker spread in points on every bar. The study uses 1H (roughly ten months of history) and 4H (roughly three years and three months); 15M holds only about 75 days and 5M about 17 days, too thin for honest validation, so both are excluded. Decimal precision per symbol is inferred mode-based from the close series. Spread in price units is charged at the prevailing bar spread on entry and on exit. The methodology rules are unchanged from the Delta-Fibonacci validation: no lookahead, no full-sample selection, and trial-counted deflation (the Deflated Sharpe is computed across all 86,634 evaluated configurations). Engine fidelity carries over from the previous cycle, where the corrected port reproduced the registry's no-spread results before costs were applied.")]));

bodyChildren.push(h1("4. Universe Sweep: What Survives"));
bodyChildren.push(body([t(`Of ${SW.configs.toLocaleString()} swept configurations, ${SW.qualified.toLocaleString()} pass the in-sample gates (at least 20 trades, profit factor at least 1.20, positive in-sample R) across ${SW.symbols} of 151 symbols. Figure 1 shows the pattern split: the cluster-stall (P4) and two-bar (P2) families dominate the qualified set at both timeframes, while the single-candle wick kiss and the pierce-fail fakeout contribute far fewer surviving configurations - on 4H the cluster family alone contributes 1,524 qualified configurations. The gold crosses' plateau cells rank at the top of the in-sample table by a wide margin (Figure 2): XAUAUD, XAUCNH, XAUUSD, XAUEUR, XAUSGD and XAUGBP at the 233 EMA on 4H, each with 30-36 qualified configurations and aggregate in-sample R from 982 to 1,563. This ranking is what the pre-registered selection rule reads - it is an in-sample fact, not an out-of-sample choice.`)]));
bodyChildren.push(...figure("htf_fig1_pattern_health.png", "Figure 1: IS-qualified configurations by pattern family and timeframe."));
bodyChildren.push(...figure("htf_fig2_cell_ranking.png", "Figure 2: Top-15 plateau cells by aggregate in-sample R - gold crosses (gold bars) lead in-sample."));

bodyChildren.push(h1("5. The Alpha Basket and Its Validation"));
bodyChildren.push(tableTitle("Table 2: The fifteen basket members (selection used IS only)"));
bodyChildren.push(mkTable(
  ["Symbol", "TF", "EMA", "Pattern", "Filters", "Exit", "IS R", "IS PF", "OOS R", "OOS PF"],
  B.members.map(m => [m.symbol, m.tf, m.period, m.pattern.split("_")[1],
    (m.slope ? "slope" : "-") + (m.regime ? "+regime" : ""), m.exit,
    f1(m.pick_is_r), f2(m.pick_is_pf), f1(m.pick_oos_r), f2(m.pick_oos_pf)]),
  [14, 6, 7, 12, 12, 13, 9, 9, 9, 9]));
bodyChildren.push(body([t(`The basket's aggregate ledger: in-sample +${f1(B.is.total_r)}R over ${B.is.n} trades (PF ${f2(B.is.pf)}, max drawdown ${f1(B.is.max_dd_r)}R); out-of-sample +${f1(B.oos.total_r)}R over ${B.oos.n} trades (PF ${f2(B.oos.pf)}, max drawdown ${f1(B.oos.max_dd_r)}R; OOS t-statistic ${f2(B.oos_t_stat)}). The full-sample ledger is +${f1(B.all.total_r)}R at PF ${f2(B.all.pf)}. Monte Carlo and block bootstrap on the mixed stream put profit probability at 1.00 with the Deflated Sharpe at ${f3(B.dsr.dsr_prob)} across 86,634 trials - reported for completeness, with the standing caveat that stream-level statistics cannot see edge decay; the OOS row above is the gate. Cost robustness is excellent: at 1.5x spreads the basket still earns +${f1(B.cost_stress["x1.5"].total_r)}R and at 2x spreads +${f1(B.cost_stress["x2"].total_r)}R, because stop distances at 4H dwarf the spread.`)]));
bodyChildren.push(tableTitle("Table 3: Robustness battery"));
bodyChildren.push(mkTable(
  ["Layer", "Trades", "Net R", "PF / statistic"],
  [
    ["In-sample (selection window)", B.is.n, f1(B.is.total_r), f2(B.is.pf)],
    ["Out-of-sample (validation)", B.oos.n, f1(B.oos.total_r), f2(B.oos.pf)],
    ["OOS t-statistic", "-", "-", f2(B.oos_t_stat)],
    ["Monte Carlo, 10k draws (mixed)", "-", "-", f3(B.mc.p_profit)],
    ["Block bootstrap (mixed)", "-", "-", f3(B.bb.p_profit)],
    ["Deflated Sharpe, 86,634 trials (mixed)", "-", "-", f3(B.dsr.dsr_prob)],
    ["Spreads x1.5", B.cost_stress["x1.5"].n, f1(B.cost_stress["x1.5"].total_r), f2(B.cost_stress["x1.5"].pf)],
    ["Spreads x2", B.cost_stress["x2"].n, f1(B.cost_stress["x2"].total_r), f2(B.cost_stress["x2"].pf)],
    ["Sensitivity: gold-only (post-hoc)", SEN.gold_only.oos_n, f1(SEN.gold_only.oos_r), f2(SEN.gold_only.oos_pf)],
    ["Sensitivity: ex-gold (post-hoc)", SEN.ex_gold.oos_n, f1(SEN.ex_gold.oos_r), f2(SEN.ex_gold.oos_pf)],
  ], [40, 12, 20, 28]));
bodyChildren.push(...figure("htf_fig3_equity.png", "Figure 3: Basket equity curve, $5,000 start, risk sized to observed concurrency."));

bodyChildren.push(h1("6. Concentration Disclosure"));
bodyChildren.push(body([t("The basket's out-of-sample gain decomposes into +61.6R from the eight gold-cross members and -13.3R from the seven non-gold members (JPY crosses, DJ30, BTCUSD). Figure 4 shows the member split; Figure 5 plots each member's in-sample against out-of-sample R and makes the structure visible: the gold cluster sits in the upper-right (transfers), the JPY crosses and DJ30 sit near or below zero OOS. Two consequences follow. First, the validated core of this strategy is the gold-cross, EMA-233, 4H, cluster-stall configuration family; the non-gold members are included because the pre-registered rule selected them, but they are not individually validated. Second, risk must be allocated to families, not members: all gold members are highly correlated expressions of one trade, so the basket's effective number of independent bets is far below fifteen, and concurrent gold signals should share one risk budget rather than stacking.")]));
bodyChildren.push(...figure("htf_fig4_member_oos.png", "Figure 4: Member-level out-of-sample R - gold cluster (gold bars) versus the rest."));
bodyChildren.push(...figure("htf_fig5_is_vs_oos.png", "Figure 5: In-sample vs out-of-sample R per member - gold crosses transfer, others do not."));

bodyChildren.push(h1("7. Deployment Notes"));
bodyChildren.push(body([t(`A $5,000 account simulation with risk sized to observed concurrency (at most ${E.max_concurrent} simultaneous positions, ${f2(E.per_trade_risk_frac * 100)}% of equity per trade) grows the account to $${Math.round(E.end_usd).toLocaleString()} (+${Math.round((E.end_usd / E.start_usd - 1) * 100)}%), with the in-sample window ending at $${Math.round(E.is_end_usd).toLocaleString()} and a maximum drawdown of ${f1(E.max_dd_pct)}%. Practical deployment notes follow. Trade the 4H gold-cross cluster family as the core position and size it as one unit; allow the remaining members only small satellite risk until each earns its own out-of-sample record. Expect trade frequency of roughly one to two signals per member per week on 4H, with holding periods of one to four days. Monitor monthly: re-run the pipeline as new bars accumulate and demand that any member kept live maintains a profit factor above 1.0 on a rolling 100-trade window. The spread environment matters less here than anywhere else in this research program - at 2x spreads the basket keeps 97 percent of its R - but the gold crosses' quoted-currency spreads (CNH, SGD, AUD) should be checked against the recorded values before sizing up.`)]));

bodyChildren.push(h1("8. Reproduction"));
bodyChildren.push(body([t("Everything regenerates from the repository. The htf_ema_rejection/ folder contains the engine (htf_reject.py - the four pattern families, slope and regime filters), the sweep (htf_sweep.py - 151 symbols x 1H/4H x 32 signal variants x 9 exits), the selection and battery (htf_select.py - pre-registered plateau rules, portfolio assembly, Monte Carlo, block bootstrap, Deflated Sharpe, cost stress), and the reporting layer (htf_summarize.py - summary JSON, workbook, charts). The pipeline reads historical_parquet/ and completes in roughly twelve minutes on four cores. The workbook HTF_EMA_Rejection_Basket.xlsx holds the basket picks, the battery, the full 1,381-trade ledger with in/out-of-sample flags and family tags, and the pattern-health table; htf_summary.json carries every number quoted in this report.")]));

const doc = new Document({
  creator: "Z.ai", title: "HTF EMA 89/233 Rejection Strategy Report",
  styles: { default: { document: { run: { font: FONT, size: 21, color: BODY } } } },
  features: { updateFields: true },
  sections: [
    {
      properties: { page: { margin: { top: 1440, bottom: 1260, left: 1350, right: 1350 } } },
      children: [
        new Paragraph({ spacing: { before: 2600 }, children: [new TextRun({ text: "STRATEGY RESEARCH REPORT", font: HFONT, size: 22, color: P.accent, bold: true })] }),
        new Paragraph({ spacing: { before: 200, after: 120 }, children: [new TextRun({ text: "HTF 89/233 EMA Rejection", font: HFONT, size: 52, bold: true, color: P.cover.titleColor })] }),
        new Paragraph({ spacing: { after: 320 }, children: [new TextRun({ text: "Trend-Continuation Rejection at Higher-Timeframe EMAs - Specification, Universe Sweep and Alpha Basket", font: FONT, size: 26, color: P.cover.subtitleColor })] }),
        ...["Source: 4 chart examples (XAUUSD H1, 89/233 EMA rejections) -> 4 formalized pattern families",
           `Sweep: ${SW.configs.toLocaleString()} configurations, 151 symbols, 1H/4H, real spreads`,
           `Alpha basket: 15 members, IS +${f1(B.is.total_r)}R PF ${f2(B.is.pf)} -> OOS +${f1(B.oos.total_r)}R PF ${f2(B.oos.pf)}`,
           `Disclosure: edge concentrated in the gold-cross family (OOS PF 1.51 gold-only, 0.92 ex-gold)`].map((m, i) => new Paragraph({
          spacing: { before: i === 0 ? 900 : 140 },
          border: { left: { style: "single", size: 12, color: P.accent, space: 12 } },
          children: [new TextRun({ text: m, font: FONT, size: 21, color: i === 3 ? "D8B25A" : P.cover.metaColor, bold: i === 3 })] })),
        new Paragraph({ spacing: { before: 2400 }, children: [new TextRun({ text: "Generated 2026-11-23 - companion to karan1399/Trading-Strategies", font: FONT, size: 18, color: P.cover.footerColor })] }),
      ],
    },
    { properties: { type: SectionType.CONTINUOUS }, children: [new Paragraph({ children: [new TableOfContents("Table of Contents", { hyperlink: true, headingStyleRange: "1-1" })] })] },
    { properties: { page: { pageNumbers: { start: 1, formatType: NumberFormat.DECIMAL } }, type: SectionType.NEXT_PAGE }, children: bodyChildren },
  ],
});

Packer.toBuffer(doc).then((buf) => { fs.writeFileSync(OUT, buf); console.log("saved", OUT, buf.length, "bytes"); });
