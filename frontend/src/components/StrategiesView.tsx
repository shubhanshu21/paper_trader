import { useState, useEffect, useRef } from "react";
import {
  Plus, Play, Pause, RefreshCw, TrendingUp, AlertCircle, BarChart3, Trash2, Pencil,
  Layers, Clock, LogOut, Activity, FileText, Target, ArrowUpRight, ArrowDownRight, Calendar, IndianRupee, Info, X,
  type LucideIcon,
} from "lucide-react";
import { C, FONT, useToast, DatePicker, fmtDate, formatTime12h, inr } from "./Common";
import { wsUrl } from "../api";
import StrategyBuilderModal from "./StrategyBuilderModal";

interface StrategyLeg {
  action: string;
  option_type: string;
  strike_selection: { mode: string; value: number | null };
  lots: number;
}

interface StrategyRules {
  legs: StrategyLeg[];
  entry: { mode: string; time: string | null };
  expiry?: { mode: string };
  exit: { take_profit_pct: number | null; stop_loss_pct: number | null; exit_time: string | null; exit_days_before_expiry: number };
}

interface CustomStrategy {
  id: number;
  name: string;
  description: string;
  instrument_type: string;
  symbols: string[];
  rules: StrategyRules | null;
  status: string;
  backtest_return_pct: number | null;
  paper_return_pct: number | null;
  live_return_pct: number | null;
  created_at: string;
  deployed_at: string | null;
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

interface BacktestCycle {
  entry_date: string;
  expiry: string;
  exit_date: string;
  exit_reason: string;
  net_pnl: number;
  pnl_pct_of_premium: number;
  won: boolean;
  liquid: boolean;
}

interface BacktestResult {
  cycles_tested: number;
  avg_return_pct_of_premium: number;
  win_rate_pct: number;
  cycles: BacktestCycle[];
  from_date?: string | null;
  to_date?: string | null;
  run_at?: string;
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

interface PayoffSymbolResult {
  max_profit: number | null;
  max_profit_pct: number | null;
  max_loss: number | null;
  breakevens: number[];
  breakevens_detail?: { price: number; pct_from_spot: number }[];
  risk_reward_ratio: number | null;
  probability_of_profit_pct: number | null;
  net_premium: number;
  spot_price?: number;
  expiry?: string;
  error?: string;
}

interface PayoffResponse {
  strategy_id: number;
  symbols: Record<string, PayoffSymbolResult>;
}

const strikeLabel = (sel: { mode: string; value: number | null }) => {
  if (sel.mode === "ATM") return "ATM";
  if (sel.mode === "OTM_PERCENT") return `${sel.value}% OTM`;
  if (sel.mode === "OTM_POINTS") return `${sel.value} pts OTM`;
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
}: { strategy: CustomStrategy; onClose: () => void; onRun: (fromDate: string, toDate: string) => void }) {
  const [fromDate, setFromDate] = useState(BACKTEST_PRESETS[1].from || "");
  const [toDate, setToDate] = useState("");
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

        <div className="flex justify-end gap-2 mt-6">
          <button
            onClick={onClose}
            className="px-3.5 py-1.5 text-xs font-semibold rounded-lg bg-gray-100 text-gray-600 hover:bg-gray-200 transition-colors focus:outline-none"
          >
            Cancel
          </button>
          <button
            onClick={() => onRun(fromDate, toDate)}
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

function BacktestResultsModal({
  strategyName, result, onClose,
}: { strategyName: string; result: BacktestResult; onClose: () => void }) {
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
              {result.run_at && <span className="text-[11px] text-gray-400">Run {new Date(result.run_at).toLocaleString()}</span>}
            </div>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 focus:outline-none shrink-0"><X size={20} /></button>
        </div>
        <div className="overflow-y-auto flex-1">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[11px] uppercase tracking-wide text-gray-400 border-b sticky top-0" style={{ borderColor: C.border2, background: C.tableHeaderBg }}>
                <th className="px-4 py-2.5 text-left font-medium">Entry</th>
                <th className="px-4 py-2.5 text-left font-medium">Exit</th>
                <th className="px-4 py-2.5 text-left font-medium">Reason</th>
                <th className="px-4 py-2.5 text-right font-medium">Net P&amp;L</th>
                <th className="px-4 py-2.5 text-right font-medium">% of Premium</th>
                <th className="px-4 py-2.5 text-center font-medium">Liquid</th>
              </tr>
            </thead>
            <tbody>
              {result.cycles.map((c, i) => (
                <tr key={i} className="border-b last:border-0 text-xs" style={{ borderColor: C.border }}>
                  <td className="px-4 py-2.5">{c.entry_date}</td>
                  <td className="px-4 py-2.5">{c.exit_date}</td>
                  <td className="px-4 py-2.5">
                    <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold" style={{ background: C.hover, color: C.muted }}>{c.exit_reason}</span>
                  </td>
                  <td className="px-4 py-2.5 text-right font-semibold" style={{ color: c.net_pnl >= 0 ? C.green : C.red }}>₹{c.net_pnl.toFixed(2)}</td>
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

export default function StrategiesView() {
  const toast = useToast();
  const [strategies, setStrategies] = useState<CustomStrategy[]>([]);
  const [selectedStrategy, setSelectedStrategy] = useState<CustomStrategy | null>(null);
  const [loading, setLoading] = useState(false);
  const [modalMode, setModalMode] = useState<null | "create" | "edit">(null);
  const [backtesting, setBacktesting] = useState(false);
  const [backtestResult, setBacktestResult] = useState<BacktestResult | null>(null);
  const [backtestError, setBacktestError] = useState("");
  const [showBacktestModal, setShowBacktestModal] = useState(false);
  const [strategiesPage, setStrategiesPage] = useState(1);
  const STRATEGIES_PER_PAGE = 5;
  const [liveGreeks, setLiveGreeks] = useState<LiveGreeksResponse | null>(null);
  const [expiryDatePreview, setExpiryDatePreview] = useState<string | null>(null);

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
        const response = await fetch(`/api/custom-strategies/templates/expiries?symbol=${encodeURIComponent(symbol)}`, { credentials: "include" });
        if (!response.ok || cancelled) return;
        const data = await response.json();
        const expiries: { date: string; label: string }[] = data.expiries || [];
        const mode = selectedStrategy?.rules?.expiry?.mode || "WEEKLY";
        const match = mode === "MONTHLY" ? expiries.find((e) => e.label === "Monthly") : expiries[0];
        if (match && !cancelled) setExpiryDatePreview(match.date);
      } catch {
        // Preview-only — silently leave it unset, the mode label alone is still shown.
      }
    })();
    return () => { cancelled = true; };
  }, [selectedStrategy?.id, selectedStrategy?.rules?.expiry?.mode]);

  const [payoff, setPayoff] = useState<PayoffResponse | null>(null);
  const [payoffLoading, setPayoffLoading] = useState(false);

  const fetchPayoff = async (strategy: CustomStrategy) => {
    setPayoffLoading(true);
    try {
      const response = await fetch(`/api/custom-strategies/${strategy.id}/payoff`, { credentials: "include" });
      if (response.ok) setPayoff(await response.json());
      else setPayoff(null);
    } catch {
      setPayoff(null);
    } finally {
      setPayoffLoading(false);
    }
  };

  useEffect(() => {
    setPayoff(null);
    if (selectedStrategy && selectedStrategy.rules) fetchPayoff(selectedStrategy);
  }, [selectedStrategy?.id]);

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
        const response = await fetch(`/api/custom-strategies/${selectedStrategy.id}/backtest`, { credentials: "include" });
        if (response.ok && !cancelled) setBacktestResult(await response.json());
      } catch {
        // No stored result yet — normal for a never-backtested strategy.
      }
    })();
    return () => { cancelled = true; };
  }, [selectedStrategy?.id]);

  const [confirmModal, setConfirmModal] = useState<{
    title: string;
    message: string;
    confirmText?: string;
    cancelText?: string;
    confirmColor?: string;
    onConfirm: () => void;
  } | null>(null);
  const [backtestRangeTarget, setBacktestRangeTarget] = useState<CustomStrategy | null>(null);

  useEffect(() => {
    loadStrategies();
  }, []);

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
        const data = JSON.parse(event.data);
        if (data.type === "greeks") setLiveGreeks(data);
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
  }, [selectedStrategy?.id, selectedStrategy?.status]);

  const loadStrategies = async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/custom-strategies", { credentials: "include" });
      const data = await response.json();
      setStrategies(data.strategies || []);
      if (data.strategies && data.strategies.length > 0) {
        setSelectedStrategy((prev) => data.strategies.find((s: CustomStrategy) => s.id === prev?.id) || data.strategies[0]);
      }
    } catch (error) {
      console.error("Failed to load strategies:", error);
    } finally {
      setLoading(false);
    }
  };

  const handleStatusChange = async (strategy: CustomStrategy, newStatus: string) => {
    try {
      const response = await fetch(`/api/custom-strategies/${strategy.id}/status`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: newStatus }),
      });
      const updated = await response.json();
      if (response.ok) {
        setStrategies(strategies.map(s => s.id === strategy.id ? updated : s));
        if (selectedStrategy?.id === strategy.id) setSelectedStrategy(updated);
        toast.success(`Strategy "${strategy.name}" is now ${newStatus.replace("_", " ").toLowerCase()}`);
      } else {
        toast.error(updated.detail || "Failed to update status.");
      }
    } catch (error) {
      console.error("Failed to update status:", error);
      toast.error("Failed to update status.");
    }
  };

  const runBacktest = async (strategy: CustomStrategy, fromDate?: string, toDate?: string) => {
    setBacktesting(true);
    setBacktestResult(null);
    setBacktestError("");
    try {
      const response = await fetch(`/api/custom-strategies/${strategy.id}/backtest`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ from_date: fromDate || null, to_date: toDate || null }),
      });
      const data = await response.json();
      if (response.ok) {
        setBacktestResult(data);
        setShowBacktestModal(true);
        loadStrategies();
        toast.success("Backtest completed successfully!");
      } else {
        const errText = Array.isArray(data.detail) ? data.detail.join(" ") : data.detail || "Backtest failed.";
        setBacktestError(errText);
        toast.error(errText);
      }
    } catch {
      setBacktestError("Backtest request failed.");
      toast.error("Backtest request failed.");
    } finally {
      setBacktesting(false);
    }
  };

  const handleDeleteStrategy = async (strategy: CustomStrategy) => {
    try {
      const response = await fetch(`/api/custom-strategies/${strategy.id}`, {
        method: "DELETE",
        credentials: "include",
      });
      if (response.ok) {
        const updatedStrategies = strategies.filter(s => s.id !== strategy.id);
        setStrategies(updatedStrategies);
        if (selectedStrategy?.id === strategy.id) {
          setSelectedStrategy(updatedStrategies.length > 0 ? updatedStrategies[0] : null);
          setBacktestResult(null);
          setBacktestError("");
        }
        toast.success("Strategy deleted successfully!");
      } else {
        const data = await response.json();
        toast.error(data.detail || "Failed to delete strategy.");
      }
    } catch (error) {
      console.error("Failed to delete strategy:", error);
      toast.error("Failed to delete strategy.");
    }
  };

  const canTransitionTo = (currentStatus: string, targetStatus: string) => {
    const transitions: Record<string, string[]> = {
      "DRAFT": ["BACKTESTING", "STOPPED"],
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
            <div className="divide-y" style={{ borderColor: C.border }}>
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
                      onKeyDown={(e) => { if (e.key === "Enter") { setSelectedStrategy(strategy); setBacktestResult(null); setBacktestError(""); } }}
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
                                className="flex items-center justify-center w-6 h-6 rounded-md transition-colors focus:outline-none hover:opacity-80"
                                style={{ backgroundColor: C.sellBg, color: C.red }} title="Stop strategy">
                                <AlertCircle size={12} />
                              </button>
                            )}
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
                                className="flex items-center justify-center w-6 h-6 rounded-md transition-colors focus:outline-none hover:opacity-80"
                                style={{ backgroundColor: C.sellBg, color: C.red }} title="Delete strategy">
                                <Trash2 size={12} />
                              </button>
                            )}
                          </div>
                        </div>
                        <div className="flex items-center gap-2 mt-1.5 flex-wrap">
                          <StatusPill status={strategy.status} />
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
                  {canTransitionTo(selectedStrategy.status, "BACKTESTING") && selectedStrategy.status === "DRAFT" && (
                    <button onClick={() => setBacktestRangeTarget(selectedStrategy)} disabled={backtesting}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold transition-colors focus:outline-none hover:opacity-80"
                      style={{ backgroundColor: C.buyBg, color: C.blue }}>
                      <BarChart3 size={14} /> {backtesting ? "Backtesting..." : "Backtest"}
                    </button>
                  )}
                  {selectedStrategy.status === "BACKTESTING" && (
                    <button onClick={() => setBacktestRangeTarget(selectedStrategy)} disabled={backtesting}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold transition-colors focus:outline-none hover:opacity-80"
                      style={{ backgroundColor: C.buyBg, color: C.blue }}>
                      <BarChart3 size={14} /> {backtesting ? "Backtesting..." : "Re-run Backtest"}
                    </button>
                  )}
                  {backtestResult && (
                    <button onClick={() => setShowBacktestModal(true)}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold transition-colors focus:outline-none hover:opacity-80"
                      style={{ backgroundColor: C.hover, color: C.text }}>
                      <TrendingUp size={14} /> View Backtest Results
                    </button>
                  )}
                  {canTransitionTo(selectedStrategy.status, "PAPER_TRADING") && (
                    <button onClick={() => handleStatusChange(selectedStrategy, "PAPER_TRADING")}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold transition-colors focus:outline-none hover:opacity-80"
                      style={{ backgroundColor: "#e6f4ea", color: C.green }}>
                      <Play size={14} /> Paper Trade
                    </button>
                  )}
                  {canTransitionTo(selectedStrategy.status, "LIVE") && (
                    <button onClick={() => {
                      setConfirmModal({
                        title: "Go LIVE",
                        message: "Go LIVE — Upstox will place real orders with real money. Continue?",
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
                    <button onClick={() => handleStatusChange(selectedStrategy, "PAUSED")}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold transition-colors focus:outline-none hover:opacity-80"
                      style={{ backgroundColor: "#fff7ed", color: C.orange }}>
                      <Pause size={14} /> Disable
                    </button>
                  )}
                  {selectedStrategy.status === "PAUSED" && (
                    <button onClick={() => handleStatusChange(selectedStrategy, "PAPER_TRADING")}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold transition-colors focus:outline-none hover:opacity-80"
                      style={{ backgroundColor: "#e6f4ea", color: C.green }}>
                      <Play size={14} /> Enable
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
                </div>
              </div>

              <div className="p-6 space-y-6">
                <div className="grid grid-cols-3 gap-4">
                  <StatCard icon={BarChart3} label="Backtest (avg/cycle)" value={selectedStrategy.backtest_return_pct} accent={C.blue} />
                  <StatCard icon={Activity} label="Paper (last trade)" value={selectedStrategy.paper_return_pct} accent={C.green} />
                  <StatCard icon={Target} label="Live (last trade)" value={selectedStrategy.live_return_pct} accent={C.orange} />
                </div>

                {selectedStrategy.rules && (
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
                                <span className="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-bold" style={{ background: C.hover, color: C.text }}>{leg.option_type}</span>
                              </td>
                              <td className="px-4 py-2.5 text-gray-600">{strikeLabel(leg.strike_selection)}</td>
                              <td className="px-4 py-2.5 text-right font-medium text-gray-700">{leg.lots}</td>
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
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                      <p className="text-[11px] text-gray-400 mt-2">Based on current live option premiums if entered right now — not a forecast, and moves as the market does.</p>
                    </div>
                  </div>
                )}

                <div className="flex items-center gap-2 text-xs pt-1">
                  <span className="px-2.5 py-1 rounded-lg" style={{ background: C.hover, color: C.muted }}>Created {new Date(selectedStrategy.created_at).toLocaleDateString()}</span>
                  {selectedStrategy.deployed_at && (
                    <span className="px-2.5 py-1 rounded-lg" style={{ background: "#fff7ed", color: C.orange }}>Deployed {new Date(selectedStrategy.deployed_at).toLocaleDateString()}</span>
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
                            <td className="px-4 py-2.5 text-right">{leg.greeks?.delta ?? "—"}</td>
                            <td className="px-4 py-2.5 text-right">{leg.greeks?.gamma ?? "—"}</td>
                            <td className="px-4 py-2.5 text-right">{leg.greeks?.theta ?? "—"}</td>
                            <td className="px-4 py-2.5 text-right">{leg.greeks?.vega ?? "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}

            {backtestError && (
              <div className="px-4 py-3 rounded-xl bg-red-50 border border-red-200 text-red-600 text-sm">{backtestError}</div>
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
          onSuccess={() => { setModalMode(null); loadStrategies(); }}
          editStrategy={modalMode === "edit" && selectedStrategy ? {
            id: selectedStrategy.id,
            name: selectedStrategy.name,
            instrument_type: selectedStrategy.instrument_type,
            symbols: selectedStrategy.symbols,
            // Server-validated (rule_schema.validate_rules) — literal unions are guaranteed at runtime.
            rules: selectedStrategy.rules as unknown as { legs: { action: "BUY" | "SELL"; option_type: "CE" | "PE"; strike_selection: { mode: "ATM" | "OTM_PERCENT" | "OTM_POINTS" | "FIXED"; value: number | null }; lots: number }[]; entry: { mode: "IMMEDIATE" | "AT_TIME"; time: string | null }; expiry?: { mode: "WEEKLY" | "MONTHLY" }; exit: { take_profit_pct: number | null; stop_loss_pct: number | null; exit_time: string | null; exit_days_before_expiry: number } } | null,
          } : null}
        />
      )}

      {backtestRangeTarget && (
        <BacktestRangeModal
          strategy={backtestRangeTarget}
          onClose={() => setBacktestRangeTarget(null)}
          onRun={(fromDate, toDate) => {
            runBacktest(backtestRangeTarget, fromDate, toDate);
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
          <div className="bg-white rounded-2xl p-6 max-w-sm w-full mx-4 border shadow-2xl" style={{ borderColor: C.border }}>
            <h3 className="text-base font-bold text-gray-800">{confirmModal.title}</h3>
            <p className="text-xs text-gray-500 mt-2 leading-relaxed">{confirmModal.message}</p>
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
