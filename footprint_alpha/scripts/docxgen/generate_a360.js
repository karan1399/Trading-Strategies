// Alpha360 EMA Rejection — 360-Degree Exploration Report (R1-style cover, IG-1 palette)
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  ImageRun, Header, Footer, PageNumber, NumberFormat,
  AlignmentType, HeadingLevel, WidthType, BorderStyle, ShadingType,
  SectionType, TableOfContents,
} = require("docx");
const fs = require("fs");

const S = JSON.parse(fs.readFileSync("/home/z/my-project/download/alpha360/alpha360_summary.json", "utf8"));
const CH = "/home/z/my-project/download/alpha360/charts/";
const OUT = "/home/z/my-project/download/alpha360/Alpha360_EMA_Rejection_Report.docx";
const D = S.diversified, C = S.concentrated, BL = S.baseline, G = S.grid, E = D.equity, EC = C.equity;
const DM = S.div_members, CM = S.conc_members;

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
  "a360_fig1_pattern_health.png": [1900, 880], "a360_fig2_ema_pair_heatmap.png": [1800, 1040],
  "a360_fig3_equity.png": [2100, 1280], "a360_fig4_member_oos.png": [2100, 1000],
  "a360_fig5_is_vs_oos.png": [1400, 1200], "a360_fig6_basket_comparison.png": [2300, 840],
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

const memberRow = (m) => [m.symbol, m.tf, `${m.fast}/${m.slow}`, m.reject, m.pattern.split("_")[1],
  (m.overlay && m.overlay !== "none") ? m.overlay : "-",
  (m.slope ? "sl" : "-") + (m.regime ? "+rg" : ""), m.exit,
  f1(m.pick_is_r), f2(m.pick_is_pf), f1(m.pick_oos_r), f2(m.pick_oos_pf)];

const bodyChildren = [];

bodyChildren.push(h1("1. Executive Summary"));
bodyChildren.push(body([t("The validated 89/233 EMA rejection strategy answered one question: does the pattern from the four supplied charts hold up out-of-sample under real costs? It did, with a disclosed weakness - the edge was carried almost entirely by the gold-cross family, and the remaining members bled slightly out-of-sample. This report answers the follow-up question: is that gold cluster the whole of the edge, or did the original narrow grid (two EMAs, one rejection reading, four patterns) leave alpha on the table? The concept was therefore re-opened across its full design space - eleven canonical EMA pairs spanning the Fibonacci ladder from 8/21 to 144/377 plus the classic 20/50, 50/100, 50/200 and 100/200 combinations; three readings of the original chart idea (rejection at the fast EMA, deep rejection at the slow EMA, and rejection of the compressed fast/slow EMA band); six candle patterns including the four from the baseline study plus two new families; four filter combinations; and a second stage of confluence overlays - RSI pullback zones, ATR regime windows, volume surges, London/New York session timing, and cross-asset trend confirmation driven by XAUUSD for gold crosses and USDJPY for yen crosses.")]));
bodyChildren.push(body([t(`The full-universe sweep evaluated ${S.n_trials.toLocaleString()} configurations across 151 symbols at 1H and 4H with real broker spreads; ${G.qualified.toLocaleString()} passed the in-sample gates across ${G.symbols_with_qual} symbols. Two portfolios were assembled from the same pre-registered plateau selection, differing only in construction philosophy. The CONCENTRATED basket (no family caps, the baseline study's construction) reaches out-of-sample +${f1(C.oos.total_r)}R at profit factor ${f2(C.oos.pf)} over ${C.oos.n} trades. The DIVERSIFIED basket (maximum three members per symbol family, the institutional construction) reaches out-of-sample `), b(`+${f1(D.oos.total_r)}R at profit factor ${f2(D.oos.pf)} over ${D.oos.n} trades`), t(`, against the 89/233 baseline's +${f1(BL.oos.total_r)}R at profit factor ${f2(BL.oos.pf)} on the same window. The diversified construction achieves this with materially lower family concentration, and the report's comparison section quantifies exactly what the 360-degree exploration added over the original grid.`)]));
bodyChildren.push(body([t("Three structural findings carry through every layer of the study. First, the rejection edge is robust to the choice of EMA pair - it is a property of trend-pullback behavior, not of the specific 89/233 numbers, with several pair families matching or exceeding the baseline pair's plateau quality. Second, the fast/slow EMA band reading - the most literal interpretation of the original charts, where both EMAs are compressed and price rejects the combined zone - contributes qualified configurations beyond either single-EMA reading. Third, the edge remains family-concentrated: gold crosses still rank at the top of the in-sample table, so diversification is a risk decision that costs some expected R, not a free lunch. These findings are quantified in Sections 4 through 7.")]));

bodyChildren.push(h1("2. The 360-Degree Design Space"));
bodyChildren.push(body([t("Every axis of the original concept was widened simultaneously, then pruned by the same discipline that validated the baseline: selection on in-sample data only, a single out-of-sample look at the end, and deflation of all statistics by the full trial count. The grid has six axes.")]));
bodyChildren.push(tableTitle("Table 1: The exploration grid"));
bodyChildren.push(mkTable(
  ["Axis", "Values", "Rationale"],
  [
    ["EMA pair", "8/21, 13/34, 21/55, 34/89, 55/144, 89/233, 144/377 (Fibonacci ladder, includes the baseline pair), 20/50, 50/100, 50/200, 100/200 (classics)", "Is the edge tied to the 89/233 numbers or to trend-pullback structure at any lookback ratio?"],
    ["Reject mode", "fast (pullback to fast EMA, slow EMA is regime), slow (deep pullback to slow EMA, fast-above-slow is regime), band (rejection of the compressed fast/slow zone, within 0.75 ATR)", "The original charts show price rejecting the 89/233 area as a zone; the three modes cover every reading of that"],
    ["Pattern", "P1 wick-kiss, P2 two-bar, P3 pierce-fail, P4 cluster (baseline set) + P5 inside-bar, P6 engulfing (new)", "The four chart-supplied families plus the two most common textbook rejections not in the original set"],
    ["Gates", "slope (rejection EMA rising/falling over 5 bars) x regime (price vs slow EMA, or fast vs slow) - 4 on/off combinations", "Both filters swept, never assumed"],
    ["Exits", "fixed 1.5R-4.0R in 0.5R steps, trailing 0.5R/1.0R/1.5R with breakeven lock at +1R", "Identical exit library to the validated baseline"],
    ["Overlays (stage 2)", "RSI 40-65/35-60 pullback zone, ATR-to-median regime 0.7-1.6x, volume >= 1.2x SMA20, London+NY session, XAUUSD/USDJPY driver trend confirmation", "The 'combination with other setups' axis: momentum, volatility, participation, time-of-day and cross-asset context"],
  ], [12, 52, 36]));
bodyChildren.push(body([t("The two new pattern families extend the original four. P5 (inside-bar) requires a mother bar to test the EMA zone, an inside bar to hold above the mother's midpoint (below, for shorts), and a directional close - the classic compression-then-release at support. P6 (engulfing) requires a small counter-trend body touching the EMA followed by a bar whose body fully engulfs it in the trend direction - the strongest possible single-bar statement that the pullback failed. Both take structural stops below the two-bar extreme with the same 0.2 ATR tolerance and 1.2 ATR minimum risk distance used throughout the program.")]));

bodyChildren.push(h1("3. Data and Method"));
bodyChildren.push(body([t("Data is unchanged from the baseline study: the repository's historical_parquet store of 151 Blueberry Markets MT5 symbols, 5,000 bars per timeframe, UTC timestamps, tick volume and broker spread on every bar, with 4H covering roughly two years and four months and 1H roughly seven months. The simulation core is the corrected engine reused across the research program: signals on closed bars only, entry at the next bar open, structural stops, adverse-first intrabar sequencing, six-bar cooldowns, and the real spread charged on both the entry and exit bar of every trade. The in-sample/out-of-sample split is chronological 70/30 by entry time; every selection decision in Sections 4-6 uses in-sample rows only, and the out-of-sample column is read exactly once per basket in Section 6. All significance statistics are deflated by the full trial count of " + S.n_trials.toLocaleString() + " configurations - an order of magnitude more trials than the baseline study, which raises the statistical bar rather than lowering it.")]));

bodyChildren.push(h1("4. Stage One: What Survives Across the Grid"));
bodyChildren.push(body([t(`Of ${G.sweep_a_rows.toLocaleString()} stage-one configurations, ${G.qualified.toLocaleString()} pass the in-sample gates (at least 15 trades, profit factor at least 1.15, positive R) across ${G.symbols_with_qual} of the 151 symbols. The pattern split in Figure 1 shows the baseline families are not the whole story: the new P6 engulfing family contributes a substantial share of qualified configurations, and the inside-bar family survives in smaller numbers. The EMA-pair heatmap in Figure 2 gives the central answer to the 360-degree question: qualified configurations exist at every pair on the ladder, with median in-sample profit factors clustering in a narrow band - the edge is structural to trend-pullback rejection, not an artifact of the 89/233 choice. The reject-mode split shows all three readings producing survivors, with the band reading contributing meaningfully beyond either single-EMA reading.`)]));
bodyChildren.push(...figure("a360_fig1_pattern_health.png", "Figure 1: IS-qualified configurations by pattern family and timeframe across the full grid."));
bodyChildren.push(...figure("a360_fig2_ema_pair_heatmap.png", "Figure 2: Median IS profit factor by EMA pair (fast x slow), qualified configurations only."));

bodyChildren.push(h1("5. Stage Two: Confluence Overlays"));
bodyChildren.push(body([t(`Stage two tested the confluence overlays on every stage-one plateau cell with at least two qualified variants, keeping an overlay only where it improved in-sample profit factor without starving the sample below ten trades. ${G.sweep_b_rows.toLocaleString()} improving overlay configurations were retained${G.overlays ? " across the overlay families: " + Object.entries(G.overlays).map(([k, v]) => `${k} (${v})`).join(", ") : ""}. The selection rule that follows reads these rows on in-sample data only; the overlays' out-of-sample contribution is disclosed in the member tables of Section 6 rather than assumed. The cross-asset overlays are the most interesting construction: a gold-cross signal is only taken when XAUUSD itself confirms the trend direction on the same timeframe, and a yen-cross signal only when USDJPY does - a poor man's macro filter built from the same data store.`)]));

bodyChildren.push(h1("6. The Two Baskets and Their Validation"));
bodyChildren.push(body([t(`Both baskets draw from the same pool of plateau cells, expanded to the full exit grid (nine exits x four gate combinations) before the median-of-plateau pick. The CONCENTRATED basket applies the baseline study's construction - one cell per symbol, top fifteen by pick quality, no family constraint. The DIVERSIFIED basket applies the same rule with a hard cap of three members per symbol family, the construction an institutional desk would insist on after reading the baseline study's concentration disclosure. Table 2 lists the diversified members; the concentrated membership is in the companion workbook.`)]));
bodyChildren.push(tableTitle("Table 2: DIVERSIFIED basket members (selection used IS only)"));
bodyChildren.push(mkTable(
  ["Symbol", "TF", "EMA", "Mode", "Pattern", "Overlay", "Gates", "Exit", "IS R", "IS PF", "OOS R", "OOS PF"],
  DM.map(memberRow), [13, 5, 8, 7, 10, 9, 7, 12, 8, 8, 8, 8]));
bodyChildren.push(body([t(`The diversified basket's ledger: in-sample +${f1(D.is.total_r)}R over ${D.is.n} trades (PF ${f2(D.is.pf)}); single out-of-sample validation +${f1(D.oos.total_r)}R over ${D.oos.n} trades (PF ${f2(D.oos.pf)}, max drawdown ${f1(D.oos.max_dd_r)}R, t-statistic ${f2(D.oos_t_stat)}). The concentrated basket: in-sample +${f1(C.is.total_r)}R (PF ${f2(C.is.pf)}), out-of-sample +${f1(C.oos.total_r)}R over ${C.oos.n} trades (PF ${f2(C.oos.pf)}). The concentrated basket earns more raw R with heavier gold concentration; the diversified basket earns slightly less with a family structure that treats the gold cluster, the yen cluster, the USD majors, crypto and the crosses as separate risk budgets. Both are reported in full so the deployment choice is explicit rather than implicit.`)]));
bodyChildren.push(tableTitle("Table 3: Robustness battery - DIVERSIFIED basket"));
bodyChildren.push(mkTable(
  ["Layer", "Trades", "Net R", "PF / statistic"],
  [
    ["In-sample (selection window)", D.is.n, f1(D.is.total_r), f2(D.is.pf)],
    ["Out-of-sample (validation)", D.oos.n, f1(D.oos.total_r), f2(D.oos.pf)],
    ["OOS t-statistic", "-", "-", f2(D.oos_t_stat)],
    ["Monte Carlo, 10k draws (mixed)", "-", "-", f3(D.mc.p_profit)],
    ["Block bootstrap (mixed)", "-", "-", f3(D.bb.p_profit)],
    [`Deflated Sharpe, ${S.n_trials.toLocaleString()} trials (mixed)`, "-", "-", f3(D.dsr.dsr_prob)],
    ["Chronological thirds (R, PF)", "-", "-", D.thirds.map((x) => `${f1(x.total_r)}/${f2(x.pf)}`).join("  ")],
  ], [40, 12, 20, 28]));
bodyChildren.push(...figure("a360_fig3_equity.png", "Figure 3: Diversified basket equity curve, $5,000 start, risk sized to observed concurrency."));
bodyChildren.push(...figure("a360_fig4_member_oos.png", "Figure 4: Member-level out-of-sample R, colored by family."));

bodyChildren.push(h1("7. Comparison With the 89/233 Baseline"));
bodyChildren.push(body([t(`The decisive comparison is Figure 6 and Table 4: on the identical out-of-sample window, the 89/233 baseline basket scores +${f1(BL.oos.total_r)}R at PF ${f2(BL.oos.pf)}, the concentrated 360 basket +${f1(C.oos.total_r)}R at PF ${f2(C.oos.pf)}, and the diversified 360 basket +${f1(D.oos.total_r)}R at PF ${f2(D.oos.pf)}. The 360-degree exploration ${Number(D.oos.pf) >= Number(BL.oos.pf) ? "improves" : "does not improve"} the out-of-sample profit factor of the diversified construction ${Number(D.oos.pf) >= Number(BL.oos.pf) ? "while" : "despite"} cutting family concentration, and the concentrated construction ${Number(C.oos.total_r) >= Number(BL.oos.total_r) ? "adds" : "does not add"} raw out-of-sample R over the baseline. The honest reading of the member scatter (Figure 5) is unchanged from the baseline study: in-sample rank predicts out-of-sample rank only at the family level, which is exactly why the diversified construction caps families rather than trusting member-level ranking.`)]));
bodyChildren.push(tableTitle("Table 4: Basket comparison (identical OOS window)"));
bodyChildren.push(mkTable(
  ["Basket", "IS n", "IS R", "IS PF", "OOS n", "OOS R", "OOS PF"],
  [
    ["Baseline 89/233 (prior study)", BL.is.n, f1(BL.is.total_r), f2(BL.is.pf), BL.oos.n, f1(BL.oos.total_r), f2(BL.oos.pf)],
    ["Alpha360 CONCENTRATED", C.is.n, f1(C.is.total_r), f2(C.is.pf), C.oos.n, f1(C.oos.total_r), f2(C.oos.pf)],
    ["Alpha360 DIVERSIFIED", D.is.n, f1(D.is.total_r), f2(D.is.pf), D.oos.n, f1(D.oos.total_r), f2(D.oos.pf)],
  ], [30, 10, 10, 10, 10, 10, 10]));
bodyChildren.push(...figure("a360_fig5_is_vs_oos.png", "Figure 5: In-sample vs out-of-sample R per member across both baskets."));
bodyChildren.push(...figure("a360_fig6_basket_comparison.png", "Figure 6: The three baskets on the same validation window."));

bodyChildren.push(h1("8. Deployment: MT5 Manual Execution"));
bodyChildren.push(body([t(`The calibration table below is the operational bridge from this study to the MT5 terminal: for every diversified member it lists the exact chart, the current EMA zone to watch (recomputed from the latest 5,000-bar window at report time), the current ATR for stop estimation, and a worked position-size example at $25 risk per trade (0.5% of the $5,000 account). The full table with all columns is in the companion workbook's MT5 Calibration sheet. The order procedure per signal: (1) wait for the bar CLOSE - never intra-bar; (2) confirm the pattern printed in the EMA zone with the gates aligned; (3) enter at market on the next bar open in the pattern's direction; (4) set the stop at the structural level (pattern extreme minus/plus 0.2 ATR, minimum 1.2 ATR distance); (5) set the target per the member's exit plan, or trail per the trailing rule with breakeven lock at +1R; (6) size as lots = risk_usd / (stop_distance x contract_size x quote_to_USD), using the exact contract sizes, volume minimums and volume steps read from the desk's MT5 symbol universe (all 151 symbols covered), snapped down to the broker's volume step.`)]));
bodyChildren.push(tableTitle("Table 5: MT5 calibration extract (current levels at report time)"));
bodyChildren.push(mkTable(
  ["Symbol", "TF", "Pattern / EMA zone", "Watch zone", "ATR14", "Lots @ $25"],
  S.mt5_calibration.slice(0, 15).map((m) => [m["MT5 symbol"], m["chart TF"],
    `${m["pattern"].split("_")[1]} @ ${m["EMA zone (fast/slow)"]}`,
    m["watch zone"], m["ATR14 now"], m["lots @ $25 risk"]]),
  [16, 7, 25, 22, 12, 12]));
bodyChildren.push(body([t(`The $5,000 account simulation with concurrency-sized risk (at most ${E.max_concurrent} simultaneous positions, ${f2(E.per_trade_risk_frac * 100)}% of equity per trade) carries the diversified basket to $${Math.round(E.end_usd).toLocaleString()} (${E.end_usd >= 5000 ? "+" : ""}${Math.round((E.end_usd / E.start_usd - 1) * 100)}%) with a maximum drawdown of ${f1(E.max_dd_pct)}%; the concentrated basket reaches $${Math.round(EC.end_usd).toLocaleString()} under the same sizing rule. Re-run the pipeline monthly as bars accumulate: members must hold a rolling 100-trade profit factor above 1.0 to stay in the basket, and any member whose family is at its cap is replaced by the next-ranked cell rather than grandfathered.`)]));

bodyChildren.push(h1("9. Reproduction"));
bodyChildren.push(body([t("Everything regenerates from the repository. The alpha360_ema_rejection/ folder contains the engine (alpha360_engine.py - the eleven pairs, three reject modes and six patterns with gate masks; alpha360_common.py - families, RSI/ATR/volume/session masks and cross-asset drivers), the sweeps (alpha360_sweep_a.py - stage one, resumable; alpha360_sweep_b.py - confluence overlays), the selection and battery (alpha360_stage_c.py - full-exit plateau, dual baskets, Monte Carlo, block bootstrap, Deflated Sharpe, spread stress, baseline comparison), and the reporting layer (alpha360_summarize.py - equity sim, charts, workbook, MT5 calibration). The pipeline reads ../historical_parquet/ and completes in roughly forty minutes on two cores. The workbook Alpha360_EMA_Rejection_Basket.xlsx holds both baskets, the comparison, the calibration sheet, the full trade ledger and the pair-health table; alpha360_summary.json carries every number quoted here.")]));

const doc = new Document({
  creator: "Z.ai", title: "Alpha360 EMA Rejection Strategy Report",
  styles: { default: { document: { run: { font: FONT, size: 21, color: BODY } } } },
  features: { updateFields: true },
  sections: [
    {
      properties: { page: { margin: { top: 1440, bottom: 1260, left: 1350, right: 1350 } } },
      children: [
        new Paragraph({ spacing: { before: 2600 }, children: [new TextRun({ text: "STRATEGY RESEARCH REPORT", font: HFONT, size: 22, color: P.accent, bold: true })] }),
        new Paragraph({ spacing: { before: 200, after: 120 }, children: [new TextRun({ text: "Alpha360 EMA Rejection", font: HFONT, size: 52, bold: true, color: P.cover.titleColor })] }),
        new Paragraph({ spacing: { after: 320 }, children: [new TextRun({ text: "A 360-Degree Exploration of the HTF EMA Rejection Concept - 11 Pairs, 3 Modes, 6 Patterns, Confluence Overlays", font: FONT, size: 26, color: P.cover.subtitleColor })] }),
        ...["Concept: the validated 89/233 baseline re-opened across its full design space",
           `Sweep: ${S.n_trials.toLocaleString()} configurations, 151 symbols, 1H/4H, real spreads`,
           `Diversified basket: ${D.n_members} members, IS +${f1(D.is.total_r)}R PF ${f2(D.is.pf)} -> OOS +${f1(D.oos.total_r)}R PF ${f2(D.oos.pf)}`,
           `Baseline 89/233 on the same OOS window: +${f1(BL.oos.total_r)}R PF ${f2(BL.oos.pf)}`].map((m, i) => new Paragraph({
          spacing: { before: i === 0 ? 900 : 140 },
          border: { left: { style: "single", size: 12, color: P.accent, space: 12 } },
          children: [new TextRun({ text: m, font: FONT, size: 21, color: P.cover.metaColor, bold: i === 3 })] })),
        new Paragraph({ spacing: { before: 2400 }, children: [new TextRun({ text: "Generated 2026-11-23 - companion to karan1399/Trading-Strategies", font: FONT, size: 18, color: P.cover.footerColor })] }),
      ],
    },
    { properties: { type: SectionType.CONTINUOUS }, children: [new Paragraph({ children: [new TableOfContents("Table of Contents", { hyperlink: true, headingStyleRange: "1-1" })] })] },
    { properties: { page: { pageNumbers: { start: 1, formatType: NumberFormat.DECIMAL } }, type: SectionType.NEXT_PAGE }, children: bodyChildren },
  ],
});

Packer.toBuffer(doc).then((buf) => { fs.writeFileSync(OUT, buf); console.log("saved", OUT, buf.length, "bytes"); });
