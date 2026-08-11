import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSelector } from "react-redux";
import { BarChart3, Download, RefreshCw, TrendingUp, TrendingDown } from "lucide-react";
import {
  api, ApiError, PerformanceMetrics, PerformanceAnalytics, TradeJournalEntryRow,
  PerformancePeriod, PerformanceMode,
} from "../api";
import { C, FONT, inr, withSign, sign, fmtDateTime } from "../lib/format";
import { Select } from "./Common";
import { RootState } from "../store";

const PERIODS: { value: PerformancePeriod; label: string }[] = [
  { value: "all", label: "All Time" },
  { value: "today", label: "Today" },
  { value: "week", label: "This Week" },
  { value: "month", label: "This Month" },
  { value: "quarter", label: "This Quarter" },
  { value: "year", label: "This Year" },
];

function Metric({ label, value, color, sub }: { label: string; value: string; color?: string; sub?: string }) {
  return (
    <div className="bg-white rounded-2xl border shadow-sm px-5 py-4" style={{ borderColor: C.border }}>
      <div className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide">{label}</div>
      <div className="text-xl font-bold tabular-nums mt-1" style={{ color: color || C.text }}>{value}</div>
      {sub && <div className="text-[11px] text-gray-400 mt-0.5">{sub}</div>}
    </div>
  );
}

function EquityCurve({ curve }: { curve: { date: string | null; cumulative_pnl: number }[] }) {
  if (curve.length < 2) {
    return <div className="flex items-center justify-center h-56 text-xs text-gray-400 border rounded-xl" style={{ borderColor: C.border2 }}>Not enough closed trades yet for an equity curve.</div>;
  }
  const width = 760, height = 240, padding = 36;
  const values = curve.map((p) => p.cumulative_pnl);
  const minV = Math.min(0, ...values), maxV = Math.max(0, ...values);
  const xScale = (i: number) => padding + (i / (curve.length - 1)) * (width - 2 * padding);
  const yScale = (v: number) => height - padding - ((v - minV) / (maxV - minV || 1)) * (height - 2 * padding);
  const zeroY = yScale(0);
  const points = curve.map((p, i) => `${xScale(i)},${yScale(p.cumulative_pnl)}`).join(" ");
  const last = curve[curve.length - 1].cumulative_pnl;

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto">
      <line x1={padding} y1={zeroY} x2={width - padding} y2={zeroY} stroke={C.border2} strokeWidth={1} />
      <polyline points={points} fill="none" stroke={last >= 0 ? C.green : C.red} strokeWidth={2} />
      <text x={padding} y={height - 8} fontSize={10} fill={C.muted}>{curve[0].date ? fmtDateTime(curve[0].date) : ""}</text>
      <text x={width - padding} y={height - 8} fontSize={10} fill={C.muted} textAnchor="end">
        {curve[curve.length - 1].date ? fmtDateTime(curve[curve.length - 1].date as string) : ""}
      </text>
    </svg>
  );
}

function BreakdownTable({ title, rows, showWinRate }: { title: string; rows: [string, { trades: number; total_pnl: number; average_pnl: number; win_rate?: number }][]; showWinRate: boolean }) {
  return (
    <div className="bg-white rounded-2xl border shadow-sm overflow-hidden" style={{ borderColor: C.border }}>
      <div className="px-4 py-2.5 border-b text-xs font-semibold text-gray-700" style={{ borderColor: C.border }}>{title}</div>
      {rows.length === 0 ? (
        <p className="text-xs text-gray-400 py-6 text-center">No data yet.</p>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-[10px] uppercase tracking-wide text-gray-400 border-b" style={{ borderColor: C.border2 }}>
              <th className="text-left py-2 px-4 font-medium">Name</th>
              <th className="text-right py-2 px-4 font-medium">Trades</th>
              {showWinRate && <th className="text-right py-2 px-4 font-medium">Win %</th>}
              <th className="text-right py-2 px-4 font-medium">Avg P&amp;L</th>
              <th className="text-right py-2 px-4 font-medium">Total P&amp;L</th>
            </tr>
          </thead>
          <tbody className="divide-y" style={{ borderColor: C.border2 }}>
            {rows.map(([name, r]) => (
              <tr key={name}>
                <td className="py-2 px-4 font-semibold text-gray-700">{name}</td>
                <td className="py-2 px-4 text-right tabular-nums">{r.trades}</td>
                {showWinRate && <td className="py-2 px-4 text-right tabular-nums">{r.win_rate != null ? `${r.win_rate.toFixed(1)}%` : "—"}</td>}
                <td className="py-2 px-4 text-right tabular-nums" style={{ color: sign(r.average_pnl) }}>{withSign(r.average_pnl)}</td>
                <td className="py-2 px-4 text-right tabular-nums font-semibold" style={{ color: sign(r.total_pnl) }}>{withSign(r.total_pnl)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default function PerformanceView() {
  const currentUser = useSelector((s: RootState) => s.auth.currentUser);
  const userId = currentUser?.id;

  const [period, setPeriod] = useState<PerformancePeriod>("all");
  const [mode, setMode] = useState<"all" | "paper" | "live">("all");
  const [metrics, setMetrics] = useState<PerformanceMetrics | null>(null);
  const [analytics, setAnalytics] = useState<PerformanceAnalytics | null>(null);
  const [entries, setEntries] = useState<TradeJournalEntryRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);

  const modeParam: PerformanceMode = mode === "all" ? undefined : mode;

  // Guards against an earlier, slower request resolving AFTER a newer one
  // — rapidly toggling period/mode could otherwise let a stale response
  // land last and overwrite the screen with figures for the wrong filter
  // combination. Same pattern as StrategiesView.tsx's payoffRequestIdRef.
  const loadRequestIdRef = useRef(0);

  const load = useCallback(async () => {
    if (!userId) return;
    const requestId = ++loadRequestIdRef.current;
    setLoading(true);
    setError(null);
    try {
      const [m, a, j] = await Promise.all([
        api.getPerformanceMetrics(userId, period, modeParam),
        api.getPerformanceAnalytics(userId, modeParam),
        api.getTradeJournal({ period, mode: modeParam, limit: 100 }),
      ]);
      if (loadRequestIdRef.current !== requestId) return;
      setMetrics(m);
      setAnalytics(a);
      setEntries(j.entries || []);
    } catch (err) {
      if (loadRequestIdRef.current !== requestId) return;
      const detail = err instanceof ApiError && err.detail ? (Array.isArray(err.detail) ? err.detail.join(" ") : err.detail) : null;
      setError(detail || (err instanceof Error ? err.message : "Could not load performance data."));
    } finally {
      if (loadRequestIdRef.current === requestId) setLoading(false);
    }
  }, [userId, period, modeParam]);

  useEffect(() => { load(); }, [load]);

  const handleExport = async (format: "csv" | "json") => {
    if (!userId) return;
    setExporting(true);
    try {
      const res = await api.exportTradeJournal(userId, format, period, modeParam);
      const content = format === "csv" ? (res.csv || "") : JSON.stringify(res.trades || [], null, 2);
      const blob = new Blob([content], { type: format === "csv" ? "text/csv" : "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `trade-journal-${period}.${format}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Export failed.");
    } finally {
      setExporting(false);
    }
  };

  const strategyRows = useMemo(
    () => Object.entries(analytics?.strategy_breakdown || {}).sort((a, b) => b[1].total_pnl - a[1].total_pnl),
    [analytics],
  );
  const symbolRows = useMemo(
    () => Object.entries(analytics?.symbol_performance || {}).sort((a, b) => b[1].total_pnl - a[1].total_pnl),
    [analytics],
  );
  const dowRows = useMemo(
    () => Object.entries(analytics?.day_of_week_performance || {}),
    [analytics],
  );

  return (
    <div className="flex-1 overflow-y-auto p-4 md:p-6 bg-gray-50 min-h-screen" style={FONT}>
      <div className="flex flex-col md:flex-row md:items-center justify-between mb-4 gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-800 flex items-center gap-2">
            <BarChart3 size={24} style={{ color: C.orange }} />
            Trade Journal
          </h1>
          <p className="text-xs text-gray-500 mt-1">
            Real performance from your actual closed trades — win rate, profit factor, drawdown, and equity curve.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="w-40">
            <Select value={period} onChange={(v) => setPeriod(v as PerformancePeriod)} options={PERIODS} />
          </div>
          <div className="w-32">
            <Select
              value={mode}
              onChange={(v) => setMode(v as "all" | "paper" | "live")}
              options={[{ value: "all", label: "All Modes" }, { value: "paper", label: "Paper" }, { value: "live", label: "Live" }]}
            />
          </div>
          <button onClick={load} disabled={loading} className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg bg-gray-50 border hover:bg-gray-100 disabled:opacity-50" style={{ borderColor: C.border2 }}>
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} /> Refresh
          </button>
          <button onClick={() => handleExport("csv")} disabled={exporting} className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg text-white disabled:opacity-50" style={{ backgroundColor: C.orange }}>
            <Download size={12} /> Export CSV
          </button>
        </div>
      </div>

      {error && (
        <div className="p-4 rounded-xl border text-sm font-semibold mb-4" style={{ backgroundColor: C.sellBg, borderColor: C.red, color: C.red }}>{error}</div>
      )}

      {metrics && metrics.total_trades === 0 ? (
        <div className="bg-white rounded-2xl border shadow-sm p-10 text-center" style={{ borderColor: C.border }}>
          <p className="text-sm text-gray-500">No closed trades in this period yet — your journal fills in automatically as strategies close positions.</p>
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3 mb-4">
            <Metric label="Total P&L" value={metrics ? withSign(metrics.total_pnl) : "—"} color={metrics ? sign(metrics.total_pnl) : undefined} />
            <Metric label="Win Rate" value={metrics ? `${metrics.win_rate.toFixed(1)}%` : "—"} sub={metrics ? `${metrics.winning_trades}W / ${metrics.losing_trades}L` : undefined} />
            <Metric label="Profit Factor" value={metrics ? metrics.profit_factor.toFixed(2) : "—"} />
            <Metric label="Max Drawdown" value={metrics ? inr(metrics.max_drawdown) : "—"} color={C.red} />
            <Metric label="Sharpe Ratio" value={metrics?.sharpe_ratio != null ? metrics.sharpe_ratio.toFixed(2) : "N/A"} />
            <Metric label="Total Trades" value={metrics ? String(metrics.total_trades) : "—"} />
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-4">
            <div className="lg:col-span-2 bg-white rounded-2xl border shadow-sm p-4" style={{ borderColor: C.border }}>
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-semibold text-gray-700">Equity Curve</span>
                {metrics && (
                  <span className="text-[11px] font-semibold flex items-center gap-1" style={{ color: sign(metrics.total_pnl) }}>
                    {metrics.total_pnl >= 0 ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
                    {withSign(metrics.total_pnl)}
                  </span>
                )}
              </div>
              <EquityCurve curve={metrics?.equity_curve || []} />
            </div>
            <div className="bg-white rounded-2xl border shadow-sm p-4" style={{ borderColor: C.border }}>
              <div className="text-xs font-semibold text-gray-700 mb-3">Best / Worst</div>
              <div className="space-y-3">
                <Metric label="Best Trade" value={metrics?.best_trade != null ? withSign(metrics.best_trade) : "—"} color={C.green} />
                <Metric label="Worst Trade" value={metrics?.worst_trade != null ? withSign(metrics.worst_trade) : "—"} color={C.red} />
                <Metric label="Avg Win / Loss" value={metrics ? `${withSign(metrics.average_win)} / ${withSign(metrics.average_loss)}` : "—"} />
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-4">
            <BreakdownTable title="By Strategy" rows={strategyRows} showWinRate />
            <BreakdownTable title="By Symbol" rows={symbolRows} showWinRate={false} />
            <BreakdownTable title="By Day of Week" rows={dowRows} showWinRate={false} />
          </div>

          <div className="bg-white rounded-2xl border shadow-sm overflow-hidden" style={{ borderColor: C.border }}>
            <div className="px-4 py-2.5 border-b text-xs font-semibold text-gray-700" style={{ borderColor: C.border }}>Recent Trades</div>
            {entries.length === 0 ? (
              <p className="text-xs text-gray-400 py-6 text-center">No trades to show.</p>
            ) : (
              <div className="overflow-x-auto max-h-[480px] overflow-y-auto">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-white z-10">
                    <tr className="text-[10px] uppercase tracking-wide text-gray-400 border-b" style={{ borderColor: C.border2 }}>
                      <th className="text-left py-2 px-4 font-medium">Strategy</th>
                      <th className="text-left py-2 px-4 font-medium">Symbol</th>
                      <th className="text-left py-2 px-4 font-medium">Mode</th>
                      <th className="text-right py-2 px-4 font-medium">Legs</th>
                      <th className="text-left py-2 px-4 font-medium">Exited</th>
                      <th className="text-right py-2 px-4 font-medium">P&amp;L</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y" style={{ borderColor: C.border2 }}>
                    {entries.map((e, i) => (
                      <tr key={i}>
                        <td className="py-2 px-4 font-semibold text-gray-700">{e.strategy}</td>
                        <td className="py-2 px-4">{e.symbol}</td>
                        <td className="py-2 px-4">
                          <span className="px-1.5 py-0.5 rounded text-[10px] font-bold" style={e.mode === "live" ? { background: C.buyBg, color: C.buyText } : { background: "#f3f4f6", color: C.muted }}>
                            {e.mode.toUpperCase()}
                          </span>
                        </td>
                        <td className="py-2 px-4 text-right tabular-nums">{e.legs}</td>
                        <td className="py-2 px-4 text-gray-500">{e.exit_date ? fmtDateTime(e.exit_date) : "—"}</td>
                        <td className="py-2 px-4 text-right tabular-nums font-semibold" style={{ color: sign(e.pnl) }}>{withSign(e.pnl)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
