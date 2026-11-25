// Delta-Fibonacci Independent Validation Report — docx generator (R1 cover, IG-1 palette)
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  ImageRun, PageBreak, Header, Footer, PageNumber, NumberFormat,
  AlignmentType, HeadingLevel, WidthType, BorderStyle, ShadingType,
  SectionType, TableLayoutType, TableOfContents,
} = require("docx");
const fs = require("fs");

const S = JSON.parse(fs.readFileSync("/home/z/my-project/download/dfib/dfib_summary.json", "utf8"));
const CH = "/home/z/my-project/download/dfib/charts/";
const OUT = "/home/z/my-project/download/dfib/Delta_Fibonacci_Validation_Report.docx";
const V = S.verification, PF = S.portfolio, E = PF.equity;

const P = {
  bg: "1A1A1A", accent: "C9A84C",
  cover: { titleColor: "FFFFFF", subtitleColor: "B0B8C0", metaColor: "90989F", footerColor: "687078" },
  table: { headerBg: "C9A84C", headerText: "1A1A1A", innerLine: "DDD5C0", surface: "F5F2E8" },
};
const BODY = "000000", HEAD = "3A3020";
const FONT = { ascii: "Times New Roman", eastAsia: "SimSun" };
const HFONT = { ascii: "Times New Roman", eastAsia: "SimHei" };
const NB = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };

const IMG_DIMS = {
  "dfib_fig1_claim_vs_corrected.png": [1900, 960], "dfib_fig2_cost_cliff.png": [1440, 1200],
  "dfib_fig3_pooled_health.png": [1900, 920], "dfib_fig4_portfolio_equity.png": [2100, 1280],
  "dfib_fig5_member_oos.png": [2100, 960],
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
  const [w, h] = IMG_DIMS[fname];
  const maxW = 560, maxH = 340;
  let iw = w, ih = h;
  if (w / h > maxW / maxH) { iw = maxW; ih = Math.round(maxW * h / w); } else { ih = maxH; iw = Math.round(maxH * w / h); }
  return [new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 160, after: 40 },
    children: [new ImageRun({ type: "png", data: fs.readFileSync(CH + fname), transformation: { width: iw, height: ih } })] }),
    caption(cap)];
}

const bodyChildren = [];

bodyChildren.push(h1("1. Executive Summary"));
bodyChildren.push(body([t("This report documents an independent, end-to-end backtest, optimization and robustness validation of the "), b("Delta-Fibonacci Institutional Business"), t(" trading system - the multi-dimensional VWAP standard-deviation band and multi-timeframe EMA candle-rejection strategy defined in the repository's design document and configured in its champion registry - against the repository's own historical_parquet data store. Every result charges the full broker spread recorded in the data on both sides of every trade, evaluates signals only on closed bars with entry at the next bar open, and separates a 70 percent chronological in-sample window from a 30 percent out-of-sample window that is never used for selection.")]));
bodyChildren.push(body([t("The headline claim in the repository's audit - 362 champion setups totalling roughly +30,900R - does not survive contact with realistic costs. Re-simulated under the corrected engine with spreads switched off, the same setups earn +5,105R with a median profit factor of 1.14, which confirms the engine port is faithful and reproduces the direction of the claim. With real broker spreads charged, the same setups lose "), b("-38,762R"), t(" with a median profit factor of 0.84, and only 126 of 362 remain positive. The system's raw edge before costs is real but thin, and spreads consume essentially all of it at 5M and 15M trade frequency.")]));
bodyChildren.push(body([t("Optimization was then attempted from scratch under strict discipline: a 10,350-configuration sweep on the 23 champion assets, 240 pooled same-config portfolio variants across two universes with cost gates and stop-distance floors, and a full-universe 151-symbol sweep at the deep 1H/4H timeframes yielding 27,090 evaluated configurations. The best construct anywhere in the study - a fifteen-symbol, plateau-stable portfolio selected strictly on in-sample data - earns +327R in-sample (profit factor 1.58) but "), b("-8.1R out-of-sample (profit factor 0.96)"), t(". No variant achieves profitability that survives out-of-sample validation under real costs. The recommendation is therefore explicit: "), b("do not launch this system on live capital"), t(". The complete validation infrastructure ships with this report so the verdict can be re-tested as new data accumulates.")]));

bodyChildren.push(h1("2. The System Under Test"));
bodyChildren.push(body([t("The repository's current strategy is the multi-dimensional framework specified in its 2026-11-19 design document. Two indicator families are swept per asset. The first is a "), b("session-anchored VWAP with +/-1 and +/-2 standard deviation bands"), t(": typical price is volume-weighted cumulatively from the session anchor (00:00 broker time, UTC+2, for FX and indices; 00:00 UTC for crypto; weekly anchors for 4H bars), and the bands are the cumulative volume-weighted standard deviation around the anchor. The second is a "), b("multi-timeframe EMA lattice"), t(" with periods 20 through 233. Entries are candle-rejection patterns at those levels: a wick kiss of the level, a close back on the reactive side, and a rejection wick of at least 35 percent of the bar range - extended by five multi-bar variants (two-bar reversal, three-bar liquidity sweep, cluster stall breakaway, double-tap retest, and the single-bar pinbar). Stops are structural (beyond the level plus 0.2 ATR, floored at 1.2 ATR), exits sweep fixed risk-reward ratios from 1.5R to 4.0R and three trailing modes with breakeven locks, and each timeframe cools down six bars after any exit.")]));
bodyChildren.push(body([t("The prior audit selected, for each of up to 98 symbols and four timeframes, the best-performing combination of family, parameter and exit mode - 362 champion setups in the registry - and aggregated them into the +8,176R basket report and later the expanded +30,938R claim. That selection used the full historical sample for both choosing and reporting, and the engine charged a flat $0.05 commission with no spread. Both choices systematically inflate results, and the repository's own README acknowledges the gap. This validation closes it.")]));

bodyChildren.push(h1("3. Data and Corrected Methodology"));
bodyChildren.push(body([t("The data is the repository's historical_parquet store: 151 Blueberry Markets MT5 symbols, 5,000 bars per timeframe, with UTC timestamps, OHLC, tick volume and the broker spread in points on every bar. Decimal precision per symbol is inferred from the dominant decimal grid of the close series (a mode-based inference; a max-based inference is unstable to floating-point artifacts and was deliberately avoided). The spread in price units is charged on entry and exit at the prevailing bar spread. The 5M timeframe is excluded from selection: 5,000 five-minute bars span roughly 17 days, which cannot support an honest in-sample/out-of-sample statement. The 15M window is roughly 75 days, 1H roughly ten months, and 4H roughly three years and three months - so 1H and 4H carry the statistical weight of this study.")]));
bodyChildren.push(body([t("Three methodology rules are locked throughout. First, no lookahead: signals evaluate on closed bars, entries fill at the next bar open, and intrabar sequencing checks the stop before the target on adverse bars. Second, no full-sample selection: every ranking, gate and portfolio choice uses the in-sample window only, and the out-of-sample window is opened once per stage for validation. Third, honest trial counting: the Deflated Sharpe Ratio is deflated across all 27,090 configurations evaluated in this study. Fidelity of the port was verified by reproducing the champion registry's direction with spreads off (median profit factor 1.14 versus their 1.17) before any new result was trusted.")]));

bodyChildren.push(h1("4. Champion Verification: the Claim Under Real Costs"));
bodyChildren.push(body([t("All 362 champion setups from the registry were re-simulated exactly as configured - same symbol, timeframe, family, parameter and exit mode - first with spreads off, then with real spreads. The aggregate is unambiguous: their recorded +30,938R becomes +5,105R without spreads (the residual difference is data depth: the registry was built on deeper local dumps, while the parquet store holds 5,000 bars per timeframe), and "), b("-38,762R with real spreads"), t(". The median setup profit factor falls from 1.17 (their numbers) to 1.14 (spreads off) to 0.84 (spreads on). Table 1 and Figures 1 and 2 summarize the cliff.")]));
bodyChildren.push(tableTitle("Table 1: The 362 champion setups - claimed vs corrected"));
bodyChildren.push(mkTable(
  ["Layer", "Total net R", "Trades", "Median PF", "Positive share"],
  [
    ["Their audit claim", `+${Math.round(V.their_total_r).toLocaleString()}R`, V.their_trades.toLocaleString(), f2(V.median_pf_theirs), "100% (by construction)"],
    ["Corrected engine, spreads OFF", `+${Math.round(V.ours_nospread_r).toLocaleString()}R`, "59,012", f2(V.median_pf_nospread), "80%"],
    ["Corrected engine, real spreads", `${Math.round(V.ours_spread_r).toLocaleString()}R`, "59,012", f2(V.median_pf_spread), `${Math.round(V.positive_share_spread * 100)}%`],
  ], [30, 20, 14, 16, 20]));
bodyChildren.push(...figure("dfib_fig1_claim_vs_corrected.png", "Figure 1: Claimed vs corrected aggregate performance of the 362 champion setups."));
bodyChildren.push(...figure("dfib_fig2_cost_cliff.png", "Figure 2: Per-setup net R without spreads (x) vs with real spreads (y). Most setups fall from modest profit to heavy loss."));
bodyChildren.push(body([t("The mechanics of the cliff are simple: a spread of s price units costs 2s per round trip, so a setup with stop distance d loses 2s/d of risk units on every trade regardless of outcome. On HK50 15-minute bars the median spread is 55 index points against a median 14-bar ATR of about 34 points, so every trade donates roughly two full risk units to the spread before any edge can express itself. Setups on gold micro-crosses and quiet JPY crosses at 1H/4H - where stops are wide relative to spread - are the only survivors, which is exactly what the best-10 table shows.")]));

bodyChildren.push(h1("5. Stage 1: Family Sweep on the 23 Champion Assets"));
bodyChildren.push(body([t("Before any re-selection, the strategy families were swept as designed on the 23 champion assets: VWAP and EMA families, all parameters, both pattern modes, nine exit modes, 10,350 configurations in total with real spreads. No family at any timeframe reaches a median profit factor of 1.0. The EMA family at 4H is the least bad (median PF 0.96), the VWAP family at 15M the worst (median PF 0.73, median total R -39). For calibration, the same grid without spreads produces 5,064 positive configurations of which only 3,197 survive spreads, and the median configuration's total R falls from -0.5 to -9.8 purely from costs. Figure 3 shows the pooled view.")]));
bodyChildren.push(tableTitle("Table 2: Stage 1 family health (real spreads, 23 champion assets)"));
bodyChildren.push(mkTable(
  ["Family", "Timeframe", "Configs", "Median PF", "Positive configs"],
  Object.entries(S.stage1_family).map(([k, v]) => [k.split("_")[0], k.split("_")[1], v.configs, f2(v.median_pf), v.positive]),
  [20, 16, 20, 20, 24]));
bodyChildren.push(...figure("dfib_fig3_pooled_health.png", "Figure 3: Pooled same-config portfolios by universe and family - no distribution reaches PF 1.0."));

bodyChildren.push(h1("6. Pooled Portfolios and Refinement Axes"));
bodyChildren.push(body([t("The most overfit-resistant form of the system is a single configuration traded identically across the whole universe - a pooled portfolio. All 240 pooled variants were tested: two universes (the 23 champions and the 59 FX symbols), four microstructure refinements (a cost gate that skips entries when the round-trip spread exceeds a third of the stop distance, and a wider 1.5-ATR stop floor, alone and combined), across the family grid. "), b("Zero variants pass the in-sample gates"), t(" of at least 100 trades and profit factor 1.15. The closest - EMA 200 on the champions with the cost gate - reaches in-sample PF 1.04 and then loses out-of-sample at PF 0.85. The refinements help directionally (the cost gate lifts the best pooled in-sample PF from 0.90 to 1.04) but cannot close a gap of this size. The FX-only universe performs worse than the champion universe, ruling out the hypothesis that cheaper instruments alone rescue the pooled system.")]));

bodyChildren.push(h1("7. Stage 4: Full-Universe Plateau Portfolio"));
bodyChildren.push(body([t("The final optimization pass swept all 151 symbols at the statistically deep 1H and 4H timeframes: 27,090 configurations, of which 1,932 across 104 symbols pass the in-sample gates (at least 25 trades, profit factor at least 1.20, positive total R). To select without cherry-picking, a pre-registered rule was applied: a symbol-timeframe-family cell qualifies only if at least three of its configurations pass the gates (a parameter plateau, evidence the edge is not a lone spike); within each cell the "), b("median"), t("-performing qualified configuration is picked rather than the best; and the portfolio takes one cell per symbol, capped at fifteen symbols ranked by cell-aggregate in-sample R. The resulting portfolio is dominated by gold crosses and JPY crosses at 4H - exactly the low-spread, wide-stop corner of the universe the verification predicted.")]));
bodyChildren.push(tableTitle("Table 3: The fifteen portfolio picks (selection used IS only)"));
bodyChildren.push(mkTable(
  ["Symbol", "TF", "Config", "Exit", "IS R", "IS PF", "OOS R", "OOS PF"],
  PF.members.map(m => [m.symbol, m.tf, `${m.param}${m.multivariant ? " mv" : ""}`, m.exit,
    f1(m.pick_is_r), f2(m.pick_is_pf), f1(m.pick_oos_r), f2(m.pick_oos_pf)]),
  [20, 8, 22, 16, 10, 10, 10, 8]));
bodyChildren.push(body([t("In-sample the portfolio earns "), b(`+${f1(PF.is.total_r)}R over ${PF.is.n} trades at profit factor ${f2(PF.is.pf)}`), t(". The single out-of-sample validation - the honest gate - earns "), b(`${f1(PF.oos.total_r)}R over ${PF.oos.n} trades at profit factor ${f2(PF.oos.pf)}`), t(` with a ${f1(PF.oos.max_dd_r)}R drawdown. Monte Carlo and block bootstrap on the mixed stream report 100 percent profit probability and the Deflated Sharpe Ratio computes at ${f3(PF.dsr.dsr_prob)} - all of which is why those statistics are reported here only alongside the OOS row that invalidates them: resampling a stream whose profit is concentrated in the selection window cannot detect that the edge stopped. The cost-stress rows degrade gracefully (PF ${f2(PF.cost_stress["x1.5"].pf)} at 1.5x spreads, ${f2(PF.cost_stress["x2"].pf)} at 2x), confirming this portfolio is spread-resistant - it simply has no surviving edge to protect.`)]));
bodyChildren.push(tableTitle("Table 4: Portfolio robustness battery"));
bodyChildren.push(mkTable(
  ["Layer", "Trades", "Net R", "PF / probability"],
  [
    ["In-sample (selection window)", PF.is.n, f1(PF.is.total_r), f2(PF.is.pf)],
    ["Out-of-sample (validation)", PF.oos.n, f1(PF.oos.total_r), f2(PF.oos.pf)],
    ["Monte Carlo, 10k draws (mixed stream)", "-", "-", f3(PF.mc.p_profit)],
    ["Block bootstrap (mixed stream)", "-", "-", f3(PF.bb.p_profit)],
    ["Deflated Sharpe, 27,090 trials (mixed)", "-", "-", f3(PF.dsr.dsr_prob)],
    ["Spreads x1.5", PF.cost_stress["x1.5"].n, f1(PF.cost_stress["x1.5"].total_r), f2(PF.cost_stress["x1.5"].pf)],
    ["Spreads x2", PF.cost_stress["x2"].n, f1(PF.cost_stress["x2"].total_r), f2(PF.cost_stress["x2"].pf)],
  ], [40, 14, 20, 26]));
bodyChildren.push(...figure("dfib_fig4_portfolio_equity.png", "Figure 4: Best-variant equity curve, $5,000 start, risk sized to observed concurrency (0.17% per trade, max 12 open)."));
bodyChildren.push(...figure("dfib_fig5_member_oos.png", "Figure 5: Member-level out-of-sample R - a coin-flip split totalling -8.1R."));

bodyChildren.push(h1("8. Why the Claim Failed"));
bodyChildren.push(body([t("Two mechanisms produced the +30,938R claim, and both are instructive. The first is cost omission: at 5M and 15M frequency the strategy trades hundreds of times per symbol, and a round-trip spread of even 0.2R per trade - typical for index CFDs and metals crosses at those timeframes - converts a thin pre-cost edge into a certain loss. The second is full-sample selection: choosing the best of thousands of configurations on the same data used for reporting guarantees inflated numbers even with zero costs, because with 27,090 configurations the extreme order statistics of pure noise are large. The registry's 100 percent positive share is the signature: real, selection-free systems always contain losers. The honest expectation for this family, measured on data never used for selection, is a profit factor near 0.96 - indistinguishable from break-even minus costs.")]));

bodyChildren.push(h1("9. Verdict and Recommendations"));
bodyChildren.push(body([t("Verdict: "), b("the Delta-Fibonacci multi-dimensional VWAP/EMA rejection system is not profitable after real broker spreads on this data, in any configuration this study could find"), t(", and no variant is recommended for live deployment. The claim in the repository's audit is an artifact of zero-cost simulation and full-sample selection, and the repository's own README warning to validate the simulator gap before live use is confirmed in full.")]));
bodyChildren.push(body([t("Three constructive recommendations follow. First, if the system is still wanted, the only corner worth further research is the one this study isolates: wide-stop, low-spread instruments (gold micro-crosses, JPY crosses) at 4H, entered only when the round-trip spread is below a third of the stop distance - the cost gate lifted pooled in-sample PF by 0.14 and is the only refinement with a measurable effect. Second, keep the validation infrastructure running: the engine, sweep, selection and battery scripts ship with this report and re-execute in minutes, so the claim can be re-tested monthly as bars accumulate; if a future re-run shows an out-of-sample profit factor above 1.2 on at least 100 untouched trades, revisit the verdict. Third, treat the existing champion registry and its Monte Carlo audit as retired: their numbers cannot be restored by re-optimization, only by changing the cost reality (a raw-spread account with commission, or higher timeframes) or the entry logic itself.")]));

bodyChildren.push(h1("10. Reproduction"));
bodyChildren.push(body([t("Everything in this report regenerates from the repository. The corrected engine and all stage scripts are in the validation/ folder added next to this report: dfib_engine.py (faithful port with real spread costs, cost gate and stop-floor parameters), dfib_sweep.py (Stage 1), dfib_stage3.py and dfib_stage3b.py (pooled portfolios and refinements), dfib_verify_champions.py (the 362-setup verification), dfib_stage4a.py and dfib_stage4b.py (full-universe sweep and plateau portfolio), dfib_robustness.py (Monte Carlo, block bootstrap, Deflated Sharpe, thirds), dfib_make_charts.py and dfib_make_summary_xlsx.py (figures, workbook, summary). Run them in that order against historical_parquet/; total runtime is under fifteen minutes on four cores. The workbook Delta_Fibonacci_Validation.xlsx contains the verification table, family aggregates, portfolio picks, the full 1,399-trade ledger and the battery; dfib_summary.json holds every number quoted here.")]));

// ---------------- document ----------------
const doc = new Document({
  creator: "Z.ai", title: "Delta-Fibonacci Independent Validation Report",
  styles: { default: { document: { run: { font: FONT, size: 21, color: BODY } } } },
  features: { updateFields: true },
  sections: [
    {
      properties: { page: { margin: { top: 1440, bottom: 1260, left: 1350, right: 1350 } } },
      children: [
        new Paragraph({ spacing: { before: 2600 }, children: [new TextRun({ text: "INDEPENDENT VALIDATION REPORT", font: HFONT, size: 22, color: P.accent, bold: true })] }),
        new Paragraph({ spacing: { before: 200, after: 120 }, children: [new TextRun({ text: "Delta-Fibonacci Institutional Business", font: HFONT, size: 52, bold: true, color: P.cover.titleColor })] }),
        new Paragraph({ spacing: { after: 320 }, children: [new TextRun({ text: "Multi-Dimensional VWAP / MTF-EMA Rejection - Backtest, Optimization and Robustness Validation", font: FONT, size: 26, color: P.cover.subtitleColor })] }),
        ...["Data: 151-symbol historical_parquet store, 1M-4H, real broker spreads on every trade",
           "Verified: all 362 registry champion setups re-simulated with costs",
           "Optimized: 27,090 configurations, pooled portfolios, plateau selection, full quant battery",
           `Verdict: ${S.verdict}`].map((m, i) => new Paragraph({
          spacing: { before: i === 0 ? 900 : 140 },
          border: { left: { style: "single", size: 12, color: P.accent, space: 12 } },
          children: [new TextRun({ text: m, font: FONT, size: 21, color: i === 3 ? "D8B25A" : P.cover.metaColor, bold: i === 3 })] })),
        new Paragraph({ spacing: { before: 2400 }, children: [new TextRun({ text: "Generated 2026-11-23 - companion to Delta-Fibonacci-Institutional-Business", font: FONT, size: 18, color: P.cover.footerColor })] }),
      ],
    },
    { properties: { type: SectionType.CONTINUOUS }, children: [new Paragraph({ children: [new TableOfContents("Table of Contents", { hyperlink: true, headingStyleRange: "1-1" })] })] },
    { properties: { page: { pageNumbers: { start: 1, formatType: NumberFormat.DECIMAL } }, type: SectionType.NEXT_PAGE }, children: bodyChildren },
  ],
});

Packer.toBuffer(doc).then((buf) => { fs.writeFileSync(OUT, buf); console.log("saved", OUT, buf.length, "bytes"); });
