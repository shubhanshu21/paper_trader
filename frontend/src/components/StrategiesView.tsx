import { useState, useEffect, useRef, type ReactNode } from "react";
import {
  Plus, Play, Pause, RefreshCw, TrendingUp, AlertCircle, BarChart3, Trash2, Pencil,
  Layers, Clock, LogOut, Activity, FileText, Target, ArrowUpRight, ArrowDownRight, Calendar, IndianRupee, Info, X,
  Download, GitCompare, Eye, Wallet,
  type LucideIcon,
} from "lucide-react";
import { DatePicker } from "./Common";
import { C, FONT, fmtDate, fmtDateTime, formatTime12h, inr } from "../lib/format";
import { useToast } from "../hooks/useToast";
import { api, wsUrl } from "../api";
import type {
  BacktestCycle, BacktestResult, BacktestRunSummary, BacktestRunDetail,
  PayoffResponse, PositionLeg, MarginResponse, PortfolioMarginResponse,
} from "../api";
import type { CustomStrategy, CustomStrategyRules, EngineRules, PortfolioGreeksResponse } from "../types/customStrategy";
import { useCustomStrategyPositions } from "../hooks/useCustomStrategyPositions";
import StrategyBuilderModal from "./StrategyBuilderModal";
import AdjustmentPreviewCard from "./AdjustmentPreviewCard";
import BacktestEquityChart from "./BacktestEquityChart";
import PayoffDiagramChart from "./PayoffDiagramChart";

interface StrategiesViewProps {
  strategies: CustomStrategy[];
  loading: boolean;
  statusUpdatingId: number | null;
  deletingId: number | null;
  onRefresh: () => void;
  onCreate: (payload: { name: string; instrument_type: string; symbols: string[]; rules: CustomStrategyRules | EngineRules; strategy_type?: string }) => Promise<CustomStrategy>;
  onUpdate: (id: number, payload: { name: string; symbols: string[]; rules: CustomStrategyRules | EngineRules }) => Promise<CustomStrategy>;
  onStatusChange: (id: number, status: string) => Promise<CustomStrategy>;
  onDelete: (id: number) => Promise<number>;
}

export interface StrategyTemplate {
  type: string;
  description: string;
  risk_level: string;
}

export interface InstrumentTypeOption {
  type: string;
  description: string;
}

interface LegGreeks {
  iv: number;
  delta: number;
  gamma: number;
  theta: number;
  vega: number;
  rho: number;
}

interface LiveGreeksLeg {
  leg_index: number;
  symbol: string | null;
  option_type: string | null;
  strike: number | null;
  expiry: string | null;
  transaction_type: string;
  quantity: number;
  current_price: number | null;
  futures_price: number | null;
  greeks: LegGreeks | null;
}

interface LiveGreeksResponse {
  strategy_id: number;
  legs: LiveGreeksLeg[];
  net: { delta: number; gamma: number; theta: number; vega: number } | null;
  message?: string;
}

const strikeLabel = (sel: { mode: string; value: number | null; min?: number | null; max?: number | null }) => {
  if (sel.mode === "ATM") return "ATM";
  if (sel.mode === "OTM_PERCENT") return `${sel.value}% OTM`;
  if (sel.mode === "OTM_POINTS") return `${sel.value} pts OTM`;
  if (sel.mode === "FIXED") return `Strike ${sel.value}`;
  if (sel.mode === "PREMIUM_OFFSET") return `Straddle premium / ${sel.value} OTM`;
  if (sel.mode === "PREMIUM_BAND") return `₹${sel.min}-₹${sel.max} premium`;
  return `Strike ${sel.value}`;
};

const STATUS_META: Record<string, { bg: string; fg: string; dot: string; label: string }> = {
  DRAFT: { bg: "#f1f2f4", fg: "#5f6672", dot: "#9aa1ac", label: "Draft" },
  BACKTESTING: { bg: "#eaf1ff", fg: "#2f5fd6", dot: "#4184f3", label: "Backtesting" },
  PAPER_TRADING: { bg: "#e8f7ec", fg: "#1f8a3d", dot: "#3fb457", label: "Paper Trading" },
  LIVE: { bg: "#fff0e8", fg: "#c8460a", dot: "#ff5722", label: "Live" },
  PAUSED: { bg: "#fff6df", fg: "#a16a00", dot: "#e5a300", label: "Paused" },
  STOPPED: { bg: "#fdeceb", fg: "#c22b26", dot: "#df514c", label: "Stopped" },
};

function StatusPill({ status, big = false }: { status: string; big?: boolean }) {
  const meta = STATUS_META[status] || STATUS_META.DRAFT;
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full font-semibold ${big ? "px-3 py-1 text-xs" : "px-2 py-0.5 text-[11px]"}`}
      style={{ backgroundColor: meta.bg, color: meta.fg }}
    >
      <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: meta.dot }} />
      {meta.label}
    </span>
  );
}

// strategy_types with their OWN schema/executor/scheduler (see e.g.
// strategies/custom/intraday_schema.py / combo_schema.py's module
// docstrings for why) — no legs/entry/exit rules shape, no payoff/margin
// preview, no backtest support. Every "hide this for non-leg-based
// strategies" spot below checks membership here instead of repeating a
// strategy_type string each time, so a future new engine only needs
// adding to this one set.
const NON_LEG_STRATEGY_TYPES = new Set(["SUPERTREND_INTRADAY", "WEEKEND_GAP_COMBO", "OTM_PUT_ROLL", "SMART_CONDOR", "GRAVITY", "SESSION_SELLER", "MACD_CREDIT_SPREAD", "DELTA_NEUTRAL_STRANGLE", "WEEKLY_DIRECTIONAL", "MATRIX_CALENDAR"]);
const isLegBased = (strategy: Pick<CustomStrategy, "strategy_type">): boolean => !NON_LEG_STRATEGY_TYPES.has(strategy.strategy_type);

/** "Intraday"/"Weekend Combo"/"Roll"/"Condor"/"Signal"/"Sessions"/"Stop & Reverse"/"Delta-Neutral" for the non-leg-based engines (see NON_LEG_STRATEGY_TYPES); otherwise the leg-based builder's own expiry cadence (defaults to Weekly, same convention as rule_schema.py/describe_rules). */
function cadenceLabel(strategy: Pick<CustomStrategy, "strategy_type" | "rules">): string {
  if (strategy.strategy_type === "SUPERTREND_INTRADAY") return "Intraday";
  if (strategy.strategy_type === "WEEKEND_GAP_COMBO") return "Weekend Combo";
  if (strategy.strategy_type === "OTM_PUT_ROLL") return "Roll";
  if (strategy.strategy_type === "SMART_CONDOR") return "Condor";
  if (strategy.strategy_type === "GRAVITY") return "Signal";
  if (strategy.strategy_type === "SESSION_SELLER") return "Sessions";
  if (strategy.strategy_type === "MACD_CREDIT_SPREAD") return "Stop & Reverse";
  if (strategy.strategy_type === "DELTA_NEUTRAL_STRANGLE") return "Delta-Neutral";
  return (strategy.rules?.expiry?.mode || "WEEKLY") === "MONTHLY" ? "Monthly" : "Weekly";
}

function CadenceBadge({ strategy }: { strategy: Pick<CustomStrategy, "strategy_type" | "rules"> }) {
  const label = cadenceLabel(strategy);
  const isCustomEngine = !isLegBased(strategy);
  const color = isCustomEngine ? "#7c3aed" : C.muted;
  const bg = isCustomEngine ? "#f3ecfe" : "#f1f2f4";
  return (
    <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold" style={{ backgroundColor: bg, color }}>
      {label}
    </span>
  );
}

/**
 * "Buying" (net long/debit) vs "Selling" (net short/credit) — from total
 * lots on BUY legs vs SELL legs (not live premium, which isn't available
 * for a DRAFT strategy that's never been priced). PREMIUM_BAND legs are
 * excluded from the count entirely — rule_schema.py's own contract
 * defines that mode as "a deep-OTM insurance leg" (e.g. an over-hedge
 * bought in a ₹40-70 premium band), so counting its lots as part of the
 * core position would misclassify a lot-heavy-but-cheap hedge as the
 * strategy's real bias (this is what a plain lot count gets wrong for
 * the Over-Hedged Iron Fly template). A tie on the remaining legs (e.g.
 * Iron Condor, Butterfly, the 1:3:2 ratio spreads) defaults to "Selling"
 * since every tied template on this platform is actually a credit/
 * theta-decay structure. Returns null for non-leg-based engines (no
 * rules.legs shape to read at all — see NON_LEG_STRATEGY_TYPES).
 */
function strategyBias(strategy: Pick<CustomStrategy, "strategy_type" | "rules">): "BUYING" | "SELLING" | null {
  if (!isLegBased(strategy) || !strategy.rules?.legs?.length) return null;
  let buyLots = 0, sellLots = 0;
  for (const leg of strategy.rules.legs) {
    if (leg.strike_selection?.mode === "PREMIUM_BAND") continue;
    if (leg.action === "BUY") buyLots += leg.lots;
    else sellLots += leg.lots;
  }
  return buyLots > sellLots ? "BUYING" : "SELLING";
}

function BiasBadge({ strategy }: { strategy: Pick<CustomStrategy, "strategy_type" | "rules"> }) {
  const bias = strategyBias(strategy);
  if (!bias) return null;
  const isBuying = bias === "BUYING";
  return (
    <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold"
      style={{ backgroundColor: isBuying ? C.buyBg : C.sellBg, color: isBuying ? C.buyText : C.sellText }}>
      {isBuying ? "Buying" : "Selling"}
    </span>
  );
}

function StatCard({
  icon: Icon, label, value, accent,
}: { icon: LucideIcon; label: string; value: number | null; accent: string }) {
  const hasValue = value != null;
  const positive = hasValue && value >= 0;
  return (
    <div className="rounded-xl p-4 border" style={{ borderColor: C.border2, background: hasValue ? `${accent}0d` : "#fafafa" }}>
      <div className="flex items-center gap-2 mb-2">
        <div className="w-7 h-7 rounded-lg flex items-center justify-center" style={{ backgroundColor: hasValue ? `${accent}22` : "#eeeeee" }}>
          <Icon size={14} style={{ color: hasValue ? accent : C.muted }} />
        </div>
        <span className="text-xs font-medium text-gray-500">{label}</span>
      </div>
      <div className="flex items-baseline gap-1">
        <span className="text-2xl font-semibold" style={{ color: hasValue ? (positive ? C.green : C.red) : C.faint }}>
          {hasValue ? `${value!.toFixed(2)}%` : "—"}
        </span>
        {hasValue && (positive ? <ArrowUpRight size={16} style={{ color: C.green }} /> : <ArrowDownRight size={16} style={{ color: C.red }} />)}
      </div>
    </div>
  );
}

/**
 * A row of three cards — same visual language as the EXPIRY/ENTRY/EXIT row
 * further down this page (rounded-xl border, small uppercase icon+label
 * header, value below) — but sized a bit roomier (p-4/text-lg, not
 * p-3.5/text-sm) since each availability card also carries a second
 * surplus/shortfall line. Required is neutral (it's just a fact, not a
 * pass/fail); Paper/Live are colored green when that pool alone covers
 * the requirement, red when it doesn't, and gray "—" while unknown
 * (e.g. the live funds API didn't return a figure this time) — never red
 * for "unknown," only for a confirmed shortfall.
 */
function MarginRow({ margin, loading }: { margin: MarginResponse | null; loading: boolean }) {
  const hasRequired = !loading && margin != null && margin.margin_required > 0;

  const poolCard = (label: string, Icon: LucideIcon, status: { available_balance: number | null; sufficient: boolean | null }) => {
    const known = status.sufficient != null && status.available_balance != null;
    const color = !known ? C.muted : status.sufficient ? C.green : C.red;
    const bg = !known ? "#fafafa" : status.sufficient ? `${C.green}0d` : `${C.red}0d`;
    const diff = known && hasRequired ? status.available_balance! - margin!.margin_required : null;
    return (
      <div key={label} className="rounded-xl border p-4" style={{ borderColor: C.border2, background: bg }}>
        <div className="flex items-center gap-1.5 text-[11px] font-medium text-gray-400 mb-1.5"><Icon size={12} /> {label.toUpperCase()} AVAILABLE</div>
        <div className="text-lg font-semibold" style={{ color: known ? color : C.faint }}>
          {loading ? "…" : status.available_balance != null ? `₹${inr(status.available_balance, 0)}` : "—"}
        </div>
        <div className="text-[11px] font-medium mt-1" style={{ color: diff != null ? color : C.faint }}>
          {diff == null ? "—" : diff >= 0 ? `+₹${inr(diff, 0)} surplus` : `-₹${inr(Math.abs(diff), 0)} short`}
        </div>
      </div>
    );
  };

  return (
    <div className="grid grid-cols-3 gap-4">
      <div className="rounded-xl border p-4" style={{ borderColor: C.border2 }}>
        <div className="flex items-center gap-1.5 text-[11px] font-medium text-gray-400 mb-1.5"><Wallet size={12} /> MARGIN REQUIRED</div>
        <div className="text-lg font-semibold" style={{ color: hasRequired ? C.text : C.faint }}>
          {loading ? "…" : hasRequired ? `₹${inr(margin!.margin_required, 0)}` : "—"}
        </div>
        <div className="text-[11px] font-medium mt-1 text-transparent select-none">spacer</div>
      </div>
      {poolCard("Paper", Activity, margin?.paper ?? { available_balance: null, sufficient: null })}
      {poolCard("Live", Target, margin?.live ?? { available_balance: null, sufficient: null })}
    </div>
  );
}

function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

const BACKTEST_PRESETS: { label: string; from: string | null; to: string | null }[] = [
  { label: "Last 6 Months", from: isoDaysAgo(182), to: null },
  { label: "Last 1 Year", from: isoDaysAgo(365), to: null },
  { label: "Last 3 Years", from: isoDaysAgo(365 * 3), to: null },
  { label: "All Available Data", from: null, to: null },
];

function BacktestRangeModal({
  strategy, onClose, onRun,
}: { strategy: CustomStrategy; onClose: () => void; onRun: (fromDate: string, toDate: string, slippagePct: number) => void }) {
  const [fromDate, setFromDate] = useState(BACKTEST_PRESETS[1].from || "");
  const [toDate, setToDate] = useState("");
  const [slippagePct, setSlippagePct] = useState(0.1);
  const [activePreset, setActivePreset] = useState<string | null>(BACKTEST_PRESETS[1].label);

  const applyPreset = (preset: typeof BACKTEST_PRESETS[number]) => {
    setFromDate(preset.from || "");
    setToDate(preset.to || "");
    setActivePreset(preset.label);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-40 backdrop-blur-sm">
      <div className="bg-white rounded-2xl p-6 max-w-md w-full mx-4 border shadow-2xl" style={{ borderColor: C.border }}>
        <h3 className="text-base font-bold text-gray-800">Backtest "{strategy.name}"</h3>
        <p className="text-xs text-gray-500 mt-1.5">Choose the historical date range to simulate over real past expiry cycles.</p>

        <div className="flex flex-wrap gap-1.5 mt-4">
          {BACKTEST_PRESETS.map((preset) => (
            <button
              key={preset.label}
              onClick={() => applyPreset(preset)}
              className="px-2.5 py-1 rounded-full text-[11px] font-semibold transition-colors focus:outline-none"
              style={activePreset === preset.label
                ? { backgroundColor: C.orange, color: "#fff" }
                : { backgroundColor: C.hover, color: C.text }}
            >
              {preset.label}
            </button>
          ))}
        </div>

        <div className="grid grid-cols-2 gap-3 mt-4">
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1.5">From</label>
            <DatePicker value={fromDate} onChange={(v) => { setFromDate(v); setActivePreset(null); }} allowClear maxDate={toDate || undefined} placeholder="Earliest available" />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1.5">To</label>
            <DatePicker value={toDate} onChange={(v) => { setToDate(v); setActivePreset(null); }} allowClear minDate={fromDate || undefined} placeholder="Today" />
          </div>
        </div>

        <div className="mt-4">
          <label className="block text-xs font-medium text-gray-500 mb-1.5">Slippage (% per trade)</label>
          <input
            type="number"
            step="0.01"
            min="0"
            max="5"
            value={slippagePct}
            onChange={(e) => setSlippagePct(parseFloat(e.target.value) || 0)}
            className="w-full px-3 py-1.5 border rounded-lg text-xs font-semibold outline-none focus:border-orange-500 transition-colors"
            style={{ borderColor: C.border2 }}
          />
        </div>

        <div className="flex justify-end gap-2 mt-6">
          <button
            onClick={onClose}
            className="px-3.5 py-1.5 text-xs font-semibold rounded-lg bg-gray-100 text-gray-600 hover:bg-gray-200 transition-colors focus:outline-none"
          >
            Cancel
          </button>
          <button
            onClick={() => onRun(fromDate, toDate, slippagePct)}
            className="flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-semibold rounded-lg text-white transition-colors focus:outline-none hover:opacity-90"
            style={{ backgroundColor: C.orange }}
          >
            <BarChart3 size={13} /> Run Backtest
          </button>
        </div>
      </div>
    </div>
  );
}

function EquityCurveChart({ curve }: { curve: number[] }) {
  if (curve.length < 2) return null;
  const w = 640, h = 120, pad = 8;
  const min = Math.min(0, ...curve);
  const max = Math.max(0, ...curve);
  const range = max - min || 1;
  const x = (i: number) => pad + (i / (curve.length - 1)) * (w - pad * 2);
  const y = (v: number) => h - pad - ((v - min) / range) * (h - pad * 2);
  const zeroY = y(0);
  const points = curve.map((v, i) => `${x(i)},${y(v)}`).join(" ");
  const last = curve[curve.length - 1];
  const isUp = last >= 0;
  const areaPoints = `${x(0)},${zeroY} ${points} ${x(curve.length - 1)},${zeroY}`;

  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full" style={{ height: 120 }} preserveAspectRatio="none">
      <line x1={pad} y1={zeroY} x2={w - pad} y2={zeroY} stroke={C.border2} strokeWidth={1} strokeDasharray="3,3" />
      <polygon points={areaPoints} fill={isUp ? C.green : C.red} opacity={0.08} />
      <polyline points={points} fill="none" stroke={isUp ? C.green : C.red} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

function BacktestStatTile({ label, value, positive }: { label: string; value: string; positive?: boolean | null }) {
  const color = positive == null ? C.text : positive ? C.green : C.red;
  return (
    <div className="px-3 py-2 rounded-lg border" style={{ borderColor: C.border2, background: C.hover }}>
      <div className="text-[10px] uppercase tracking-wide text-gray-400 mb-0.5">{label}</div>
      <div className="text-sm font-semibold" style={{ color }}>{value}</div>
    </div>
  );
}

const SAMPLE_SIZE_COPY: Record<string, string> = {
  very_limited: "Fewer than 5 cycles tested — not enough history to draw any real conclusion from these numbers.",
  limited: "Fewer than 20 cycles tested — treat these stats as a rough signal, not a reliable estimate.",
};

function BacktestStatsPanel({ result }: { result: BacktestResult }) {
  const hasStats = result.total_net_pnl !== undefined;
  if (!hasStats) return null;
  return (
    <div className="px-6 py-4 border-b" style={{ borderColor: C.border2 }}>
      {result.sample_size_warning && (
        <div className="flex items-center gap-2 px-3 py-2 mb-4 rounded-lg text-xs" style={{ background: "#fff9e6", color: "#a16a00" }}>
          <AlertCircle size={13} className="shrink-0" />
          {SAMPLE_SIZE_COPY[result.sample_size_warning]}
        </div>
      )}

      {result.equity_curve_compounded && result.equity_curve_compounded.length >= 2 ? (
        <div className="mb-4">
          <div className="text-[11px] uppercase tracking-wide text-gray-400 mb-1.5">Equity Curve (₹100,000 notional, compounded) &amp; Drawdown</div>
          <BacktestEquityChart
            equityCurve={result.equity_curve_compounded}
            dates={result.cycles.map((c) => c.exit_date)}
          />
        </div>
      ) : result.equity_curve && result.equity_curve.length >= 2 && (
        <div className="mb-4">
          <div className="text-[11px] uppercase tracking-wide text-gray-400 mb-1.5">Equity Curve (cumulative % of premium)</div>
          <EquityCurveChart curve={result.equity_curve} />
        </div>
      )}

      <div className="text-[11px] uppercase tracking-wide text-gray-400 mb-1.5 mt-1">Return &amp; Risk</div>
      <div className="grid grid-cols-4 gap-2 mb-3">
        <BacktestStatTile label="Total Return" value={result.total_return_pct != null ? `${result.total_return_pct.toFixed(2)}%` : "—"} positive={result.total_return_pct != null ? result.total_return_pct >= 0 : null} />
        <BacktestStatTile label="CAGR" value={result.cagr_pct != null ? `${result.cagr_pct.toFixed(2)}%` : "—"} positive={result.cagr_pct != null ? result.cagr_pct >= 0 : null} />
        <BacktestStatTile label="Sharpe Ratio" value={result.sharpe_ratio != null ? result.sharpe_ratio.toFixed(2) : "—"} positive={result.sharpe_ratio != null ? result.sharpe_ratio >= 0 : null} />
        <BacktestStatTile label="Sortino Ratio" value={result.sortino_ratio != null ? result.sortino_ratio.toFixed(2) : "—"} positive={result.sortino_ratio != null ? result.sortino_ratio >= 0 : null} />
        <BacktestStatTile label="Calmar Ratio" value={result.calmar_ratio != null ? result.calmar_ratio.toFixed(2) : "—"} positive={result.calmar_ratio != null ? result.calmar_ratio >= 0 : null} />
        <BacktestStatTile
          label="Max Drawdown Duration"
          value={result.max_drawdown_duration_days != null ? `${result.max_drawdown_duration_days}d${result.max_drawdown_ongoing ? "+ (ongoing)" : ""}` : "—"}
          positive={false}
        />
        <BacktestStatTile label="Exposure" value={result.exposure_pct != null ? `${result.exposure_pct.toFixed(1)}%` : "—"} />
        <BacktestStatTile
          label="vs NIFTY 50 (Alpha)"
          value={result.alpha_pct != null ? `${result.alpha_pct >= 0 ? "+" : ""}${result.alpha_pct.toFixed(2)}%` : "—"}
          positive={result.alpha_pct != null ? result.alpha_pct >= 0 : null}
        />
      </div>

      <div className="text-[11px] uppercase tracking-wide text-gray-400 mb-1.5">Trade Stats</div>
      <div className="grid grid-cols-4 gap-2">
        <BacktestStatTile label="Total Net P&L" value={`${(result.total_net_pnl ?? 0) < 0 ? "-" : ""}₹${Math.abs(result.total_net_pnl ?? 0).toFixed(2)}`} positive={(result.total_net_pnl ?? 0) >= 0} />
        <BacktestStatTile label="Max Drawdown" value={`${(result.max_drawdown_pct ?? 0).toFixed(2)}%`} positive={false} />
        <BacktestStatTile label="Profit Factor" value={result.profit_factor != null ? result.profit_factor.toFixed(2) : "No losses"} positive={result.profit_factor != null ? result.profit_factor >= 1 : true} />
        <BacktestStatTile label="Best Cycle" value={result.best_cycle_pct != null ? `${result.best_cycle_pct.toFixed(2)}%` : "—"} positive />
        <BacktestStatTile label="Worst Cycle" value={result.worst_cycle_pct != null ? `${result.worst_cycle_pct.toFixed(2)}%` : "—"} positive={false} />
        <BacktestStatTile label="Avg Win" value={result.avg_win_pct != null ? `${result.avg_win_pct.toFixed(2)}%` : "—"} positive />
        <BacktestStatTile label="Avg Loss" value={result.avg_loss_pct != null ? `${result.avg_loss_pct.toFixed(2)}%` : "—"} positive={false} />
        <BacktestStatTile label="Max Streak (W / L)" value={`${result.max_consecutive_wins ?? 0} / ${result.max_consecutive_losses ?? 0}`} />
      </div>

      {result.per_symbol && Object.keys(result.per_symbol).length > 1 && (
        <div className="mt-4">
          <div className="text-[11px] uppercase tracking-wide text-gray-400 mb-1.5">Per Symbol</div>
          <div className="flex flex-wrap gap-2">
            {Object.entries(result.per_symbol).map(([sym, s]) => (
              <div key={sym} className="px-3 py-1.5 rounded-lg border text-xs" style={{ borderColor: C.border2 }}>
                <span className="font-semibold text-gray-700">{sym}</span>
                <span className="text-gray-400"> · {s.cycles_tested} cycles · </span>
                <span style={{ color: s.avg_return_pct >= 0 ? C.green : C.red }}>{s.avg_return_pct >= 0 ? "+" : ""}{s.avg_return_pct.toFixed(2)}%</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <p className="text-[10px] text-gray-400 mt-4">
        Based on daily EOD NSE bhavcopy data with simulated slippage — not tick-accurate. Benchmark is NIFTY 50 buy-and-hold over the same date range.
      </p>
    </div>
  );
}

function cyclesToCsv(cycles: BacktestCycle[]): string {
  const header = ["symbol", "entry_date", "exit_date", "exit_reason", "net_pnl", "pnl_pct_of_premium", "won", "liquid"];
  const rows = cycles.map((c) => [
    c.symbol ?? "", c.entry_date, c.exit_date, c.exit_reason,
    c.net_pnl.toFixed(2), c.pnl_pct_of_premium.toFixed(2), c.won ? "true" : "false", c.liquid ? "true" : "false",
  ]);
  return [header, ...rows].map((r) => r.join(",")).join("\n");
}

function downloadCsv(filename: string, content: string) {
  const blob = new Blob([content], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

type OutcomeFilter = "all" | "won" | "lost";

function BacktestResultsModal({
  strategyName, result, onClose,
}: { strategyName: string; result: BacktestResult; onClose: () => void }) {
  const [outcomeFilter, setOutcomeFilter] = useState<OutcomeFilter>("all");
  const [liquidOnly, setLiquidOnly] = useState(false);
  const [symbolFilter, setSymbolFilter] = useState<string>("all");

  const symbols = Array.from(new Set(result.cycles.map((c) => c.symbol).filter(Boolean))) as string[];
  const baseFilteredCycles = result.cycles.filter((c) =>
    (outcomeFilter === "all" || (outcomeFilter === "won") === c.won) &&
    (symbolFilter === "all" || c.symbol === symbolFilter)
  );
  const filteredCycles = baseFilteredCycles.filter((c) => !liquidOnly || c.liquid);
  // "Liquid only" hid every cycle these OTHER filters would've shown — worth telling the
  // user why, rather than the generic empty state (see the Info tooltip on the toggle
  // itself for what "liquid" means: every leg traded a nonzero volume on its exit day).
  const liquidOnlyHidAll = liquidOnly && filteredCycles.length === 0 && baseFilteredCycles.length > 0;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-40 backdrop-blur-sm p-4">
      <div className="bg-white rounded-2xl w-full max-w-3xl max-h-[85vh] flex flex-col border shadow-2xl" style={{ borderColor: C.border }}>
        <div className="px-6 py-4 border-b flex items-start justify-between shrink-0" style={{ borderColor: C.border2 }}>
          <div>
            <h3 className="text-base font-bold text-gray-800 flex items-center gap-2"><TrendingUp size={16} style={{ color: C.blue }} /> Backtest Results — {strategyName}</h3>
            <div className="flex items-center gap-4 mt-2 flex-wrap">
              <span className="text-xs text-gray-500">{result.cycles_tested} historical expiry cycles</span>
              {(result.from_date || result.to_date) && (
                <span className="text-xs text-gray-500">{result.from_date ? fmtDate(result.from_date) : "Earliest"} → {result.to_date ? fmtDate(result.to_date) : "Today"}</span>
              )}
              <span className="text-xs text-gray-500">Win rate <span className="font-semibold text-gray-700">{result.win_rate_pct.toFixed(1)}%</span></span>
              <span className="text-xs font-semibold px-2 py-0.5 rounded-full" style={{ background: result.avg_return_pct_of_premium >= 0 ? "#e8f7ec" : C.sellBg, color: result.avg_return_pct_of_premium >= 0 ? C.green : C.red }}>
                Avg {result.avg_return_pct_of_premium.toFixed(2)}% / cycle
              </span>
              {result.run_at && <span className="text-[11px] text-gray-400">Run {fmtDateTime(result.run_at)}</span>}
            </div>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 focus:outline-none shrink-0" aria-label="Close"><X size={20} /></button>
        </div>
        <div className="overflow-y-auto flex-1">
          <BacktestStatsPanel result={result} />

          <div className="flex items-center justify-between gap-2 px-4 py-2.5 border-b flex-wrap" style={{ borderColor: C.border2 }}>
            <div className="flex items-center gap-1.5 flex-wrap">
              {(["all", "won", "lost"] as OutcomeFilter[]).map((f) => (
                <button key={f} onClick={() => setOutcomeFilter(f)}
                  className="px-2.5 py-1 rounded-full text-[11px] font-semibold transition-colors focus:outline-none"
                  style={outcomeFilter === f ? { backgroundColor: C.orange, color: "#fff" } : { backgroundColor: C.hover, color: C.text }}>
                  {f === "all" ? "All" : f === "won" ? "Won" : "Lost"}
                </button>
              ))}
              <button onClick={() => setLiquidOnly((v) => !v)}
                className="flex items-center gap-1 px-2.5 py-1 rounded-full text-[11px] font-semibold transition-colors focus:outline-none"
                style={liquidOnly ? { backgroundColor: C.orange, color: "#fff" } : { backgroundColor: C.hover, color: C.text }}>
                Liquid only
                <span title="Only show cycles where EVERY leg traded a nonzero volume (contracts) in NSE's real historical data on its exit day. Deep-OTM/far-hedge legs commonly show zero traded volume on any given day — that's real market illiquidity, not a bug, so this filter can legitimately hide most or all cycles for a wide-hedge strategy.">
                  <Info size={11} style={{ color: liquidOnly ? "#fff" : C.faint, opacity: 0.85 }} />
                </span>
              </button>
              {symbols.length > 1 && (
                <select value={symbolFilter} onChange={(e) => setSymbolFilter(e.target.value)}
                  className="px-2 py-1 rounded-full text-[11px] font-semibold border focus:outline-none" style={{ borderColor: C.border2 }}>
                  <option value="all">All symbols</option>
                  {symbols.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              )}
            </div>
            <button onClick={() => downloadCsv(`backtest_${strategyName.replace(/\s+/g, "_")}_${result.run_id ?? "latest"}.csv`, cyclesToCsv(filteredCycles))}
              className="flex items-center gap-1 text-[11px] font-semibold hover:underline focus:outline-none" style={{ color: C.blue }}>
              <Download size={12} /> Export CSV
            </button>
          </div>

          <table className="w-full text-sm">
            <thead>
              <tr className="text-[11px] uppercase tracking-wide text-gray-400 border-b sticky top-0" style={{ borderColor: C.border2, background: C.tableHeaderBg }}>
                {symbols.length > 1 && <th className="px-4 py-2.5 text-left font-medium">Symbol</th>}
                <th className="px-4 py-2.5 text-left font-medium">Entry</th>
                <th className="px-4 py-2.5 text-left font-medium">Exit</th>
                <th className="px-4 py-2.5 text-left font-medium">Reason</th>
                <th className="px-4 py-2.5 text-right font-medium">Net P&amp;L</th>
                <th className="px-4 py-2.5 text-right font-medium">% of Premium</th>
                <th className="px-4 py-2.5 text-center font-medium">Liquid</th>
              </tr>
            </thead>
            <tbody>
              {filteredCycles.length === 0 ? (
                <tr><td colSpan={symbols.length > 1 ? 7 : 6} className="px-4 py-8 text-center text-xs text-gray-400">
                  {liquidOnlyHidAll ? (
                    <>
                      "Liquid only" hid all {baseFilteredCycles.length} matching cycle{baseFilteredCycles.length === 1 ? "" : "s"} — at least one leg
                      traded zero volume on its exit day in every one of them (common for far-OTM/deep-hedge legs; real historical illiquidity, not a bug).{" "}
                      <button onClick={() => setLiquidOnly(false)} className="font-semibold hover:underline focus:outline-none" style={{ color: C.blue }}>Turn it off</button> to see them.
                    </>
                  ) : "No cycles match these filters."}
                </td></tr>
              ) : filteredCycles.map((c, i) => (
                <tr key={i} className="border-b last:border-0 text-xs" style={{ borderColor: C.border }}>
                  {symbols.length > 1 && <td className="px-4 py-2.5 font-semibold text-gray-600">{c.symbol}</td>}
                  <td className="px-4 py-2.5">{fmtDate(c.entry_date)}</td>
                  <td className="px-4 py-2.5">{fmtDate(c.exit_date)}</td>
                  <td className="px-4 py-2.5">
                    <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold" style={{ background: C.hover, color: C.muted }}>{c.exit_reason}</span>
                  </td>
                  <td className="px-4 py-2.5 text-right font-semibold" style={{ color: c.net_pnl >= 0 ? C.green : C.red }}>{c.net_pnl < 0 ? "-" : ""}₹{Math.abs(c.net_pnl).toFixed(2)}</td>
                  <td className="px-4 py-2.5 text-right">{c.pnl_pct_of_premium.toFixed(2)}%</td>
                  <td className="px-4 py-2.5 text-center">{c.liquid ? <span style={{ color: C.green }}>✓</span> : <span style={{ color: C.faint }}>—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

const RUN_STATUS_META: Record<string, { bg: string; fg: string; label: string }> = {
  QUEUED: { bg: "#f1f2f4", fg: "#5f6672", label: "Queued" },
  RUNNING: { bg: "#eaf1ff", fg: "#2f5fd6", label: "Running" },
  COMPLETED: { bg: "#e8f7ec", fg: "#1f8a3d", label: "Completed" },
  FAILED: { bg: "#fdeceb", fg: "#c22b26", label: "Failed" },
};

function BacktestRunHistoryPanel({
  runs, compareRunIds, onToggleCompare, onViewRun,
}: { runs: BacktestRunSummary[]; compareRunIds: number[]; onToggleCompare: (runId: number) => void; onViewRun: (runId: number) => void }) {
  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Clock size={14} style={{ color: C.muted }} />
          <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500">Backtest Run History</h3>
        </div>
        {compareRunIds.length > 0 && (
          <span className="text-[11px] text-gray-400">{compareRunIds.length}/2 selected to compare</span>
        )}
      </div>
      <div className="overflow-x-auto border rounded-xl" style={{ borderColor: C.border2 }}>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-[11px] uppercase tracking-wide text-gray-400 border-b" style={{ borderColor: C.border2, background: C.tableHeaderBg }}>
              <th className="px-3 py-2 text-center font-medium w-10">
                <GitCompare size={12} className="mx-auto" style={{ color: C.muted }} />
              </th>
              <th className="px-4 py-2 text-left font-medium">Run</th>
              <th className="px-4 py-2 text-left font-medium">Range</th>
              <th className="px-4 py-2 text-left font-medium">Status</th>
              <th className="px-4 py-2 text-right font-medium w-16" />
            </tr>
          </thead>
          <tbody>
            {runs.slice(0, 10).map((r) => {
              const meta = RUN_STATUS_META[r.status] || RUN_STATUS_META.QUEUED;
              return (
                <tr key={r.run_id} className="border-b last:border-0" style={{ borderColor: C.border }}>
                  <td className="px-3 py-2 text-center">
                    {r.status === "COMPLETED" && (
                      <input type="checkbox" checked={compareRunIds.includes(r.run_id)} onChange={() => onToggleCompare(r.run_id)}
                        className="rounded border-gray-300 text-orange-500 focus:ring-orange-500" />
                    )}
                  </td>
                  <td className="px-4 py-2 text-xs text-gray-700">{r.created_at ? fmtDateTime(r.created_at) : "—"}</td>
                  <td className="px-4 py-2 text-xs text-gray-500">
                    {r.from_date ? fmtDate(r.from_date) : "Earliest"} → {r.to_date ? fmtDate(r.to_date) : "Today"}
                  </td>
                  <td className="px-4 py-2">
                    <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-semibold" style={{ backgroundColor: meta.bg, color: meta.fg }}>
                      {r.status === "RUNNING" && r.progress_total ? `${Math.round((r.progress_current / r.progress_total) * 100)}%` : meta.label}
                    </span>
                    {r.status === "FAILED" && r.error_message && (
                      <span className="block text-[10px] text-gray-400 mt-0.5 max-w-xs truncate" title={r.error_message}>{r.error_message}</span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-right">
                    {r.status === "COMPLETED" && (
                      <button onClick={() => onViewRun(r.run_id)} className="text-gray-400 hover:text-orange-500 transition-colors focus:outline-none" title="View results">
                        <Eye size={14} />
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function BacktestCompareModal({
  strategyId, runIds, runs, onClose,
}: { strategyId: number; runIds: number[]; runs: BacktestRunSummary[]; onClose: () => void }) {
  const [results, setResults] = useState<Record<number, BacktestResult>>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    (async () => {
      const entries = await Promise.all(runIds.map(async (id) => {
        try {
          const detail: BacktestRunDetail = await api.getCustomStrategyBacktestRun(strategyId, id);
          return detail.result ? [id, detail.result] as const : null;
        } catch {
          return null;
        }
      }));
      if (!cancelled) {
        setResults(Object.fromEntries(entries.filter((e): e is [number, BacktestResult] => e !== null)));
        setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [strategyId, runIds]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-40 backdrop-blur-sm p-4">
      <div className="bg-white rounded-2xl w-full max-w-4xl max-h-[85vh] flex flex-col border shadow-2xl" style={{ borderColor: C.border }}>
        <div className="px-6 py-4 border-b flex items-center justify-between shrink-0" style={{ borderColor: C.border2 }}>
          <h3 className="text-base font-bold text-gray-800 flex items-center gap-2"><GitCompare size={16} style={{ color: C.blue }} /> Compare Backtest Runs</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 focus:outline-none" aria-label="Close"><X size={20} /></button>
        </div>
        <div className="overflow-y-auto flex-1 p-6">
          {loading ? (
            <div className="flex items-center justify-center py-16"><RefreshCw className="animate-spin" size={20} style={{ color: C.orange }} /></div>
          ) : (
            <div className="grid grid-cols-2 gap-5">
              {runIds.map((id) => {
                const result = results[id];
                const run = runs.find((r) => r.run_id === id);
                if (!result || !run) return <div key={id} className="text-xs text-gray-400">Result unavailable.</div>;
                return (
                  <div key={id} className="border rounded-xl p-4" style={{ borderColor: C.border2 }}>
                    <div className="text-xs font-semibold text-gray-700 mb-1">{fmtDateTime(run.created_at || "")}</div>
                    <div className="text-[11px] text-gray-400 mb-3">
                      {run.from_date ? fmtDate(run.from_date) : "Earliest"} → {run.to_date ? fmtDate(run.to_date) : "Today"}
                    </div>
                    {result.equity_curve_compounded && result.equity_curve_compounded.length >= 2 && (
                      <div className="mb-3">
                        <BacktestEquityChart equityCurve={result.equity_curve_compounded} dates={result.cycles.map((c) => c.exit_date)} height={180} />
                      </div>
                    )}
                    <div className="grid grid-cols-2 gap-2 text-xs">
                      <div><span className="text-gray-400">Cycles</span> <span className="font-semibold text-gray-700">{result.cycles_tested}</span></div>
                      <div><span className="text-gray-400">Win Rate</span> <span className="font-semibold text-gray-700">{result.win_rate_pct.toFixed(1)}%</span></div>
                      <div><span className="text-gray-400">Total Return</span> <span className="font-semibold" style={{ color: (result.total_return_pct ?? 0) >= 0 ? C.green : C.red }}>{result.total_return_pct != null ? `${result.total_return_pct.toFixed(2)}%` : "—"}</span></div>
                      <div><span className="text-gray-400">CAGR</span> <span className="font-semibold text-gray-700">{result.cagr_pct != null ? `${result.cagr_pct.toFixed(2)}%` : "—"}</span></div>
                      <div><span className="text-gray-400">Sharpe</span> <span className="font-semibold text-gray-700">{result.sharpe_ratio != null ? result.sharpe_ratio.toFixed(2) : "—"}</span></div>
                      <div><span className="text-gray-400">Max DD</span> <span className="font-semibold text-gray-700">{(result.max_drawdown_pct ?? 0).toFixed(2)}%</span></div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function StrategiesView({
  strategies, loading, statusUpdatingId, deletingId,
  onRefresh, onCreate, onUpdate, onStatusChange, onDelete,
}: StrategiesViewProps) {
  // Only read from the (commented-out) delete buttons below — kept
  // referenced so it doesn't trip noUnusedLocals while they're disabled.
  void deletingId;
  const toast = useToast();
  const [selectedStrategy, setSelectedStrategy] = useState<CustomStrategy | null>(null);
  const [modalMode, setModalMode] = useState<null | "create" | "edit">(null);
  const [backtesting, setBacktesting] = useState(false);
  const [backtestProgress, setBacktestProgress] = useState<{ current: number; total: number } | null>(null);
  const [backtestResult, setBacktestResult] = useState<BacktestResult | null>(null);
  const [backtestError, setBacktestError] = useState("");
  const [showBacktestModal, setShowBacktestModal] = useState(false);
  const [backtestRuns, setBacktestRuns] = useState<BacktestRunSummary[]>([]);
  const [compareRunIds, setCompareRunIds] = useState<number[]>([]);
  const [strategiesPage, setStrategiesPage] = useState(1);
  const STRATEGIES_PER_PAGE = 10;
  const [liveGreeks, setLiveGreeks] = useState<LiveGreeksResponse | null>(null);
  const [expiryDatePreview, setExpiryDatePreview] = useState<string | null>(null);
  const [portfolioGreeks, setPortfolioGreeks] = useState<PortfolioGreeksResponse | null>(null);

  // Account-wide Greeks — net exposure across EVERY open leg of EVERY
  // active strategy, not just the currently-selected one (see
  // liveGreeks above, and api/live_greeks.py::compute_portfolio_greeks
  // for why this is a materially different, more useful number). A
  // plain interval poll, not a WebSocket — this is a summary card, not
  // something that needs sub-second freshness.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const data = await api.getPortfolioGreeks();
        if (!cancelled) setPortfolioGreeks(data);
      } catch {
        // Supplementary widget — silently leave the last-known value on a transient failure.
      }
    };
    load();
    const interval = setInterval(load, 15000);
    return () => { cancelled = true; clearInterval(interval); };
  }, []);

  const [portfolioMargin, setPortfolioMargin] = useState<PortfolioMarginResponse | null>(null);

  // Real broker-netted margin currently blocked across EVERY open leg of
  // EVERY active strategy, split paper vs live — see api/live_greeks.py::
  // compute_portfolio_margin. Same coarse polling as portfolioGreeks above.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const data = await api.getPortfolioMargin();
        if (!cancelled) setPortfolioMargin(data);
      } catch {
        // Supplementary widget — silently leave the last-known value on a transient failure.
      }
    };
    load();
    const interval = setInterval(load, 15000);
    return () => { cancelled = true; clearInterval(interval); };
  }, []);

  // Keep the selection valid whenever the redux-backed `strategies` list
  // changes (initial load, create, update, status change, delete) — same
  // "keep it if it still exists, else fall back to the first one" logic
  // the old local loadStrategies() used to run inline after every fetch.
  useEffect(() => {
    setSelectedStrategy((prev) => (strategies.find((s) => s.id === prev?.id) || strategies[0]) ?? null);
  }, [strategies]);

  const strategiesTotalPages = Math.max(1, Math.ceil(strategies.length / STRATEGIES_PER_PAGE));
  useEffect(() => {
    if (strategiesPage > strategiesTotalPages) setStrategiesPage(strategiesTotalPages);
  }, [strategies.length, strategiesTotalPages, strategiesPage]);
  const paginatedStrategies = strategies.slice((strategiesPage - 1) * STRATEGIES_PER_PAGE, strategiesPage * STRATEGIES_PER_PAGE);

  useEffect(() => {
    setExpiryDatePreview(null);
    const symbol = selectedStrategy?.symbols[0];
    if (!symbol) return;
    let cancelled = false;
    (async () => {
      try {
        const data = await api.getCustomStrategyTemplateExpiries(symbol);
        if (cancelled) return;
        const expiries = data.expiries || [];
        const mode = selectedStrategy?.rules?.expiry?.mode || "WEEKLY";
        const match = mode === "MONTHLY" ? expiries.find((e) => e.label === "Monthly") : expiries[0];
        if (match && !cancelled) setExpiryDatePreview(match.date);
      } catch {
        // Preview-only — silently leave it unset, the mode label alone is still shown.
      }
    })();
    return () => { cancelled = true; };
  }, [selectedStrategy]);

  const [payoff, setPayoff] = useState<PayoffResponse | null>(null);
  const [payoffLoading, setPayoffLoading] = useState(false);
  const [ivShift, setIvShift] = useState(0);
  const [daysRemaining, setDaysRemaining] = useState<number | null>(null);

  // Bumped on every call (useEffect-triggered OR the manual Refresh button
  // below) — a response only gets applied if it's still the MOST RECENT
  // request when it resolves, so switching strategies quickly (or hitting
  // Refresh mid-flight) can never let a stale, late response overwrite
  // what's currently selected.
  const payoffRequestIdRef = useRef(0);
  const fetchPayoff = async (strategy: CustomStrategy) => {
    const requestId = ++payoffRequestIdRef.current;
    setPayoffLoading(true);
    try {
      const data = await api.getCustomStrategyPayoff(strategy.id);
      if (payoffRequestIdRef.current === requestId) setPayoff(data);
    } catch {
      if (payoffRequestIdRef.current === requestId) setPayoff(null);
    } finally {
      if (payoffRequestIdRef.current === requestId) setPayoffLoading(false);
    }
  };

  useEffect(() => {
    setPayoff(null);
    setIvShift(0);
    setDaysRemaining(null);
    // Non-leg-based strategies (see NON_LEG_STRATEGY_TYPES) have a
    // completely different rules shape (no legs/entry/exit) that this
    // payoff calculator doesn't apply to at all.
    if (selectedStrategy && selectedStrategy.rules && isLegBased(selectedStrategy)) fetchPayoff(selectedStrategy);
  }, [selectedStrategy]);

  const [margin, setMargin] = useState<MarginResponse | null>(null);
  const [marginLoading, setMarginLoading] = useState(false);

  // Same stale-response guard as fetchPayoff above.
  const marginRequestIdRef = useRef(0);
  const fetchMargin = async (strategy: CustomStrategy) => {
    const requestId = ++marginRequestIdRef.current;
    setMarginLoading(true);
    try {
      const data = await api.getCustomStrategyMargin(strategy.id);
      if (marginRequestIdRef.current === requestId) setMargin(data);
    } catch {
      if (marginRequestIdRef.current === requestId) setMargin(null);
    } finally {
      if (marginRequestIdRef.current === requestId) setMarginLoading(false);
    }
  };

  useEffect(() => {
    setMargin(null);
    if (selectedStrategy && selectedStrategy.rules && isLegBased(selectedStrategy)) fetchMargin(selectedStrategy);
  }, [selectedStrategy]);

  useEffect(() => {
    if (payoff && Object.keys(payoff.symbols).length > 0) {
      const firstSym = Object.values(payoff.symbols)[0];
      if (firstSym && !firstSym.error && firstSym.expiry) {
        const totalDays = Math.max((new Date(firstSym.expiry).getTime() - new Date().getTime()) / (1000 * 3600 * 24), 1);
        setDaysRemaining(Math.round(totalDays));
      }
    }
  }, [payoff]);

  // Live (WebSocket-pushed) open legs across all strategies, filtered to
  // the one currently selected — real-time LTP/P&L, no polling.
  const liveRows = useCustomStrategyPositions();
  const liveOpenLegs = selectedStrategy ? liveRows.filter((r) => r.strategy_id === selectedStrategy.id) : [];

  const [closedLegs, setClosedLegs] = useState<PositionLeg[]>([]);
  // Net-of-real-charges total across every closed leg (from the backend —
  // the same compute_basket_pnl() math the leaderboard uses), so this
  // page's realized figure agrees with the leaderboard instead of the two
  // silently drifting with independent gross-only formulas.
  const [realizedNetPnl, setRealizedNetPnl] = useState<number | null>(null);
  const [positionsLoading, setPositionsLoading] = useState(false);

  // Same stale-response guard as fetchPayoff above.
  const closedLegsRequestIdRef = useRef(0);
  const fetchClosedLegs = async (strategy: CustomStrategy) => {
    const requestId = ++closedLegsRequestIdRef.current;
    setPositionsLoading(true);
    try {
      const data = await api.getCustomStrategyPositions(strategy.id);
      if (closedLegsRequestIdRef.current === requestId) {
        setClosedLegs(data.closed || []);
        setRealizedNetPnl(data.realized_net_pnl ?? null);
      }
    } catch {
      if (closedLegsRequestIdRef.current === requestId) {
        setClosedLegs([]);
        setRealizedNetPnl(null);
      }
    } finally {
      if (closedLegsRequestIdRef.current === requestId) setPositionsLoading(false);
    }
  };

  useEffect(() => {
    setClosedLegs([]);
    setRealizedNetPnl(null);
    if (selectedStrategy) fetchClosedLegs(selectedStrategy);
  }, [selectedStrategy]);

  // Event-driven refetch (not a poll): when the live open-leg count for
  // this strategy drops, a leg just closed (SL/TP/expiry/manual) — pull
  // the fresh closed-history row for it once, rather than guessing on a
  // timer.
  const prevOpenCountRef = useRef<number>(0);
  useEffect(() => {
    if (!selectedStrategy) return;
    if (liveOpenLegs.length < prevOpenCountRef.current) fetchClosedLegs(selectedStrategy);
    prevOpenCountRef.current = liveOpenLegs.length;
  }, [liveOpenLegs.length, selectedStrategy]);

  // Pull whatever backtest result is already stored for this strategy (if
  // any) as soon as it's selected — persists across page reloads/navigating
  // away and back, so "View Backtest Results" works without re-running.
  useEffect(() => {
    setBacktestResult(null);
    setBacktestError("");
    if (!selectedStrategy) return;
    let cancelled = false;
    (async () => {
      try {
        const data = await api.getCustomStrategyBacktestStatus(selectedStrategy.id);
        if (!cancelled) setBacktestResult(data);
      } catch {
        // No stored result yet — normal for a never-backtested strategy.
      }
    })();
    return () => { cancelled = true; };
  }, [selectedStrategy]);

  useEffect(() => {
    setBacktestRuns([]);
    setCompareRunIds([]);
    if (selectedStrategy) loadBacktestRuns(selectedStrategy);
  }, [selectedStrategy]);

  const [confirmModal, setConfirmModal] = useState<{
    title: string;
    message: string;
    detail?: ReactNode;
    confirmText?: string;
    cancelText?: string;
    confirmColor?: string;
    onConfirm: () => void;
  } | null>(null);
  const [backtestRangeTarget, setBacktestRangeTarget] = useState<CustomStrategy | null>(null);

  useEffect(() => {
    onRefresh();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const greeksWsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    setLiveGreeks(null);
    greeksWsRef.current?.close();
    greeksWsRef.current = null;
    if (!selectedStrategy || !["PAPER_TRADING", "LIVE"].includes(selectedStrategy.status)) return;

    let cancelled = false;
    let reconnectTimer: ReturnType<typeof setTimeout>;

    const connect = () => {
      if (cancelled) return;
      const ws = new WebSocket(wsUrl(`/ws/custom-strategy-greeks/${selectedStrategy.id}`));
      greeksWsRef.current = ws;
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === "greeks") setLiveGreeks(data);
        } catch {
          // A malformed/partial push shouldn't take down the whole handler — the
          // server pushes a fresh snapshot on its own timer regardless.
        }
      };
      ws.onclose = () => {
        if (!cancelled) reconnectTimer = setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();
    };
    connect();

    return () => {
      cancelled = true;
      clearTimeout(reconnectTimer);
      greeksWsRef.current?.close();
      greeksWsRef.current = null;
    };
  }, [selectedStrategy]);

  const handleStatusChange = async (strategy: CustomStrategy, newStatus: string) => {
    try {
      const updated = await onStatusChange(strategy.id, newStatus);
      if (selectedStrategy?.id === strategy.id) setSelectedStrategy(updated);
      toast.success(`Strategy "${strategy.name}" is now ${newStatus.replace("_", " ").toLowerCase()}`);
    } catch (err) {
      toast.error((Array.isArray(err) ? err.join(" ") : typeof err === "string" ? err : null) || "Failed to update status.");
    }
  };

  const loadBacktestRuns = async (strategy: CustomStrategy) => {
    try {
      const data = await api.getCustomStrategyBacktestRuns(strategy.id);
      setBacktestRuns(data.runs || []);
    } catch {
      // Run history is supplementary — silently leave the list as-is.
    }
  };

  const pollBacktestRun = async (strategy: CustomStrategy, runId: number, signal: AbortSignal) => {
    // Polls every 1.5s while QUEUED/RUNNING — a 20+ year monthly history
    // is a real background job now (see routes_custom_strategies.py's
    // async POST /backtest), not something that finishes within one request.
    // Uses an AbortSignal so polling stops cleanly when the component
    // unmounts or the user navigates away, preventing state updates on
    // an unmounted component and orphaned fetch loops.
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500));
      if (signal.aborted) return;
      let run: BacktestRunDetail;
      try {
        run = await api.getCustomStrategyBacktestRun(strategy.id, runId, { signal });
      } catch (err) {
        if ((err as Error).name === "AbortError") return;
        continue; // transient network hiccup — keep polling rather than giving up
      }

      if (run.progress_total != null) setBacktestProgress({ current: run.progress_current, total: run.progress_total });

      if (run.status === "COMPLETED" && run.result) {
        setBacktestResult(run.result);
        setShowBacktestModal(true);
        setBacktesting(false);
        setBacktestProgress(null);
        onRefresh();
        loadBacktestRuns(strategy);
        toast.success("Backtest completed successfully!");
        return;
      }
      if (run.status === "FAILED") {
        const errText = run.error_message || "Backtest failed.";
        setBacktestError(errText);
        setBacktesting(false);
        setBacktestProgress(null);
        loadBacktestRuns(strategy);
        toast.error(errText);
        return;
      }
      // else QUEUED/RUNNING — keep polling
    }
  };

  // Ref to the current poll's AbortController so navigation / unmount can cancel it.
  const backtestPollAbortRef = useRef<AbortController | null>(null);

  const runBacktest = async (strategy: CustomStrategy, fromDate?: string, toDate?: string, slippagePct?: number) => {
    // Cancel any previous in-flight poll before starting a new one
    backtestPollAbortRef.current?.abort();
    backtestPollAbortRef.current = new AbortController();
    setBacktesting(true);
    setBacktestProgress(null);
    setBacktestResult(null);
    setBacktestError("");
    try {
      const data = await api.runCustomStrategyBacktest(strategy.id, fromDate || null, toDate || null, slippagePct);
      if (data.run_id) {
        pollBacktestRun(strategy, data.run_id, backtestPollAbortRef.current.signal); // not awaited — polls in the background, updates state as it goes
      } else {
        const errText = "Backtest failed.";
        setBacktestError(errText);
        toast.error(errText);
        setBacktesting(false);
      }
    } catch (err) {
      const errText = err instanceof Error ? err.message : "Backtest request failed.";
      setBacktestError(errText);
      toast.error(errText);
      setBacktesting(false);
    }
  };

  // Abort ongoing backtest poll when the component unmounts.
  useEffect(() => {
    return () => { backtestPollAbortRef.current?.abort(); };
  }, []);

  // Delete buttons that call this are temporarily commented out (not
  // removed) below — this reference keeps both this function and the
  // Trash2 import "used" for noUnusedLocals while they're disabled.
  void Trash2;
  const handleDeleteStrategy = async (strategy: CustomStrategy) => {
    try {
      await onDelete(strategy.id);
      if (selectedStrategy?.id === strategy.id) {
        setBacktestResult(null);
        setBacktestError("");
      }
      toast.success("Strategy deleted successfully!");
    } catch (err) {
      toast.error((Array.isArray(err) ? err.join(" ") : typeof err === "string" ? err : null) || "Failed to delete strategy.");
    }
  };
  void handleDeleteStrategy;

  const canTransitionTo = (currentStatus: string, targetStatus: string) => {
    // Mirrors routes_custom_strategies.py::update_strategy_status's
    // valid_transitions exactly, including DRAFT -> PAPER_TRADING — the
    // backend additionally requires backtest_return_pct to be set for
    // that specific transition (see the strategy-object-aware check at
    // this function's "Paper Trade" button call site below); update_strategy()
    // clears backtest_return_pct back to None the moment rules actually
    // change, so a non-null value here is trustworthy, not stale.
    const transitions: Record<string, string[]> = {
      "DRAFT": ["BACKTESTING", "PAPER_TRADING", "STOPPED"],
      "BACKTESTING": ["PAPER_TRADING", "DRAFT", "STOPPED"],
      "PAPER_TRADING": ["LIVE", "DRAFT", "PAUSED", "STOPPED"],
      "LIVE": ["PAUSED", "STOPPED"],
      "PAUSED": ["PAPER_TRADING", "LIVE", "STOPPED"],
      "STOPPED": ["DRAFT"]
    };
    return transitions[currentStatus]?.includes(targetStatus) || false;
  };

  if (loading) {
    return (
      <div className="w-full flex items-center justify-center py-20" style={FONT}>
        <RefreshCw className="animate-spin" size={24} style={{ color: C.orange }} />
      </div>
    );
  }

  const statusCounts = strategies.reduce<Record<string, number>>((acc, s) => {
    acc[s.status] = (acc[s.status] || 0) + 1;
    return acc;
  }, {});

  return (
    <div className="w-full" style={FONT}>
      <div className="flex items-start justify-between mb-5 flex-wrap gap-3">
        <div>
          <h1 className="text-3xl font-light text-gray-800">Strategies</h1>
          <p className="text-xs text-gray-500 mt-1">Build any options strategy from Buy/Sell legs, backtest it on real historical data, paper trade it, then go live.</p>
        </div>
        <button onClick={() => setModalMode("create")}
          className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold text-white transition-all focus:outline-none hover:opacity-90 shadow-sm"
          style={{ backgroundColor: C.orange }}>
          <Plus size={16} /> Build Strategy
        </button>
      </div>

      {((portfolioGreeks && portfolioGreeks.net) || (portfolioMargin && (portfolioMargin.paper.open_legs > 0 || portfolioMargin.live.open_legs > 0))) && (
        <div className="flex items-stretch gap-4 mb-6 flex-wrap">
          {portfolioGreeks && portfolioGreeks.net && (
            <div className="flex-1 min-w-[320px] bg-white border rounded-xl overflow-hidden shadow-sm" style={{ borderColor: C.border2 }}>
              <div className="px-5 py-3.5 flex items-center gap-6 flex-wrap">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 flex items-center gap-2 shrink-0">
                  <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" /> Portfolio Greeks
                </h3>
                <div className="flex items-center gap-5 flex-wrap">
                  {([
                    ["Delta", portfolioGreeks.net.delta],
                    ["Gamma", portfolioGreeks.net.gamma],
                    ["Theta", portfolioGreeks.net.theta],
                    ["Vega", portfolioGreeks.net.vega],
                  ] as [string, number][]).map(([label, val]) => (
                    <div key={label} className="text-xs">
                      <span className="text-gray-400">Net {label}</span>{" "}
                      <span className="font-semibold" style={{ color: val > 0 ? C.green : val < 0 ? C.red : C.text }}>{val}</span>
                    </div>
                  ))}
                </div>
                <div className="text-[11px] text-gray-400 ml-auto shrink-0">
                  {portfolioGreeks.open_legs_count} open leg{portfolioGreeks.open_legs_count === 1 ? "" : "s"} across {portfolioGreeks.by_strategy.length} strateg{portfolioGreeks.by_strategy.length === 1 ? "y" : "ies"}
                </div>
              </div>
            </div>
          )}

          {portfolioMargin && (portfolioMargin.paper.open_legs > 0 || portfolioMargin.live.open_legs > 0) && (
            <div className="flex-1 min-w-[320px] bg-white border rounded-xl overflow-hidden shadow-sm" style={{ borderColor: C.border2 }}>
              <div className="px-5 py-3.5 flex items-center gap-6 flex-wrap">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 flex items-center gap-2 shrink-0">
                  <Wallet size={12} /> Portfolio Margin
                </h3>
                <div className="flex items-center gap-5 flex-wrap">
                  {([["Paper", portfolioMargin.paper], ["Live", portfolioMargin.live]] as [string, { margin_required: number | null; open_legs: number }][])
                    .filter(([, pool]) => pool.open_legs > 0)
                    .map(([label, pool]) => (
                      <div key={label} className="text-xs">
                        <span className="text-gray-400">{label}</span>{" "}
                        <span className="font-semibold" style={{ color: C.text }}>
                          {pool.margin_required != null ? `₹${inr(pool.margin_required, 0)}` : "—"}
                        </span>
                        <span className="text-gray-400"> ({pool.open_legs} leg{pool.open_legs === 1 ? "" : "s"})</span>
                      </div>
                    ))}
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {strategies.length > 0 && (
        <div className="flex items-center gap-2 mb-6 flex-wrap">
          {Object.entries(statusCounts).map(([status, count]) => (
            <div key={status} className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-[11px] font-medium" style={{ backgroundColor: (STATUS_META[status] || STATUS_META.DRAFT).bg, color: (STATUS_META[status] || STATUS_META.DRAFT).fg }}>
              <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: (STATUS_META[status] || STATUS_META.DRAFT).dot }} />
              {count} {(STATUS_META[status] || STATUS_META.DRAFT).label}
            </div>
          ))}
        </div>
      )}

      <div className="flex gap-6">
        <div className="w-80 shrink-0">
          <div className="bg-white border rounded-xl overflow-hidden shadow-sm" style={{ borderColor: C.border2 }}>
            <div className="px-4 py-3 border-b flex items-center gap-2" style={{ borderColor: C.border2 }}>
              <Layers size={14} style={{ color: C.muted }} />
              <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-500">My Strategies</h2>
            </div>
            <div className="divide-y overflow-y-auto" style={{ borderColor: C.border, maxHeight: 640 }}>
              {strategies.length === 0 ? (
                <div className="px-5 py-10 text-center">
                  <Layers size={28} className="mx-auto mb-3" style={{ color: C.border2 }} />
                  <div className="text-sm text-gray-500">No strategies yet</div>
                  <div className="text-xs text-gray-400 mt-1">Build your first one to get started!</div>
                </div>
              ) : (
                paginatedStrategies.map((strategy) => {
                  const meta = STATUS_META[strategy.status] || STATUS_META.DRAFT;
                  const selected = selectedStrategy?.id === strategy.id;
                  const perf = strategy.status === "LIVE" ? strategy.live_return_pct
                    : strategy.status === "PAPER_TRADING" ? strategy.paper_return_pct
                    : strategy.backtest_return_pct;
                  return (
                    <div key={strategy.id} role="button" tabIndex={0}
                      onClick={() => { setSelectedStrategy(strategy); setBacktestResult(null); setBacktestError(""); }}
                      onKeyDown={(e) => {
                        // Native interactive elements activate on BOTH Enter and Space —
                        // Space additionally needs preventDefault() or the browser scrolls
                        // the page instead of (or as well as) selecting the row.
                        if (e.key === "Enter" || e.key === " ") {
                          if (e.key === " ") e.preventDefault();
                          setSelectedStrategy(strategy); setBacktestResult(null); setBacktestError("");
                        }
                      }}
                      className="w-full pl-3 pr-4 py-3.5 text-left text-sm transition-colors flex items-start gap-3 cursor-pointer"
                      style={{ backgroundColor: selected ? "#fff7f2" : "transparent" }}
                      onMouseEnter={(e) => { if (!selected) e.currentTarget.style.backgroundColor = C.hover; }}
                      onMouseLeave={(e) => { if (!selected) e.currentTarget.style.backgroundColor = "transparent"; }}
                    >
                      <span className="w-1 self-stretch rounded-full shrink-0" style={{ backgroundColor: selected ? C.orange : meta.dot, opacity: selected ? 1 : 0.5 }} />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-start justify-between gap-2">
                          <div className="font-medium text-gray-800 truncate">{strategy.name}</div>
                          <div className="flex items-center gap-1 shrink-0">
                            {canTransitionTo(strategy.status, "STOPPED") && (
                              <button
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setConfirmModal({
                                    title: "Stop Strategy",
                                    message: `Stop "${strategy.name}" and square off any open position?`,
                                    confirmText: "Stop",
                                    confirmColor: C.red,
                                    onConfirm: () => handleStatusChange(strategy, "STOPPED")
                                  });
                                }}
                                disabled={statusUpdatingId === strategy.id}
                                className="flex items-center justify-center w-6 h-6 rounded-md transition-colors focus:outline-none hover:opacity-80 disabled:opacity-40 disabled:cursor-not-allowed"
                                style={{ backgroundColor: C.sellBg, color: C.red }} title="Stop strategy" aria-label={`Stop strategy "${strategy.name}"`}>
                                {statusUpdatingId === strategy.id ? <RefreshCw size={12} className="animate-spin" /> : <AlertCircle size={12} />}
                              </button>
                            )}
                            {/* Delete button temporarily hidden — kept commented out, not removed, so it's easy to restore.
                            {["DRAFT", "STOPPED"].includes(strategy.status) && (
                              <button
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setConfirmModal({
                                    title: "Delete Strategy",
                                    message: `Are you sure you want to delete "${strategy.name}"? This action cannot be undone.`,
                                    confirmText: "Delete",
                                    confirmColor: C.red,
                                    onConfirm: () => handleDeleteStrategy(strategy)
                                  });
                                }}
                                disabled={deletingId === strategy.id}
                                className="flex items-center justify-center w-6 h-6 rounded-md transition-colors focus:outline-none hover:opacity-80 disabled:opacity-40 disabled:cursor-not-allowed"
                                style={{ backgroundColor: C.sellBg, color: C.red }} title="Delete strategy">
                                {deletingId === strategy.id ? <RefreshCw size={12} className="animate-spin" /> : <Trash2 size={12} />}
                              </button>
                            )}
                            */}
                          </div>
                        </div>
                        <div className="flex items-center gap-2 mt-1.5 flex-wrap">
                          <StatusPill status={strategy.status} />
                          <CadenceBadge strategy={strategy} />
                          <span className="text-[11px] text-gray-400 truncate">{strategy.symbols.join(", ")}</span>
                        </div>
                        {perf != null && (
                          <div className="text-[11px] font-semibold mt-1" style={{ color: perf >= 0 ? C.green : C.red }}>
                            {perf >= 0 ? "+" : ""}{perf.toFixed(2)}%
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })
              )}
            </div>
            {strategies.length > STRATEGIES_PER_PAGE && (
              <div className="flex items-center justify-between px-4 py-2.5 border-t" style={{ borderColor: C.border2 }}>
                <button
                  onClick={() => setStrategiesPage((p) => Math.max(1, p - 1))}
                  disabled={strategiesPage === 1}
                  className="text-[11px] font-semibold px-2 py-1 rounded transition-colors focus:outline-none disabled:opacity-30 disabled:cursor-not-allowed hover:bg-gray-100"
                  style={{ color: C.text }}
                >
                  ‹ Prev
                </button>
                <span className="text-[11px] text-gray-400">Page {strategiesPage} of {strategiesTotalPages}</span>
                <button
                  onClick={() => setStrategiesPage((p) => Math.min(strategiesTotalPages, p + 1))}
                  disabled={strategiesPage === strategiesTotalPages}
                  className="text-[11px] font-semibold px-2 py-1 rounded transition-colors focus:outline-none disabled:opacity-30 disabled:cursor-not-allowed hover:bg-gray-100"
                  style={{ color: C.text }}
                >
                  Next ›
                </button>
              </div>
            )}
          </div>
        </div>

        {selectedStrategy ? (
          <div className="flex-1 space-y-5 min-w-0">
            <div className="bg-white border rounded-xl overflow-hidden shadow-sm" style={{ borderColor: C.border2 }}>
              <div className="px-6 py-5 border-b" style={{ borderColor: C.border2 }}>
                <div className="flex items-start justify-between gap-4 flex-wrap">
                  <div className="min-w-0">
                    <div className="flex items-center gap-3 flex-wrap">
                      <h2 className="text-xl font-semibold text-gray-800">{selectedStrategy.name}</h2>
                      <StatusPill status={selectedStrategy.status} big />
                      <CadenceBadge strategy={selectedStrategy} />
                      <BiasBadge strategy={selectedStrategy} />
                    </div>
                    <p className="text-sm text-gray-500 mt-1.5">{selectedStrategy.description || "No description"}</p>
                    <div className="flex items-center gap-1.5 mt-2.5">
                      <span className="text-[11px] font-medium px-2 py-0.5 rounded" style={{ backgroundColor: C.hover, color: C.text }}>{selectedStrategy.instrument_type}</span>
                      {selectedStrategy.symbols.map((s) => (
                        <span key={s} className="text-[11px] font-medium px-2 py-0.5 rounded" style={{ backgroundColor: "#fff7ed", color: C.orange }}>{s}</span>
                      ))}
                    </div>
                  </div>
                </div>

                <div className="flex items-center gap-2 flex-wrap mt-4 pt-4 border-t" style={{ borderColor: C.border }}>
                  {/* Backtest button — visible on DRAFT (first run), BACKTESTING (re-run),
                      PAPER_TRADING, and PAUSED (re-run without stopping the strategy).
                      Hidden entirely for non-leg-based strategies/COMMODITY — POST /{id}/backtest
                      always rejects those (see routes_custom_strategies.py), so showing a
                      button that can only ever error isn't useful. */}
                  {isLegBased(selectedStrategy) && selectedStrategy.instrument_type !== "COMMODITY" &&
                    (canTransitionTo(selectedStrategy.status, "BACKTESTING") ||
                    ["PAPER_TRADING", "PAUSED"].includes(selectedStrategy.status)) && (
                    <button onClick={() => setBacktestRangeTarget(selectedStrategy)} disabled={backtesting}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold transition-colors focus:outline-none hover:opacity-80"
                      style={{ backgroundColor: C.buyBg, color: C.blue }}>
                      <BarChart3 size={14} /> {backtesting ? "Backtesting..." : selectedStrategy.backtest_return_pct != null ? "Re-run Backtest" : "Backtest"}
                    </button>
                  )}
                  {backtesting && backtestProgress && (
                    <div className="flex items-center gap-2 px-3 py-2 rounded-lg text-xs font-semibold" style={{ backgroundColor: C.hover, color: C.text }}>
                      <div className="w-20 h-1.5 rounded-full overflow-hidden" style={{ backgroundColor: C.border2 }}>
                        <div className="h-full rounded-full transition-all" style={{
                          width: `${Math.min(100, Math.round((backtestProgress.current / backtestProgress.total) * 100))}%`,
                          backgroundColor: C.blue,
                        }} />
                      </div>
                      <span>{Math.min(100, Math.round((backtestProgress.current / backtestProgress.total) * 100))}%</span>
                    </div>
                  )}
                  {backtestResult && (
                    <button onClick={() => setShowBacktestModal(true)}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold transition-colors focus:outline-none hover:opacity-80"
                      style={{ backgroundColor: C.hover, color: C.text }}>
                      <TrendingUp size={14} /> View Backtest Results
                    </button>
                  )}
                  {canTransitionTo(selectedStrategy.status, "PAPER_TRADING") &&
                    // Backtest-first only applies where backtesting is actually
                    // possible — non-leg-based strategies/COMMODITY skip it, same
                    // exception routes_custom_strategies.py's PATCH /status enforces.
                    (selectedStrategy.status !== "DRAFT" || selectedStrategy.backtest_return_pct != null ||
                      !isLegBased(selectedStrategy) || selectedStrategy.instrument_type === "COMMODITY") && (
                    <button onClick={() => handleStatusChange(selectedStrategy, "PAPER_TRADING")}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold transition-colors focus:outline-none hover:opacity-80"
                      style={{ backgroundColor: "#e6f4ea", color: C.green }}>
                      <Play size={14} /> Paper Trade
                    </button>
                  )}
                  {canTransitionTo(selectedStrategy.status, "LIVE") && (
                    <button onClick={() => {
                      const symbolResults = Object.entries(payoff?.symbols || {}).filter(([, r]) => !r.error);
                      setConfirmModal({
                        title: "Go LIVE",
                        message: "Go LIVE — Upstox will place real orders with real money. Continue?",
                        detail: symbolResults.length > 0 ? (
                          <div className="mt-3 space-y-2">
                            {symbolResults.map(([symbol, r]) => (
                              <div key={symbol} className="rounded-lg border p-2.5 text-xs" style={{ borderColor: C.border2 }}>
                                <div className="font-semibold text-gray-700 mb-1">{symbol}</div>
                                <div className="grid grid-cols-3 gap-2">
                                  <div><span className="text-gray-400">Max Loss</span> <span className="block font-semibold" style={{ color: C.red }}>{r.max_loss != null ? `₹${inr(Math.abs(r.max_loss), 2)}` : "Unlimited"}</span></div>
                                  <div><span className="text-gray-400">Margin/Capital</span> <span className="block font-semibold text-gray-700">{r.capital_basis != null ? `₹${inr(r.capital_basis, 2)}` : "—"}</span></div>
                                  <div><span className="text-gray-400">POP</span> <span className="block font-semibold text-gray-700">{r.probability_of_profit_pct != null ? `${r.probability_of_profit_pct}%` : "N/A"}</span></div>
                                </div>
                              </div>
                            ))}
                            <p className="text-[10px] text-gray-400">Based on current live premiums if entered right now — actual live fills may differ.</p>
                          </div>
                        ) : (
                          <p className="mt-2 text-xs text-gray-400">Could not load a current risk estimate — check the "Expected Profit &amp; Loss" panel below before going live.</p>
                        ),
                        confirmText: "Go Live",
                        confirmColor: C.orange,
                        onConfirm: () => handleStatusChange(selectedStrategy, "LIVE")
                      });
                    }}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold text-white transition-colors focus:outline-none hover:opacity-90 shadow-sm"
                      style={{ backgroundColor: C.orange }}>
                      <Play size={14} /> Go Live
                    </button>
                  )}
                  {canTransitionTo(selectedStrategy.status, "PAUSED") && (
                    <button onClick={() => {
                      setConfirmModal({
                        title: "Pause Strategy",
                        message: "Pause this strategy? No new positions will be entered, but any position it already holds stays open and keeps being managed (TP/SL/trailing/expiry) until it closes naturally, or you Stop the strategy.",
                        confirmText: "Pause",
                        confirmColor: C.orange,
                        onConfirm: () => handleStatusChange(selectedStrategy, "PAUSED")
                      });
                    }}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold transition-colors focus:outline-none hover:opacity-80"
                      style={{ backgroundColor: "#fff7ed", color: C.orange }}>
                      <Pause size={14} /> Disable
                    </button>
                  )}
                  {selectedStrategy.status === "PAUSED" && (
                    <button onClick={() => handleStatusChange(selectedStrategy, "PAPER_TRADING")}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold transition-colors focus:outline-none hover:opacity-80"
                      style={{ backgroundColor: "#e6f4ea", color: C.green }}>
                      <Play size={14} /> Resume Paper
                    </button>
                  )}
                  {selectedStrategy.status === "PAUSED" && (
                    <button onClick={() => {
                      setConfirmModal({
                        title: "Resume LIVE",
                        message: "Resume LIVE trading — Upstox will place real orders with real money again. Continue?",
                        confirmText: "Resume Live",
                        confirmColor: C.orange,
                        onConfirm: () => handleStatusChange(selectedStrategy, "LIVE")
                      });
                    }}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold text-white transition-colors focus:outline-none hover:opacity-90 shadow-sm"
                      style={{ backgroundColor: C.orange }}>
                      <Play size={14} /> Resume Live
                    </button>
                  )}
                  {canTransitionTo(selectedStrategy.status, "DRAFT") && selectedStrategy.status !== "DRAFT" && (
                    <button onClick={() => handleStatusChange(selectedStrategy, "DRAFT")}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold transition-colors focus:outline-none hover:opacity-80"
                      style={{ backgroundColor: C.hover, color: C.text }}>
                      <RefreshCw size={14} /> Reactivate
                    </button>
                  )}
                  {["DRAFT", "BACKTESTING", "PAUSED", "STOPPED"].includes(selectedStrategy.status) && (
                    <button onClick={() => setModalMode("edit")}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold transition-colors focus:outline-none hover:opacity-80"
                      style={{ backgroundColor: C.hover, color: C.text }}>
                      <Pencil size={14} /> Edit
                    </button>
                  )}

                  <span className="flex-1" />

                  {canTransitionTo(selectedStrategy.status, "STOPPED") && (
                    <button onClick={() => {
                      setConfirmModal({
                        title: "Stop Strategy",
                        message: "Stop this strategy and square off any open position?",
                        confirmText: "Stop",
                        confirmColor: C.red,
                        onConfirm: () => handleStatusChange(selectedStrategy, "STOPPED")
                      });
                    }}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold transition-colors focus:outline-none hover:opacity-80"
                      style={{ backgroundColor: C.sellBg, color: C.red }}>
                      <AlertCircle size={14} /> Stop
                    </button>
                  )}
                  {/* Delete button temporarily hidden — kept commented out, not removed, so it's easy to restore.
                  {["DRAFT", "STOPPED"].includes(selectedStrategy.status) && (
                    <button onClick={() => {
                      setConfirmModal({
                        title: "Delete Strategy",
                        message: `Are you sure you want to delete "${selectedStrategy.name}"? This action cannot be undone.`,
                        confirmText: "Delete",
                        confirmColor: C.red,
                        onConfirm: () => handleDeleteStrategy(selectedStrategy)
                      });
                    }}
                      className="flex items-center justify-center w-8 h-8 rounded-lg transition-colors focus:outline-none hover:opacity-80"
                      style={{ backgroundColor: C.sellBg, color: C.red }} title="Delete strategy">
                      <Trash2 size={14} />
                    </button>
                  )}
                  */}
                </div>
              </div>

              <div className="p-6 space-y-6">
                <div className="grid grid-cols-3 gap-4">
                  <StatCard icon={BarChart3} label="Backtest (avg/cycle)" value={selectedStrategy.backtest_return_pct} accent={C.blue} />
                  <StatCard icon={Activity} label="Paper (last trade)" value={selectedStrategy.paper_return_pct} accent={C.green} />
                  <StatCard icon={Target} label="Live (last trade)" value={selectedStrategy.live_return_pct} accent={C.orange} />
                </div>

                <MarginRow margin={margin} loading={marginLoading} />

                {backtestRuns.length > 0 && (
                  <BacktestRunHistoryPanel
                    runs={backtestRuns}
                    compareRunIds={compareRunIds}
                    onToggleCompare={(runId) => {
                      setCompareRunIds((prev) => {
                        if (prev.includes(runId)) return prev.filter((id) => id !== runId);
                        return prev.length >= 2 ? [prev[1], runId] : [...prev, runId];
                      });
                    }}
                    onViewRun={async (runId) => {
                      try {
                        const run: BacktestRunDetail = await api.getCustomStrategyBacktestRun(selectedStrategy.id, runId);
                        if (run.result) { setBacktestResult(run.result); setShowBacktestModal(true); }
                      } catch {
                        // ignore failures
                      }
                    }}
                  />
                )}

                {compareRunIds.length === 2 && selectedStrategy && (
                  <BacktestCompareModal
                    strategyId={selectedStrategy.id}
                    runIds={compareRunIds}
                    runs={backtestRuns}
                    onClose={() => setCompareRunIds([])}
                  />
                )}

                {selectedStrategy.rules && isLegBased(selectedStrategy) && (
                  <div>
                    <div className="flex items-center gap-2 mb-3">
                      <FileText size={14} style={{ color: C.muted }} />
                      <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500">Legs</h3>
                    </div>
                    <div className="overflow-x-auto border rounded-xl" style={{ borderColor: C.border2 }}>
                      <table className="w-full text-sm">
                        <thead>
                          <tr className="text-[11px] uppercase tracking-wide text-gray-400 border-b" style={{ borderColor: C.border2, background: C.tableHeaderBg }}>
                            <th className="px-4 py-2.5 text-left font-medium">Action</th>
                            <th className="px-4 py-2.5 text-left font-medium">Type</th>
                            <th className="px-4 py-2.5 text-left font-medium">Strike</th>
                            <th className="px-4 py-2.5 text-right font-medium">Lots</th>
                          </tr>
                        </thead>
                        <tbody>
                          {selectedStrategy.rules.legs.map((leg, i) => (
                            <tr key={i} className="border-b last:border-0" style={{ borderColor: C.border }}>
                              <td className="px-4 py-2.5">
                                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-bold"
                                  style={leg.action === "BUY" ? { background: C.buyBg, color: C.buyText } : { background: C.sellBg, color: C.sellText }}>
                                  {leg.action}
                                </span>
                              </td>
                              <td className="px-4 py-2.5">
                                <span className="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-bold" style={{ background: C.hover, color: C.text }}>
                                  {(leg.instrument_type ?? "OPTION") === "EQUITY" ? "EQ" : leg.option_type}
                                </span>
                              </td>
                              <td className="px-4 py-2.5 text-gray-600">
                                {(leg.instrument_type ?? "OPTION") === "EQUITY" ? "Equity (no strike)" : strikeLabel(leg.strike_selection!)}
                              </td>
                              <td className="px-4 py-2.5 text-right font-medium text-gray-700">
                                {leg.lots}{(leg.instrument_type ?? "OPTION") === "EQUITY" ? " sh" : ""}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <div className="grid grid-cols-3 gap-4 mt-4">
                      <div className="rounded-xl border p-3.5" style={{ borderColor: C.border2 }}>
                        <div className="flex items-center gap-1.5 text-[11px] font-medium text-gray-400 mb-1"><Calendar size={12} /> EXPIRY</div>
                        <div className="text-sm text-gray-800 font-medium">
                          {(selectedStrategy.rules.expiry?.mode || "WEEKLY") === "MONTHLY" ? "Nearest Monthly" : "Nearest Weekly"}
                          {expiryDatePreview && <span className="text-gray-400 font-normal"> · {fmtDate(expiryDatePreview)}</span>}
                        </div>
                      </div>
                      <div className="rounded-xl border p-3.5" style={{ borderColor: C.border2 }}>
                        <div className="flex items-center gap-1.5 text-[11px] font-medium text-gray-400 mb-1"><Clock size={12} /> ENTRY</div>
                        <div className="text-sm text-gray-800 font-medium">{selectedStrategy.rules.entry.mode === "AT_TIME" ? `At ${formatTime12h(selectedStrategy.rules.entry.time)}` : "Immediately when live"}</div>
                      </div>
                      <div className="rounded-xl border p-3.5" style={{ borderColor: C.border2 }}>
                        <div className="flex items-center gap-1.5 text-[11px] font-medium text-gray-400 mb-1"><LogOut size={12} /> EXIT</div>
                        <div className="text-sm text-gray-800 font-medium">
                          {[
                            selectedStrategy.rules.exit.take_profit_pct ? `+${selectedStrategy.rules.exit.take_profit_pct}% profit` : null,
                            selectedStrategy.rules.exit.stop_loss_pct ? `-${selectedStrategy.rules.exit.stop_loss_pct}% loss` : null,
                            selectedStrategy.rules.exit.exit_time ? `${formatTime12h(selectedStrategy.rules.exit.exit_time)} time exit` : null,
                            selectedStrategy.rules.exit.exit_days_before_expiry ? `${selectedStrategy.rules.exit.exit_days_before_expiry}d before expiry` : null,
                          ].filter(Boolean).join(" or ") || "Expiry only"}
                        </div>
                      </div>
                    </div>

                    <div className="mt-4">
                      <div className="flex items-center gap-2 mb-3">
                        <IndianRupee size={14} style={{ color: C.muted }} />
                        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500">Expected Profit &amp; Loss</h3>
                        {payoffLoading && <RefreshCw size={12} className="animate-spin" style={{ color: C.muted }} />}
                        {!payoffLoading && (
                          <button onClick={() => selectedStrategy && fetchPayoff(selectedStrategy)} className="text-[11px] font-medium hover:underline focus:outline-none" style={{ color: C.blue }}>
                            Refresh
                          </button>
                        )}
                      </div>
                      {Object.entries(payoff?.symbols || {}).length === 0 ? (
                        <div className="rounded-xl border p-4 text-xs text-gray-400" style={{ borderColor: C.border2 }}>
                          {payoffLoading ? "Fetching current option premiums..." : "Could not compute a payoff estimate right now — try Refresh."}
                        </div>
                      ) : (
                        <div className="space-y-2">
                          {Object.entries(payoff!.symbols).map(([symbol, r]) => (
                            <div key={symbol} className="rounded-xl border p-4" style={{ borderColor: C.border2 }}>
                              {r.error ? (
                                <div className="text-xs text-gray-400">{symbol}: {r.error}</div>
                              ) : (
                                <>
                                {r.payoff_curve && r.payoff_curve.length >= 2 && r.spot_price != null && (
                                  <div className="mb-4">
                                    <PayoffDiagramChart
                                      symbol={symbol}
                                      curve={r.payoff_curve}
                                      spotPrice={r.spot_price}
                                      breakevens={r.breakevens_detail?.map((b) => b.price) ?? r.breakevens}
                                      legs={r.legs}
                                      expiry={r.expiry}
                                      daysRemaining={daysRemaining !== null ? daysRemaining : undefined}
                                      ivShift={ivShift}
                                    />
                                    {/* Sliders for dynamic options evaluation */}
                                    <div className="grid grid-cols-2 gap-6 mt-4 p-3 bg-slate-50 border rounded-xl" style={{ borderColor: C.border2 }}>
                                      <div>
                                        <div className="flex items-center justify-between text-[11px] font-semibold text-gray-600 mb-1">
                                          <span>Target Day: {daysRemaining !== null ? `${daysRemaining} days left` : "Expiry"}</span>
                                          {r.expiry && (
                                            <span className="text-[10px] font-normal text-gray-400">
                                              (Expiry: {r.expiry})
                                            </span>
                                          )}
                                        </div>
                                        <input
                                          type="range"
                                          min="0"
                                          max={(() => {
                                            const maxVal = r.expiry ? Math.max((new Date(r.expiry).getTime() - new Date().getTime()) / (1000 * 3600 * 24), 1) : 30;
                                            return Math.round(maxVal);
                                          })()}
                                          value={daysRemaining ?? 0}
                                          onChange={(e) => setDaysRemaining(parseInt(e.target.value))}
                                          className="w-full h-1 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-blue-500"
                                        />
                                      </div>
                                      <div>
                                        <div className="flex items-center justify-between text-[11px] font-semibold text-gray-600 mb-1">
                                          <span>IV Shift: {ivShift >= 0 ? "+" : ""}{(ivShift * 100).toFixed(0)}%</span>
                                          <button onClick={() => setIvShift(0)} className="text-[10px] font-normal text-blue-500 hover:underline focus:outline-none">Reset</button>
                                        </div>
                                        <input
                                          type="range"
                                          min="-0.5"
                                          max="0.5"
                                          step="0.05"
                                          value={ivShift}
                                          onChange={(e) => setIvShift(parseFloat(e.target.value))}
                                          className="w-full h-1 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-blue-500"
                                        />
                                      </div>
                                    </div>
                                  </div>
                                )}
                                <div className="grid grid-cols-5 gap-4">
                                  <div>
                                    <div className="text-[11px] text-gray-400 mb-1">{symbol} · Max Profit</div>
                                    <div className="text-base font-semibold" style={{ color: C.green }}>
                                      {r.max_profit != null ? `₹${inr(r.max_profit, 2)}` : "Unlimited"}
                                      {r.max_profit != null && r.max_profit_pct != null && (
                                        <span className="text-xs font-normal ml-1">({r.max_profit_pct >= 0 ? "+" : ""}{r.max_profit_pct}%)</span>
                                      )}
                                    </div>
                                  </div>
                                  <div>
                                    <div className="text-[11px] text-gray-400 mb-1">Max Loss</div>
                                    <div className="text-base font-semibold" style={{ color: C.red }}>
                                      {r.max_loss != null ? `₹${inr(Math.abs(r.max_loss), 2)}` : "Unlimited"}
                                    </div>
                                  </div>
                                  <div>
                                    <div className="text-[11px] text-gray-400 mb-1">Risk Reward Ratio</div>
                                    <div className="text-base font-semibold text-gray-800">
                                      {r.risk_reward_ratio != null ? `1 : ${r.risk_reward_ratio}` : "N/A"}
                                    </div>
                                  </div>
                                  <div>
                                    <div className="text-[11px] text-gray-400 mb-1 flex items-center gap-1">
                                      POP <span title="Probability of Profit — the market-implied (not a forecast) chance this settles profitably at expiry, based on current IV."><Info size={10} style={{ color: C.faint }} /></span>
                                    </div>
                                    <div className="text-base font-semibold text-gray-800">
                                      {r.probability_of_profit_pct != null ? `${r.probability_of_profit_pct}%` : "N/A"}
                                    </div>
                                  </div>
                                  <div>
                                    <div className="text-[11px] text-gray-400 mb-1">Breakeven{(r.breakevens_detail?.length ?? 0) !== 1 ? "s" : ""} at</div>
                                    {r.breakevens_detail && r.breakevens_detail.length > 0 ? (
                                      <div className="space-y-0.5">
                                        {r.breakevens_detail.map((b, i) => (
                                          <div key={i} className="text-sm font-medium text-gray-700">
                                            {inr(b.price, 2)} <span className="text-xs font-normal text-gray-400">({b.pct_from_spot >= 0 ? "+" : ""}{b.pct_from_spot}%)</span>
                                          </div>
                                        ))}
                                      </div>
                                    ) : (
                                      <div className="text-sm font-medium text-gray-700">—</div>
                                    )}
                                  </div>
                                </div>
                                </>
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                      <p className="text-[11px] text-gray-400 mt-2">Based on current live option premiums if entered right now — not a forecast, and moves as the market does.</p>
                    </div>
                  </div>
                )}

                {(selectedStrategy.strategy_type === "SMART_CONDOR" || selectedStrategy.strategy_type === "DELTA_NEUTRAL_STRANGLE") && (
                  <AdjustmentPreviewCard strategyId={selectedStrategy.id} />
                )}

                <div className="flex items-center gap-2 text-xs pt-1">
                  <span className="px-2.5 py-1 rounded-lg" style={{ background: C.hover, color: C.muted }}>Created {fmtDate(selectedStrategy.created_at)}</span>
                  {selectedStrategy.deployed_at && (
                    <span className="px-2.5 py-1 rounded-lg" style={{ background: "#fff7ed", color: C.orange }}>Deployed {fmtDate(selectedStrategy.deployed_at)}</span>
                  )}
                </div>
              </div>
            </div>

            {liveGreeks && (
              <div className="bg-white border rounded-xl overflow-hidden shadow-sm" style={{ borderColor: C.border2 }}>
                <div className="px-6 py-4 border-b flex items-center gap-5 flex-wrap" style={{ borderColor: C.border2 }}>
                  <h3 className="text-sm font-semibold text-gray-700 flex items-center gap-2">
                    <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" /> Live Greeks
                  </h3>
                  {liveGreeks.net && (
                    <div className="flex items-center gap-4 flex-wrap">
                      {([
                        ["Delta", liveGreeks.net.delta],
                        ["Gamma", liveGreeks.net.gamma],
                        ["Theta", liveGreeks.net.theta],
                        ["Vega", liveGreeks.net.vega],
                      ] as [string, number][]).map(([label, val]) => (
                        <div key={label} className="text-xs">
                          <span className="text-gray-400">Net {label}</span>{" "}
                          <span className="font-semibold" style={{ color: val > 0 ? C.green : val < 0 ? C.red : C.text }}>{val}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
                {liveGreeks.legs.length === 0 ? (
                  <div className="px-6 py-8 text-center text-xs text-gray-400">{liveGreeks.message || "No open legs to price."}</div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="text-[11px] uppercase tracking-wide text-gray-400 border-b" style={{ borderColor: C.border2, background: C.tableHeaderBg }}>
                          <th className="px-4 py-2.5 text-left font-medium">Leg</th>
                          <th className="px-4 py-2.5 text-right font-medium">LTP</th>
                          <th className="px-4 py-2.5 text-right font-medium">IV</th>
                          <th className="px-4 py-2.5 text-right font-medium">Delta</th>
                          <th className="px-4 py-2.5 text-right font-medium">Gamma</th>
                          <th className="px-4 py-2.5 text-right font-medium">Theta</th>
                          <th className="px-4 py-2.5 text-right font-medium">Vega</th>
                        </tr>
                      </thead>
                      <tbody>
                        {liveGreeks.legs.map((leg) => (
                          <tr key={leg.leg_index} className="border-b last:border-0 text-xs" style={{ borderColor: C.border }}>
                            <td className="px-4 py-2.5">
                              <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold mr-1.5"
                                style={leg.transaction_type === "BUY" ? { background: C.buyBg, color: C.buyText } : { background: C.sellBg, color: C.sellText }}>
                                {leg.transaction_type}
                              </span>
                              {leg.symbol} {leg.strike} {leg.option_type}
                            </td>
                            <td className="px-4 py-2.5 text-right font-medium text-gray-700">{leg.current_price != null ? leg.current_price.toFixed(2) : "—"}</td>
                            <td className="px-4 py-2.5 text-right">{leg.greeks ? `${leg.greeks.iv.toFixed(1)}%` : "—"}</td>
                            <td className="px-4 py-2.5 text-right">{leg.greeks ? leg.greeks.delta.toFixed(2) : "—"}</td>
                            <td className="px-4 py-2.5 text-right">{leg.greeks ? leg.greeks.gamma.toFixed(4) : "—"}</td>
                            <td className="px-4 py-2.5 text-right">{leg.greeks ? leg.greeks.theta.toFixed(2) : "—"}</td>
                            <td className="px-4 py-2.5 text-right">{leg.greeks ? leg.greeks.vega.toFixed(2) : "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}

            {(liveOpenLegs.length > 0 || closedLegs.length > 0) && (
              <div className="bg-white border rounded-xl overflow-hidden shadow-sm" style={{ borderColor: C.border2 }}>
                <div className="px-6 py-4 border-b flex items-center gap-2" style={{ borderColor: C.border2 }}>
                  <Layers size={14} style={{ color: C.muted }} />
                  <h3 className="text-sm font-semibold text-gray-700">Positions</h3>
                  {positionsLoading && <RefreshCw size={12} className="animate-spin" style={{ color: C.muted }} />}
                  {liveOpenLegs.length > 0 && (
                    <span className="ml-1 px-2 py-0.5 rounded-full text-[10px] font-semibold flex items-center gap-1" style={{ background: "#e6f4ea", color: C.green }}>
                      <span className="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse" /> {liveOpenLegs.length} open
                    </span>
                  )}
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-[11px] uppercase tracking-wide text-gray-400 border-b" style={{ borderColor: C.border2, background: C.tableHeaderBg }}>
                        <th className="px-4 py-2.5 text-left font-medium">Leg</th>
                        <th className="px-4 py-2.5 text-left font-medium">Mode</th>
                        <th className="px-4 py-2.5 text-right font-medium">Entry</th>
                        <th className="px-4 py-2.5 text-right font-medium">LTP / Exit</th>
                        <th className="px-4 py-2.5 text-right font-medium">P&amp;L</th>
                        <th className="px-4 py-2.5 text-left font-medium">Status</th>
                        <th className="px-4 py-2.5 text-left font-medium">Reason</th>
                        <th className="px-4 py-2.5 text-left font-medium">Opened</th>
                      </tr>
                    </thead>
                    <tbody>
                      {liveOpenLegs.map((leg) => (
                        <tr key={`open-${leg.id}`} className="border-b last:border-0 text-xs" style={{ borderColor: C.border }}>
                          <td className="px-4 py-2.5">
                            <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold mr-1.5"
                              style={leg.transaction_type === "BUY" ? { background: C.buyBg, color: C.buyText } : { background: C.sellBg, color: C.sellText }}>
                              {leg.transaction_type}
                            </span>
                            {leg.strike != null ? `${leg.strike} ${leg.option_type}` : leg.instrument_type} · qty {leg.quantity}
                          </td>
                          <td className="px-4 py-2.5 capitalize text-gray-500">{leg.mode}</td>
                          <td className="px-4 py-2.5 text-right font-medium text-gray-700">₹{leg.entry_price.toFixed(2)}</td>
                          <td className="px-4 py-2.5 text-right text-gray-700">{leg.ltp != null ? `₹${leg.ltp.toFixed(2)}` : "—"}</td>
                          <td className="px-4 py-2.5 text-right font-semibold" style={{ color: leg.pnl == null ? C.muted : leg.pnl >= 0 ? C.green : C.red }}>
                            {leg.pnl == null ? "—" : `${leg.pnl < 0 ? "-" : ""}₹${Math.abs(leg.pnl).toFixed(2)}`}
                          </td>
                          <td className="px-4 py-2.5">
                            <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold" style={{ background: "#e6f4ea", color: C.green }}>OPEN</span>
                          </td>
                          <td className="px-4 py-2.5 text-gray-400">—</td>
                          <td className="px-4 py-2.5 text-gray-400">{leg.opened_at ? fmtDateTime(leg.opened_at) : "—"}</td>
                        </tr>
                      ))}
                      {closedLegs.map((leg) => {
                        const sign = leg.transaction_type === "SELL" ? 1 : -1;
                        const pnl = leg.exit_price != null ? (leg.entry_price - leg.exit_price) * leg.quantity * sign : null;
                        return (
                          <tr key={`closed-${leg.id}`} className="border-b last:border-0 text-xs" style={{ borderColor: C.border }}>
                            <td className="px-4 py-2.5">
                              <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold mr-1.5"
                                style={leg.transaction_type === "BUY" ? { background: C.buyBg, color: C.buyText } : { background: C.sellBg, color: C.sellText }}>
                                {leg.transaction_type}
                              </span>
                              {leg.strike != null ? `${leg.strike} ${leg.option_type}` : leg.instrument_type} · qty {leg.quantity}
                            </td>
                            <td className="px-4 py-2.5 capitalize text-gray-500">{leg.mode}</td>
                            <td className="px-4 py-2.5 text-right font-medium text-gray-700">₹{leg.entry_price.toFixed(2)}</td>
                            <td className="px-4 py-2.5 text-right text-gray-700">{leg.exit_price != null ? `₹${leg.exit_price.toFixed(2)}` : "—"}</td>
                            <td className="px-4 py-2.5 text-right font-semibold" style={{ color: pnl == null ? C.muted : pnl >= 0 ? C.green : C.red }}>
                              {pnl == null ? "—" : `${pnl < 0 ? "-" : ""}₹${Math.abs(pnl).toFixed(2)}`}
                            </td>
                            <td className="px-4 py-2.5">
                              <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold" style={{ background: C.hover, color: C.muted }}>CLOSED</span>
                            </td>
                            <td className="px-4 py-2.5 text-gray-400">{leg.exit_reason || "—"}</td>
                            <td className="px-4 py-2.5 text-gray-400">{fmtDateTime(leg.opened_at)}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                    <tfoot>
                      {(() => {
                        // Two SEPARATE totals, not one blended figure — an open
                        // leg's P&L is unrealized mark-to-market (gross, no exit
                        // charges yet incurred) while a closed leg's is realized
                        // and net of real charges (via compute_basket_pnl on the
                        // backend, same formula the Leaderboard uses). Summing
                        // them into one "Total" made this page's number silently
                        // disagree with the Leaderboard's for the same strategy.
                        const unrealizedPnl = liveOpenLegs.reduce((sum, leg) => sum + (leg.pnl ?? 0), 0);
                        const rows: { label: string; value: number | null }[] = [
                          { label: `Unrealized P&L (${liveOpenLegs.length} open, gross MTM)`, value: liveOpenLegs.length > 0 ? unrealizedPnl : null },
                          { label: `Realized P&L (${closedLegs.length} closed, net of charges)`, value: closedLegs.length > 0 ? realizedNetPnl : null },
                        ];
                        return rows.map((row) => (
                          <tr key={row.label} className="border-t text-xs" style={{ borderColor: C.border2, background: C.tableHeaderBg }}>
                            <td className="px-4 py-2.5 font-semibold text-gray-600" colSpan={4}>{row.label}</td>
                            <td className="px-4 py-2.5 text-right font-bold" style={{ color: row.value == null ? C.muted : row.value >= 0 ? C.green : C.red }}>
                              {row.value == null ? "—" : `${row.value < 0 ? "-" : ""}₹${Math.abs(row.value).toFixed(2)}`}
                            </td>
                            <td className="px-4 py-2.5" colSpan={3}></td>
                          </tr>
                        ));
                      })()}
                    </tfoot>
                  </table>
                </div>
              </div>
            )}

            {backtestError && (
              <div className="px-4 py-3 rounded-xl border text-sm" style={{ backgroundColor: C.sellBg, borderColor: C.red, color: C.red }}>{backtestError}</div>
            )}
          </div>
        ) : (
          <div className="flex-1 flex items-center justify-center bg-white border rounded-xl shadow-sm" style={{ borderColor: C.border2, minHeight: 320 }}>
            <div className="text-center">
              <Layers size={32} className="mx-auto mb-3" style={{ color: C.border2 }} />
              <div className="text-sm text-gray-500">Select a strategy to view details</div>
              <div className="text-xs text-gray-400 mt-1">or build a new one to get started</div>
            </div>
          </div>
        )}
      </div>

      {modalMode && (
        <StrategyBuilderModal
          onClose={() => setModalMode(null)}
          onSuccess={() => setModalMode(null)}
          onCreate={onCreate}
          onUpdate={onUpdate}
          editStrategy={modalMode === "edit" && selectedStrategy ? {
            id: selectedStrategy.id,
            name: selectedStrategy.name,
            instrument_type: selectedStrategy.instrument_type,
            symbols: selectedStrategy.symbols,
            rules: selectedStrategy.rules,
            strategy_type: selectedStrategy.strategy_type,
          } : null}
        />
      )}

      {backtestRangeTarget && (
        <BacktestRangeModal
          strategy={backtestRangeTarget}
          onClose={() => setBacktestRangeTarget(null)}
          onRun={(fromDate, toDate, slippagePct) => {
            runBacktest(backtestRangeTarget, fromDate, toDate, slippagePct);
            setBacktestRangeTarget(null);
          }}
        />
      )}

      {showBacktestModal && backtestResult && selectedStrategy && (
        <BacktestResultsModal
          strategyName={selectedStrategy.name}
          result={backtestResult}
          onClose={() => setShowBacktestModal(false)}
        />
      )}

      {confirmModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-40 backdrop-blur-sm">
          <div className={`bg-white rounded-2xl p-6 w-full mx-4 border shadow-2xl max-h-[85vh] overflow-y-auto ${confirmModal.detail ? "max-w-md" : "max-w-sm"}`} style={{ borderColor: C.border }}>
            <h3 className="text-base font-bold text-gray-800">{confirmModal.title}</h3>
            <p className="text-xs text-gray-500 mt-2 leading-relaxed">{confirmModal.message}</p>
            {confirmModal.detail}
            <div className="flex justify-end gap-2 mt-6">
              <button
                onClick={() => setConfirmModal(null)}
                className="px-3.5 py-1.5 text-xs font-semibold rounded-lg bg-gray-100 text-gray-600 hover:bg-gray-200 transition-colors focus:outline-none"
              >
                {confirmModal.cancelText || "Cancel"}
              </button>
              <button
                onClick={() => {
                  confirmModal.onConfirm();
                  setConfirmModal(null);
                }}
                className="px-3.5 py-1.5 text-xs font-semibold rounded-lg text-white transition-colors focus:outline-none hover:opacity-90"
                style={{ backgroundColor: confirmModal.confirmColor || C.orange }}
              >
                {confirmModal.confirmText || "Confirm"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
