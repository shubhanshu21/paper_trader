import { useCallback, useEffect, useState } from "react";
import { Activity, RefreshCw, Percent } from "lucide-react";
import { api, ApiError, IvScreenerRow } from "../api";
import { C, FONT } from "../lib/format";

function rankColor(rank: number): string {
  if (rank >= 70) return C.green;   // rich premium relative to its own history — attractive to sell
  if (rank <= 30) return C.red;     // cheap relative to its own history — attractive to buy, not sell
  return C.muted;
}

export default function IvScreenerView() {
  const [rows, setRows] = useState<IvScreenerRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchRows = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const data = await api.getIvScreener();
      setRows(data.rows || []);
    } catch (err) {
      console.error("Failed to load IV screener:", err);
      const detail = err instanceof ApiError && err.detail ? (Array.isArray(err.detail) ? err.detail.join(" ") : err.detail) : null;
      setError(detail || (err instanceof Error ? err.message : "Could not load IV screener data."));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchRows();
  }, [fetchRows]);

  return (
    <div className="flex-1 overflow-y-auto p-4 md:p-8 bg-gray-50 min-h-screen" style={FONT}>
      <div className="flex flex-col md:flex-row md:items-center justify-between mb-8 gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-800 flex items-center gap-2">
            <Activity size={24} style={{ color: C.orange }} />
            IV Screener
          </h1>
          <p className="text-xs text-gray-500 mt-1">
            Live ATM IV and IV rank (where today's IV sits within its own trailing ~1-year range) across your watchlist symbols.
            High rank (green) means premium is rich relative to its own history — a candidate for selling; low rank (red) means it's cheap.
          </p>
        </div>
        <button
          onClick={fetchRows}
          disabled={loading}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg bg-white border shadow-sm text-gray-600 hover:bg-gray-50 disabled:opacity-50 focus:outline-none self-start"
          style={{ borderColor: C.border }}
        >
          <RefreshCw size={13} className={loading ? "animate-spin" : ""} /> Refresh
        </button>
      </div>

      {loading && rows.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 bg-white rounded-2xl border shadow-sm" style={{ borderColor: C.border }}>
          <span className="w-8 h-8 border-3 border-orange-500 border-t-transparent rounded-full animate-spin mb-4" />
          <p className="text-sm font-semibold text-gray-500">Solving live IV...</p>
        </div>
      ) : error ? (
        <div className="p-6 rounded-xl border flex items-center justify-between gap-4" style={{ backgroundColor: C.sellBg, borderColor: C.red, color: C.red }}>
          <p className="font-semibold text-sm">{error}</p>
          <button onClick={fetchRows} className="shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold text-white hover:opacity-90" style={{ backgroundColor: C.red }}>
            <RefreshCw size={12} /> Retry
          </button>
        </div>
      ) : (
        <div className="bg-white rounded-2xl border shadow-sm overflow-hidden" style={{ borderColor: C.border }}>
          <div className="px-5 py-4 border-b flex items-center gap-1.5" style={{ borderColor: C.border }}>
            <Percent size={16} style={{ color: C.orange }} />
            <h3 className="text-sm font-semibold text-gray-700">IV Rank by Symbol ({rows.length})</h3>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  <th className="px-5 py-3.5 text-left text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">Symbol</th>
                  <th className="px-5 py-3.5 text-right text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">Current ATM IV</th>
                  <th className="px-5 py-3.5 text-left text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">IV Rank</th>
                  <th className="px-5 py-3.5 text-right text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">History</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {rows.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="text-center py-10 text-xs font-semibold text-gray-400">
                      Your watchlist is empty — add symbols to the Watchlist to screen their IV.
                    </td>
                  </tr>
                ) : (
                  rows.map((r) => (
                    <tr key={r.symbol} className="hover:bg-gray-50 transition-colors">
                      <td className="px-5 py-4 text-xs font-bold text-gray-700">{r.symbol}</td>
                      <td className="px-5 py-4 text-xs font-semibold text-gray-700 text-right tabular-nums">
                        {r.current_iv != null ? `${r.current_iv.toFixed(1)}%` : "—"}
                      </td>
                      <td className="px-5 py-4 text-xs">
                        {r.iv_rank != null ? (
                          <div className="flex items-center gap-2">
                            <div className="w-24 h-2 rounded-full overflow-hidden" style={{ background: C.hover }}>
                              <div className="h-full rounded-full" style={{ width: `${r.iv_rank}%`, backgroundColor: rankColor(r.iv_rank) }} />
                            </div>
                            <span className="font-bold tabular-nums" style={{ color: rankColor(r.iv_rank) }}>{r.iv_rank.toFixed(0)}</span>
                          </div>
                        ) : (
                          <span className="text-gray-400">Insufficient history</span>
                        )}
                      </td>
                      <td className="px-5 py-4 text-xs text-right text-gray-500 tabular-nums">
                        {r.history_days}/{r.history_required}d
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
