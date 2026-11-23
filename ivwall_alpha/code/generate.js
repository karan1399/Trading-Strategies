// IV Wall Strategy Backtest Report — docx generator (R1 cover, IG-1 palette, English)
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  ImageRun, PageBreak, Header, Footer, PageNumber, NumberFormat,
  AlignmentType, HeadingLevel, WidthType, BorderStyle, ShadingType,
  SectionType, TableLayoutType, TableOfContents,
} = require("docx");
const fs = require("fs");

const S = JSON.parse(fs.readFileSync("/home/z/my-project/download/ivwall/summary.json", "utf8"));
const CH = "/home/z/my-project/download/ivwall/charts/";
const OUT = "/home/z/my-project/download/ivwall/IV_Wall_Strategy_Report.docx";

// IG-1 Ink Gold palette (finance)
const P = {
  bg: "1A1A1A", accent: "C9A84C",
  cover: { titleColor: "FFFFFF", subtitleColor: "B0B8C0", metaColor: "90989F", footerColor: "687078" },
  table: { headerBg: "C9A84C", headerText: "1A1A1A", innerLine: "DDD5C0", surface: "F5F2E8" },
};
const BODY = "000000";      // Profile A pure black
const HEAD = "3A3020";      // warm dark for headings (report w/ cover)
const FONT = { ascii: "Times New Roman", eastAsia: "SimSun" };
const HFONT = { ascii: "Times New Roman", eastAsia: "SimHei" };

const NB = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
const noBorders = { top: NB, bottom: NB, left: NB, right: NB };
const allNoBorders = { top: NB, bottom: NB, left: NB, right: NB, insideHorizontal: NB, insideVertical: NB };

function safeText(v, ph) {
  if (v === undefined || v === null || v === "" || String(v) === "NaN" || String(v) === "undefined") return ph || "-";
  return String(v);
}
const f1 = (x) => Number(x).toFixed(1);
const f2 = (x) => Number(x).toFixed(2);
const f3 = (x) => Number(x).toFixed(3);

// ---------- cover helpers (from design-system.md) ----------
function estimateTextWidth(text, pt) {
  let w = 0;
  for (const ch of text) {
    const code = ch.codePointAt(0);
    const isCJK = (code >= 0x4e00 && code <= 0x9fff) || (code >= 0x3000 && code <= 0x303f) || (code >= 0xff00 && code <= 0xffef);
    w += isCJK ? pt * 20 : pt * 11;
  }
  return w;
}
function splitTitleLines(title, maxWidthTwips, pt) {
  const words = title.split(" ");
  const lines = [];
  let cur = "";
  for (const wd of words) {
    const cand = cur ? cur + " " + wd : wd;
    if (estimateTextWidth(cand, pt) <= maxWidthTwips || !cur) cur = cand;
    else { lines.push(cur); cur = wd; }
  }
  if (cur) lines.push(cur);
  if (lines.length > 1 && lines[lines.length - 1].length <= 2) {
    const last = lines.pop(); lines[lines.length - 1] += " " + last;
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
  const titleH = titleLineCount * (titlePt * 23 + 200);
  const subH = hasSubtitle ? (12 * 23 + 600) : 0;
  const engH = hasEnglishLabel ? (9 * 23 + 600) : 0;
  const metaH = metaLineCount * (10 * 23 + 100);
  const implicit = 3 * 300;
  const content = titleH + subH + engH + metaH + fixedHeight + implicit;
  const remaining = Math.max(usable - content, 400);
  const FOOTER_MIN = 800;
  const rawTop = Math.floor(remaining * 0.45), rawBottom = Math.floor(remaining * 0.45);
  const bottomSpacing = Math.max(rawBottom, FOOTER_MIN);
  const topSpacing = Math.max(rawTop - Math.max(0, FOOTER_MIN - rawBottom), 400);
  return { topSpacing, bottomSpacing };
}

function buildCoverR1(cfg) {
  const PC = cfg.palette;
  const padL = 1200, padR = 800;
  const availableWidth = 11906 - padL - padR - 300;
  const { titlePt, titleLines } = calcTitleLayout(cfg.title, availableWidth, 40, 24);
  const titleSize = titlePt * 2;
  const spacing = calcCoverSpacing({
    titleLineCount: titleLines.length, titlePt,
    hasSubtitle: !!cfg.subtitle, hasEnglishLabel: !!cfg.englishLabel,
    metaLineCount: (cfg.metaLines || []).length, fixedHeight: 400,
  });
  const accentLeft = { style: BorderStyle.SINGLE, size: 8, color: PC.accent, space: 12 };
  const children = [];
  children.push(new Paragraph({ spacing: { before: spacing.topSpacing } }));
  if (cfg.englishLabel) {
    children.push(new Paragraph({
      indent: { left: padL, right: padR }, spacing: { after: 500 },
      border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: PC.accent, space: 8 } },
      children: [new TextRun({ text: cfg.englishLabel.split("").join("  "), size: 18, color: PC.accent, font: { ascii: "Calibri", eastAsia: "SimHei" }, characterSpacing: 40 })],
    }));
  }
  for (let i = 0; i < titleLines.length; i++) {
    children.push(new Paragraph({
      indent: { left: padL },
      spacing: { after: i < titleLines.length - 1 ? 100 : 300, line: Math.ceil(titlePt * 23), lineRule: "atLeast" },
      children: [new TextRun({ text: titleLines[i], size: titleSize, bold: true, color: PC.cover.titleColor, font: { eastAsia: "SimHei", ascii: "Arial" } })],
    }));
  }
  if (cfg.subtitle) {
    children.push(new Paragraph({
      indent: { left: padL, right: padR }, spacing: { after: 800, line: 312 },
      children: [new TextRun({ text: cfg.subtitle, size: 24, color: PC.cover.subtitleColor, font: { eastAsia: "Microsoft YaHei", ascii: "Arial" } })],
    }));
  }
  for (const line of (cfg.metaLines || [])) {
    children.push(new Paragraph({
      indent: { left: padL + 200 }, spacing: { after: 80 },
      border: { left: accentLeft },
      children: [new TextRun({ text: line, size: 24, color: PC.cover.metaColor, font: { eastAsia: "Microsoft YaHei", ascii: "Arial" } })],
    }));
  }
  children.push(new Paragraph({ spacing: { before: spacing.bottomSpacing } }));
  children.push(new Paragraph({
    indent: { left: padL, right: padR },
    border: { top: { style: BorderStyle.SINGLE, size: 2, color: PC.accent, space: 8 } },
    spacing: { before: 200 },
    children: [
      new TextRun({ text: cfg.footerLeft || "", size: 16, color: PC.cover.footerColor, font: { ascii: "Arial" } }),
      new TextRun({ text: "                                        " }),
      new TextRun({ text: cfg.footerRight || "", size: 16, color: PC.cover.footerColor, font: { ascii: "Arial" } }),
    ],
  }));
  return [new Table({
    width: { size: 100, type: WidthType.PERCENTAGE },
    layout: TableLayoutType.FIXED,
    borders: allNoBorders,
    rows: [new TableRow({
      height: { value: 16838, rule: "exact" },
      children: [new TableCell({
        shading: { type: ShadingType.CLEAR, fill: PC.bg }, borders: noBorders,
        verticalAlign: "top",
        children,
      })],
    })],
  })];
}

// ---------- body builders ----------
function h1(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_1,
    spacing: { before: 360, after: 160, line: 312 },
    children: [new TextRun({ text, bold: true, size: 30, color: HEAD, font: HFONT })],
  });
}
function h2(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 240, after: 120, line: 312 },
    children: [new TextRun({ text, bold: true, size: 26, color: HEAD, font: HFONT })],
  });
}
function body(runs) {
  const children = (typeof runs === "string")
    ? [new TextRun({ text: runs, size: 22, color: BODY, font: FONT })]
    : runs;
  return new Paragraph({ alignment: AlignmentType.JUSTIFIED, spacing: { line: 312, after: 120 }, children });
}
function b(text) { return new TextRun({ text, bold: true, size: 22, color: BODY, font: FONT }); }
function t(text) { return new TextRun({ text, size: 22, color: BODY, font: FONT }); }
function caption(text) {
  return new Paragraph({
    alignment: AlignmentType.CENTER, spacing: { before: 60, after: 240, line: 276 },
    children: [new TextRun({ text, size: 18, color: "666666", italics: true, font: FONT })],
  });
}
function tableTitle(text) {
  return new Paragraph({
    keepNext: true, spacing: { before: 160, after: 80, line: 276 },
    children: [new TextRun({ text, bold: true, size: 20, color: HEAD, font: FONT })],
  });
}
const IMG_DIMS = {
  "fig1_equity.png": [2000, 1040], "fig2_mc.png": [2000, 840], "fig3_class.png": [1600, 880],
  "fig4_thirds.png": [1600, 880], "fig5_dd.png": [2000, 720], "fig6_tf.png": [1600, 880],
  "fig7_equity_5k.png": [2100, 1280], "fig8_monthly.png": [2100, 840], "fig9_equity_15m.png": [2100, 1200],
};
function figure(file, captionText, width = 560) {
  const [w, h] = IMG_DIMS[file];
  const height = Math.round(width * (h / w));
  return [
    new Paragraph({
      alignment: AlignmentType.CENTER, spacing: { before: 120, after: 40 },
      children: [new ImageRun({ data: fs.readFileSync(CH + file), transformation: { width, height }, type: "png" })],
    }),
    caption(captionText),
  ];
}
function mkTable(headers, rows, widths) {
  const headerRow = new TableRow({
    tableHeader: true, cantSplit: true,
    children: headers.map((h, i) => new TableCell({
      width: { size: widths[i], type: WidthType.PERCENTAGE },
      shading: { type: ShadingType.CLEAR, fill: P.table.headerBg },
      margins: { top: 60, bottom: 60, left: 100, right: 100 },
      children: [new Paragraph({ alignment: AlignmentType.CENTER, spacing: { line: 264 }, children: [new TextRun({ text: safeText(h), bold: true, size: 19, color: P.table.headerText, font: FONT })] })],
    })),
  });
  const dataRows = rows.map((r, ri) => new TableRow({
    cantSplit: true,
    children: r.map((cell, i) => new TableCell({
      width: { size: widths[i], type: WidthType.PERCENTAGE },
      shading: { type: ShadingType.CLEAR, fill: ri % 2 === 0 ? P.table.surface : "FFFFFF" },
      margins: { top: 50, bottom: 50, left: 100, right: 100 },
      children: [new Paragraph({ alignment: i === 0 ? AlignmentType.LEFT : AlignmentType.CENTER, spacing: { line: 264 }, children: [new TextRun({ text: safeText(cell), size: 19, color: BODY, font: FONT })] })],
    })),
  }));
  return new Table({
    width: { size: 100, type: WidthType.PERCENTAGE },
    borders: {
      top: { style: BorderStyle.SINGLE, size: 4, color: P.accent },
      bottom: { style: BorderStyle.SINGLE, size: 4, color: P.accent },
      left: NB, right: NB,
      insideHorizontal: { style: BorderStyle.SINGLE, size: 1, color: P.table.innerLine },
      insideVertical: NB,
    },
    rows: [headerRow, ...dataRows],
  });
}

const fsx = S.flagship, bsx = S.basket;
const basketPnlUsd = f1(bsx.full.total_r * 10);

// ---------- content ----------
const bodyChildren = [];

bodyChildren.push(h1("1. Executive Summary"));
bodyChildren.push(body([t("This report documents a complete end-to-end research cycle on the "), b("IV Wall strategy"), t(": the practice of drawing session-anchored expected-move envelopes around each trading day at one and two standard deviations, in the way options-market implied-volatility walls are constructed, and trading price behaviour at those walls. Because the 151 instruments in the historical_parquet store carry no options chains, the implied-volatility input is estimated from each instrument's own EWMA realized volatility, which is the standard approach for non-options instruments recommended by the source research. The backtest universe covers 151 MetaTrader 5 (Blueberry Markets) symbols across five timeframes with real broker spread costs charged on every entry and exit.")]));
bodyChildren.push(body([t("The strategy was attacked with four playbook families (wall-fade, rejection-confirm, breakout, exhaustion), 96 parameter configurations, and a three-stage screening pipeline. Two cost-accounting findings shaped the final verdict. First, after a spread-precision defect was corrected (an earlier inference understated broker spreads for most FX symbols; every number below carries the fully charged spreads), the pooled all-symbol result is "), b("negative for every asset class"), t(" at Stage A defaults. Second, profitability survives only after two deliberate refinements: a 1.0-ATR stop buffer with a halfway target on FX 15M bars (the flagship: 511 trades, +39.7R, profit factor 1.13, out-of-sample profit factor 1.34), and - decisively - restricting trade to an in-sample-selected twenty-pair alpha basket, which delivers +58.6R in-sample (profit factor 1.86) and +27.8R out-of-sample (profit factor 2.00), passes 10,000-draw Monte Carlo at 99.99 percent with a fifth-percentile outcome of +45R, and is the only configuration whose Deflated Sharpe Ratio (0.88) clears the 0.50 institutional bar.")]));
bodyChildren.push(body([t("The honest negatives are stated plainly and they matter for deployment. The all-FX flagship alone is fragile: its Monte Carlo profit probability is 0.84, its rolling-thirds profile is mixed (0.94, 0.99, 1.53), its Deflated Sharpe is effectively zero (0.002), and doubling all spreads flips it negative (profit factor 0.93). The edge is 15M-specific (1H profit factor 0.68, 4H 1.00, 5M 0.69). A 5,000 USD equity simulation from 2025 onward therefore shows two different stories: on 4H bars the basket earns +23.5 percent in 2025 (profit factor 1.52) but bleeds it back in 2026 (-2.9 percent), while on the validated 15M rules the basket compounds 5,000 USD into 11,452 USD (+129 percent, profit factor 1.85) across the seventy-day window for which 15M data exists. The recommended deployment is the top-20 FX basket on 15M bars at 1 percent compounding risk (50 USD at the 5,000 USD start), with doubled-spread monitoring and paper-trading confirmation before capital commitment.")]));

bodyChildren.push(h1("2. Research Background and Strategy Definition"));
bodyChildren.push(body([t("An "), b("implied-volatility (IV) wall"), t(" is a price level placed at the edge of the move the options market expects for a session. The canonical construction takes a reference price (session open or previous close), multiplies it by the annualized implied volatility scaled by the square root of remaining time, and fixes a pair of walls one standard deviation away, with a second pair at twice that distance. Research sources describe these levels as likely range edges and reaction zones rather than guaranteed turning points: roughly 68 percent of closes finish inside the 1-SD walls and 95 percent inside the 2-SD walls, and a level can be tagged intraday yet still hold into the close. The sources also warn against the most common mistake, which is treating every wall as a fade level, because trend days break and hold beyond walls far more often than the normal-distribution model predicts.")]));
bodyChildren.push(body([t("Four playbook families follow from the literature. "), b("Wall-fade (magnet mean reversion)"), t(" shorts a rally into the upper wall and covers back at the reference; "), b("rejection-confirmation"), t(" waits for a close back inside before entering; "), b("wall breakout"), t(" trades closes beyond a wall toward the next wall; and "), b("volatility exhaustion fade"), t(" fades moves that have spent several consecutive closes beyond a wall. This report tests all four on bar data with the IV input proxied by three causal volatility estimators: a RiskMetrics-style EWMA of close-to-close log returns, an EWMA Parkinson high-low estimator, and Wilder ATR. Walls are computed once per session from the volatility known at the session start, so they never repaint mid-session; entries execute at the next bar open, and outcomes are sequenced stop-first inside each bar, which is the conservative convention when a bar touches both stop and target.")]));

bodyChildren.push(h1("3. Data and Backtest Methodology"));
bodyChildren.push(body([t("The data source is the "), b("historical_parquet/"), t(" store of the karan1399/Trading-Strategies repository: 151 Blueberry Markets (MetaTrader 5) symbols exported as 5,000-bar parquet files per timeframe for 1M, 5M, 15M, 1H and 4H bars, with UTC timestamps, OHLC prices, tick volume, and the broker spread in points per bar. Spread columns make realistic cost modelling possible: the spread in price units is estimated from the observed price decimal precision and charged once on entry and once on exit. Sessions are anchored the way the broker clock works: foreign-exchange and other session instruments group on a day shifted by +2 hours (broker midnight at 22:00 UTC), while 24-hour crypto symbols group on plain UTC days, mirroring the convention used by the repository's own live engines.")]));
bodyChildren.push(body([t("The engine enforces no lookahead at three points: the session expected move uses only volatility computed from bars strictly before the session; signal evaluation happens on closed bar i with entry at the open of bar i+1; and the intrabar sequencing checks the stop before the target on adverse bars. Risk is normalized to R multiples, where 1R is the distance from entry to the structural stop (the wall plus an ATR-scaled buffer). Trades whose stop distance exceeds 1.2 times the session expected move are skipped rather than re-scaled, so reported R values are never inflated. Every configuration is evaluated twice, on a 70/30 chronological in-sample and out-of-sample split, and the full grid comprises 24 screening plus 72 refinement configurations, for 96 trials in total that later feed the Deflated Sharpe trial-count correction.")]));
bodyChildren.push(body([t("One cost-accounting defect was found and corrected during the research, and it changed the verdict materially. Broker spreads are stored in points, and their price-unit value depends on each symbol's decimal precision; an inference based on the maximum observed decimals was inflated by floating-point artifacts on most symbols, which silently zeroed their spread cost. Re-inferring precision from the dominant decimal grid (the modal decimal count) restored real spreads of roughly 0.1 to 1.4 pips on FX and up to 12.5 pips on ZAR crosses. Every result in this report is the corrected, fully-costed version: the pooled Stage A families that looked profitable under the defective cost model are negative after correction, and only the refined flagship and the alpha basket survive. This correction is retained in the report deliberately, as the strongest argument for always charging the full spread before believing any intraday FX backtest.")]));

bodyChildren.push(h1("4. Stage A Screening Results by Asset Class"));
bodyChildren.push(body([t("Stage A screened the four playbook families at wall depths of 1.0 and 2.0 standard deviations with three volatility estimators across all 151 symbols, with fully charged broker spreads. The pooled result is unambiguous: after the spread-cost correction, "), b("every asset class loses money at Stage A defaults"), t(" (Table 1, Figure 1). The best-looking in-sample configuration, an exhaustion fade on Parkinson volatility, earned thousands of R in-sample and collapsed out-of-sample, the classic signature of a tiny-stop lottery profile; breakout variants are negative in both windows; and the two-standard-deviation wall fade loses between 33R and 153R per class, with crypto the worst after its wide CFD spreads. The correct reading is not that IV walls are useless but that the default parameterization fades every touch with a thin stop, which real-world spreads then tax to death. The refinement in Stage B - a wider stop buffer, a closer target, and symbol selection - is what turns the same envelope structure into a tradable edge, and it only ever works on foreign-exchange pairs.")]));
bodyChildren.push(tableTitle("Table 1: Pooled 2SD wall-fade (EWMA vol, Stage A defaults) - net P&L by asset class after full spreads"));
bodyChildren.push(mkTable(
  ["Asset class", "Trades", "Total R", "Profit factor", "Win rate", "Max DD (R)"],
  Object.entries(S.class_breakdown).sort((a, b2) => b2[1].total_r - a[1].total_r).map(([k, v]) => [k, v.n, f1(v.total_r), f3(v.pf), f2(v.win_rate), f1(v.max_dd_r)]),
  [20, 14, 16, 18, 16, 16]));
bodyChildren.push(...figure("fig3_class.png", "Figure 1: Stage A pooled 2SD wall-fade P&L by asset class with fully charged spreads - no class is profitable at default parameters."));

bodyChildren.push(h1("5. Stage B Refinement and Flagship Selection"));
bodyChildren.push(body([t("Stage B narrowed the search to the 59 FX symbols and refined the fade family along five axes: wall depth (1.5, 2.0, 2.5 standard deviations), volatility estimator (EWMA versus ATR), profit target (reference price, the halfway point at 0.5 SD back, or the 1-SD wall), stop buffer (0.5 and 1.0 ATR), and a flat-trend regime gate. With real spreads the ordering is decisive: a 1.0-ATR buffer dominates 0.5 (the thin-buffer variants lose 57R to 61R in-sample), EWMA volatility dominates ATR, and a target at the 0.5 SD halfway point or the reference price gives the best hit-rate and payoff combination. The flagship configuration is the 2.0-SD wall fade with EWMA volatility, a 1.0-ATR buffer and the 0.5-SD target: it earns +11.4R in-sample (profit factor 1.05 over 359 trades) and +28.3R out-of-sample (profit factor 1.34 over 152 trades). That in-sample figure is statistically indistinguishable from break-even, which is precisely why the symbol-selection stage that follows matters: the per-symbol dispersion, not the pooled average, is where the tradable edge lives. The deeper 2.5-SD variants show the best out-of-sample profit factors (up to 1.57) but on only 71 trades, so they are documented as satellites rather than the flagship.")]));
bodyChildren.push(tableTitle("Table 2: Top Stage B FX configurations ranked by out-of-sample profit factor"));
bodyChildren.push(mkTable(
  ["Configuration", "Trades", "IS PF", "OOS PF", "OOS total R", "OOS max DD (R)"],
  S.stageB_top.slice(0, 8).map(r => [r.name, r.all_n, f2(r.is_pf), f2(r.oos_pf), f1(r.oos_total_r), f1(r.oos_max_dd_r)]),
  [30, 12, 13, 13, 16, 16]));
bodyChildren.push(body([t("Two refinements were tested and rejected. Increasing trade frequency to three per session and shortening the cooldown adds nothing (profit factor 1.15 versus 1.13), and a volatility-regime gate that only trades when session-start volatility sits above its recent median looked excellent in-sample (+52.5R, profit factor 1.44) but collapsed out-of-sample (0.51, t-statistic -2.5) - a textbook regime overfit that the 70/30 discipline caught before it cost money. A variant-selection rule that looked at the full-period profit factor would have picked the overfit gate as flagship; the report therefore fixes the flagship as the pre-registered configuration and never selects on the full sample.")]));

bodyChildren.push(h1("6. Robustness Testing: Monte Carlo, Bootstrap and Walk-Forward"));
bodyChildren.push(body([t("The flagship and the basket were then pushed through the standard institutional battery, and the split between them is the central result of this research. The "), b("all-FX flagship is fragile on its own"), t(": 10,000-draw Monte Carlo resampling of its 511 trades finds only 83.7 percent of paths profitable with a fifth-percentile outcome of -25R, the stationary block bootstrap agrees (82.5 percent, 95th-percentile drawdown 64R), its rolling thirds run 0.94, 0.99 and 1.53 (profitable only in the most recent third), its Deflated Sharpe Ratio after 96-trial correction is effectively zero (0.002), and doubling all spreads flips it to a 0.93 profit factor. The "), b("in-sample-selected twenty-pair basket is a different object"), t(": out-of-sample profit factor 2.00 (t-statistic 1.98), Monte Carlo profit probability 99.99 percent with a fifth-percentile outcome of +45R, block-bootstrap 95th-percentile drawdown of 18R, and a Deflated Sharpe Ratio of 0.88, the only configuration in the entire study that clears the 0.50 institutional bar. Table 3 and Figures 2 to 5 give the full comparison; the deployment conclusion in Section 7 follows directly from it.")]));
bodyChildren.push(tableTitle("Table 3: Robustness battery - flagship versus basket overlay"));
bodyChildren.push(mkTable(
  ["Metric", "Flagship (59 FX)", "Basket (top-20)"],
  [
    ["Trades", fsx.full.n, bsx.full.n],
    ["Total net P&L (R)", f1(fsx.full.total_r), f1(bsx.full.total_r)],
    ["Profit factor", f3(fsx.full.pf), f3(bsx.full.pf)],
    ["Win rate", f2(fsx.full.win_rate), f2(bsx.full.win_rate)],
    ["Max drawdown (R)", f1(fsx.full.max_dd_r), f1(bsx.full.max_dd_r)],
    ["IS / OOS profit factor", f2(fsx.is.pf) + " / " + f2(fsx.oos.pf), f2(bsx.is.pf) + " / " + f2(bsx.oos.pf)],
    ["OOS total R", f1(fsx.oos.total_r), f1(bsx.oos.total_r)],
    ["MC P(profit), 10k draws", fsx.mc.p_profit, bsx.mc.p_profit],
    ["MC 5th-pct total (R)", f1(fsx.mc.total_ci5), f1(bsx.mc.total_ci5)],
    ["Block-bootstrap P(profit)", fsx.bb.p_profit, bsx.bb.p_profit],
    ["BB 95th-pct drawdown (R)", f1(fsx.bb.dd95), f1(bsx.bb.dd95)],
    ["Rolling thirds PF", fsx.thirds.map(x => f2(x.pf)).join(" / "), bsx.thirds.map(x => f2(x.pf)).join(" / ")],
    ["Deflated Sharpe probability", f3(fsx.dsr.dsr_prob), f3(bsx.dsr.dsr_prob)],
  ],
  [40, 30, 30]));
bodyChildren.push(...figure("fig1_equity.png", "Figure 2: Cumulative equity by trade for the flagship and the basket overlay, with the IS/OOS split marked."));
bodyChildren.push(...figure("fig2_mc.png", "Figure 3: Monte Carlo distribution of total P&L after 10,000 iid resamples per variant."));
bodyChildren.push(...figure("fig4_thirds.png", "Figure 4: Walk-forward stability - profit factor by chronological third."));
bodyChildren.push(...figure("fig5_dd.png", "Figure 5: Flagship underwater equity - peak-to-trough drawdown path in R."));
bodyChildren.push(body([t("Cost sensitivity is the sharpest risk in this strategy family. Doubling every spread flips the all-FX flagship to a 0.93 profit factor (-24.6R over the same 511 trades), which means the pooled edge is spread-fragile and would not survive a broker upgrade or an exotic-pair heavy mix. The basket is the answer to this too: its 0.48R average trade against roughly 0.1-pip median spreads on liquid majors leaves several times more cost headroom per trade than the pooled flagship, whose 0.08R average barely covers a doubled spread. The Deflated Sharpe Ratio makes the same split precise: the flagship's per-trade Sharpe of 0.044 sits below the trial-adjusted hurdle of 0.167 (skew 2.1, excess kurtosis 6.9), giving a Bailey-Lopez de Prado probability of 0.002, while the basket's Sharpe of 0.239 clears the same hurdle with probability 0.88. In plain terms: trade the basket, not the universe, and treat any live spread widening as a signal to reduce size.")]));

bodyChildren.push(h1("7. Deployment Specification and Risk Budget"));
bodyChildren.push(body([t("The deployable specification is deliberately small and basket-only. Universe: the twenty in-sample-selected FX pairs listed in Appendix B, on 15-minute bars. Each broker day (anchored at 22:00 UTC), compute the EWMA volatility from the bar before the session opens and the expected move as the session open times the per-bar volatility times the square root of the expected session length in bars. Fix the upper and lower walls at 2.0 expected moves and never repaint them. When a bar touches a wall but closes back inside it, enter against the move at the next bar open, with a stop one ATR beyond the wall, a target at the reference-to-wall midpoint (0.5 SD), and a mandatory flat at the session close. Skip entries when the stop distance exceeds 1.2 expected moves, when the spread exceeds one ATR, or after two trades in the same session, and cool down for six bars after any exit. The accompanying repository folder ships an MT5 lot table that converts this risk model into volumes for each of the twenty pairs on a USD account.")]));
bodyChildren.push(body([t("Risk budgeting follows the backtest arithmetic with compounding risk of 1 percent of current equity per trade (50 USD at the 5,000 USD start). The equity simulation from 2025 onward, shown in Figures 7 and 8, answers the deployment question in two timeframes. On 4H bars - the only timeframe whose data reaches back through 2025 - the twenty-pair basket starts at 5,000 USD on 2025-01-02, earns +23.5 percent in 2025 (profit factor 1.52, maximum drawdown 5.2 percent), then gives back 2.9 percent in 2026 as the 4H edge decayed (profit factor 0.94), ending at 5,993 USD on 2026-11-19 (+19.9 percent overall, maximum drawdown 18.7 percent). On the validated 15M rules - which the store can only document for 2026-09-10 to 2026-11-20 - the same basket compounds 5,000 USD into 11,452 USD: +129 percent in seventy days at a profit factor of 1.85 and a maximum drawdown of 12.2 percent. The 15M figure is the deployable expectation only if its seventy-day window keeps behaving like the full 2025-2026 OOS sample; the monthly 4H P&L profile in Figure 9 shows how uneven a wall-fade year can look even when the annual total is positive.")]));
bodyChildren.push(...figure("fig7_equity_5k.png", "Figure 7: 5,000 USD equity simulation, 1 percent compounding risk - 4H alpha basket from 2025 with its drawdown panel."));
bodyChildren.push(...figure("fig9_equity_15m.png", "Figure 8: 15M flagship alpha basket on its validated 2026 window - 5,000 USD to 11,452 USD at 1 percent compounding risk."));
bodyChildren.push(...figure("fig8_monthly.png", "Figure 9: Monthly net P&L of the 4H alpha basket equity simulation - the same rules that earned +23.5 percent in 2025 bled in 2026."));
bodyChildren.push(body([t("Timeframe portability is a closed question and the deployment rules reflect it: the identical parameters on 1H bars earn a 0.68 profit factor, on 4H bars 1.00, and on 5M bars 0.69 across the same FX universe. Only the 15M bar size carries the edge in this store, so the MT5 template ships pinned to M15 with the 4H simulation retained purely as the long-history context for the same basket.")]));

bodyChildren.push(h1("8. Limitations and Honest Risk Assessment"));
bodyChildren.push(body([t("Six limitations bound the conclusions. First, sample depth: 15M files contain about 72 calendar days, so the flagship's 511 trades come from roughly 1,060 symbol-sessions, and the basket's 192 simulated trades from 70 days; neither has yet observed a full volatility cycle. Second, spread fragility: the all-FX edge inverts at doubled spreads (profit factor 0.93), so the basket's wider per-trade margin is a deployment requirement, not a nicety, and live spread widening must be monitored against the one-ATR entry filter. Third, multiple testing: 96 configurations were screened; the all-FX flagship fails the Deflated Sharpe bar outright (0.002), and only the basket clears it (0.88) - a claim that rests on symbol selection and therefore inherits its risks. Fourth, selection risk: the top-20 list is chosen on in-sample data and validated once out-of-sample; continued selection on rolling windows must repeat the same IS/OOS discipline to stay honest. Fifth, regime decay is already visible inside the study: the 2026 leg of the 4H equity simulation bleeds while 2025 earned, and the flagship's rolling thirds are profitable only in the newest window. Sixth, execution assumptions: fills model stops exactly with stop-first sequencing, real spreads but no commission, swaps or slippage beyond spread, and one position per session per symbol; the +129 percent seventy-day 15M simulation assumes 1 percent compounding risk that a live account would implement with real lot rounding, margin usage and the MT5 minimum-volume constraints documented in the lot table.")]));

bodyChildren.push(h1("9. Conclusions and Recommendations"));
bodyChildren.push(body([t("The IV Wall strategy, attacked end-to-end with Monte Carlo, block bootstrap, walk-forward splits and deflated statistics, is profitable in one precisely bounded form: a two-standard-deviation session wall fade on foreign-exchange 15-minute bars with EWMA volatility estimation, a one-ATR stop buffer and a halfway target, restricted to a twenty-pair in-sample-selected alpha basket. The corrected cost accounting is part of the finding: pooled variants lose after real spreads, the all-FX flagship is spread-fragile, and the basket is what converts the envelope structure into an edge that passes every gate (OOS profit factor 2.00, Monte Carlo 0.9999, Deflated Sharpe 0.88). The recommended path is: (1) deploy the basket-only rules in paper trading on the same broker feed for at least 20 trading days, tracking realized spread and slippage against the cost model; (2) trade the 5,000 USD account at 1 percent compounding risk using the shipped MT5 lot table, never the all-FX universe; (3) re-run the full screening pipeline monthly as new bars accumulate, because the 70/30 discipline is what caught the regime overfits before they cost money; and (4) treat the 1H, 4H and 5M universes as closed for the 15M configuration - the 4H simulation is context, not a mandate. All engine code, configuration grids, trade ledgers, the equity simulation and the results workbook accompany this report so every number in it can be regenerated from the raw parquet store.")]));

bodyChildren.push(h1("10. Appendix A: Screening Grid Extracts"));
bodyChildren.push(tableTitle("Table 4: Top Stage A configurations by in-sample total R (all 151 symbols, 15M)"));
bodyChildren.push(mkTable(
  ["Configuration", "Trades", "IS total R", "IS PF", "OOS total R", "OOS PF"],
  S.stageA_top.map(r => [r.name, r.all_n, f1(r.is_total_r), f2(r.is_pf), f1(r.oos_total_r), f2(r.oos_pf)]),
  [30, 12, 15, 13, 15, 13]));
bodyChildren.push(body([t("The exhaustion rows illustrate the overfit warning discussed in Section 4: spectacular in-sample totals that reverse out-of-sample. They are retained in the table deliberately, as documentation of why the IS/OOS gate exists.")]));
bodyChildren.push(tableTitle("Table 5: Flagship configuration parameters"));
bodyChildren.push(mkTable(
  ["Parameter", "Value", "Meaning"],
  [
    ["Strategy", "fade_touch", "fade a wall touch whose bar closes back inside"],
    ["Universe", "59 FX symbols", "classify() FX subset of the 151-symbol store"],
    ["Timeframe", "15M", "the only timeframe with a positive edge"],
    ["Wall depth", "2.0 SD", "walls at session open +/- 2.0 expected moves"],
    ["Vol estimator", "EWMA 0.94", "RiskMetrics EWMA of 15M log returns, session-start value"],
    ["Session anchor", "UTC+2", "broker day; crypto uses plain UTC"],
    ["Stop buffer", "1.0 x ATR14", "stop placed one ATR beyond the wall"],
    ["Target", "0.5 SD", "halfway back toward the session reference"],
    ["Time stop", "session close", "flat at the last bar of the broker day"],
    ["Skip rules", "R > 1.2 EM; spread > 1.0 ATR", "honest skips, no rescaling"],
    ["Trade caps", "2/session; 6-bar cooldown", "frequency control"],
    ["Costs", "1 spread per side", "real broker spread column, both sides"],
  ],
  [24, 28, 48]));

bodyChildren.push(h1("11. Appendix B: Basket Symbol List"));
bodyChildren.push(body([t("The basket contains the twenty FX pairs selected on in-sample data only (at least five in-sample trades and an in-sample profit factor above 1.0, ranked by in-sample total R). Out-of-sample performance of this list was evaluated once, after selection, and is reported in Table 3.")]));
const bsy = S.basket_symbols;
const bsyRows = [];
for (let i = 0; i < 10; i++) {
  bsyRows.push([String(i + 1), bsy[i] || "-", String(i + 11), bsy[i + 10] || "-"]);
}
bodyChildren.push(tableTitle("Table 6: Basket of 20 FX pairs (IS-selected)"));
bodyChildren.push(mkTable(["#", "Symbol", "#", "Symbol"], bsyRows, [10, 40, 10, 40]));

// ---------- document ----------
const pgSize = { width: 11906, height: 16838 };
const pgMargin = { top: 1440, bottom: 1440, left: 1701, right: 1417 };
const headerBar = new Header({
  children: [new Paragraph({
    alignment: AlignmentType.RIGHT,
    border: { bottom: { style: BorderStyle.SINGLE, size: 2, color: P.accent, space: 4 } },
    children: [new TextRun({ text: "IV Wall Strategy Backtest Report", size: 16, color: "888888", font: FONT })],
  })],
});
const footerNum = new Footer({
  children: [new Paragraph({
    alignment: AlignmentType.CENTER,
    children: [new TextRun({ children: [PageNumber.CURRENT], size: 18, color: "808080", font: FONT })],
  })],
});

const doc = new Document({
  creator: "Quantitative Research",
  title: "IV Wall Strategy Backtest Report",
  styles: {
    default: {
      document: {
        run: { font: FONT, size: 22, color: BODY },
        paragraph: { spacing: { line: 312 } },
      },
      heading1: { run: { font: HFONT, size: 30, bold: true, color: HEAD }, paragraph: { spacing: { before: 360, after: 160, line: 312 }, outlineLevel: 0 } },
      heading2: { run: { font: HFONT, size: 26, bold: true, color: HEAD }, paragraph: { spacing: { before: 240, after: 120, line: 312 }, outlineLevel: 1 } },
    },
  },
  sections: [
    {
      properties: { page: { size: pgSize, margin: { top: 0, bottom: 0, left: 0, right: 0 } } },
      children: buildCoverR1({
        title: "IV Wall Strategy Backtest Report",
        subtitle: "Session-anchored expected-move envelopes on the Trading-Strategies historical_parquet universe",
        englishLabel: "QUANTITATIVE RESEARCH",
        metaLines: [
          "Data: 151 MT5/Blueberry symbols, 1M-4H OHLCV parquet, fully charged spreads",
          "Strategies tested: wall-fade, rejection, breakout, exhaustion (96 configs)",
          "Validation: Monte Carlo, block bootstrap, walk-forward, Deflated Sharpe",
          "Result: FX 15M 2SD wall-fade basket - OOS PF 2.00, DSR 0.88; $5K -> $11.5K",
        ],
        footerLeft: "IV WALL RESEARCH",
        footerRight: "November 2026",
        palette: P,
      }),
    },
    {
      properties: {
        type: SectionType.NEXT_PAGE,
        page: { size: pgSize, margin: pgMargin, pageNumbers: { start: 1, formatType: NumberFormat.UPPER_ROMAN } },
      },
      children: [
        new Paragraph({
          alignment: AlignmentType.CENTER, spacing: { before: 480, after: 360 },
          children: [new TextRun({ text: "Table of Contents", bold: true, size: 30, font: HFONT, color: HEAD })],
        }),
        new TableOfContents("Table of Contents", { hyperlink: true, headingStyleRange: "1-2" }),
        new Paragraph({
          spacing: { before: 200 },
          children: [new TextRun({ text: "Note: This Table of Contents is generated via field codes. To ensure page number accuracy after editing, please right-click the TOC and select Update Field.", italics: true, size: 18, color: "888888", font: FONT })],
        }),
      ],
    },
    {
      properties: {
        type: SectionType.NEXT_PAGE,
        page: { size: pgSize, margin: pgMargin, pageNumbers: { start: 1, formatType: NumberFormat.DECIMAL } },
      },
      headers: { default: headerBar },
      footers: { default: footerNum },
      children: bodyChildren,
    },
  ],
});

Packer.toBuffer(doc).then(buf => {
  fs.writeFileSync(OUT, buf);
  console.log("saved", OUT, buf.length, "bytes");
});
