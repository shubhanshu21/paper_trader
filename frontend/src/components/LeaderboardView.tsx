import { useEffect, useState } from "react";
import { Trophy, TrendingUp, Filter, BarChart3, Award, Users, Percent } from "lucide-react";
import { api } from "../api";
import { C, FONT, withSign, sign } from "../lib/format";

interface LeaderboardRow {
  strategy: string;
  symbol: string;
  mode: string;
  category: string;
  total_pnl: number;
  trades: number;
  wins: number;
  win_rate_pct: number;
  avg_pnl: number;
}

export default function LeaderboardView() {
  const [rows, setRows] = useState<LeaderboardRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filters
  const [modeFilter, setModeFilter] = useState<"all" | "paper" | "live">("all");
  const [categoryFilter, setCategoryFilter] = useState<"all" | "stock" | "index" | "commodity">("all");

  useEffect(() => {
    async function fetchLeaderboard() {
      try {
        setLoading(true);
        const data = await api.getLeaderboard();
        setRows(data.rows || []);
      } catch (err: unknown) {
        console.error("Failed to load leaderboard:", err);
        setError("Could not load leaderboard data. Please check if api service is running.");
      } finally {
        setLoading(false);
      }
    }
    fetchLeaderboard();
  }, []);

  // Filtered Rows
  const filteredRows = rows.filter((r) => {
    const modeMatch = modeFilter === "all" || r.mode === modeFilter;
    const catMatch = categoryFilter === "all" || r.category === categoryFilter;
    return modeMatch && catMatch;
  });

  // Calculate Metrics
  const totalPnL = filteredRows.reduce((sum, r) => sum + r.total_pnl, 0);
  const totalTrades = filteredRows.reduce((sum, r) => sum + r.trades, 0);
  const totalWins = filteredRows.reduce((sum, r) => sum + r.wins, 0);
  const averageWinRate = totalTrades > 0 ? (totalWins / totalTrades) * 100 : 0;

  // Chart Data: Top 5 by P&L
  const topPerformers = [...filteredRows]
    .filter((r) => r.total_pnl > 0)
    .slice(0, 5);

  const maxPnLForChart = topPerformers.length > 0 
    ? Math.max(...topPerformers.map(r => r.total_pnl)) 
    : 1000;

  return (
    <div className="flex-1 overflow-y-auto p-4 md:p-8 bg-gray-50 min-h-screen" style={FONT}>
      {/* Title Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between mb-8 gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-800 flex items-center gap-2">
            <Trophy className="animate-bounce" size={26} style={{ color: C.orange }} />
            Strategy Leaderboard
          </h1>
          <p className="text-xs text-gray-500 mt-1">
            Real-time performance ranking of all option and equity strategies across asset classes.
          </p>
        </div>

        {/* Global Controls / Filters */}
        <div className="flex flex-wrap items-center gap-2 bg-white p-1.5 rounded-xl border shadow-sm" style={{ borderColor: C.border }}>
          <div className="flex items-center gap-1 text-[11px] px-2 font-medium text-gray-400">
            <Filter size={12} />
            <span>FILTER BY:</span>
          </div>

          {/* Mode Filter */}
          <div className="flex gap-1.5">
            {(["all", "paper", "live"] as const).map((m) => {
              const active = modeFilter === m;
              return (
                <button
                  key={m}
                  onClick={() => setModeFilter(m)}
                  className="px-3 py-1 text-[11px] font-semibold uppercase tracking-wider transition-colors rounded-md focus:outline-none"
                  style={{
                    backgroundColor: active ? C.orange : "rgba(0,0,0,0.04)",
                    color: active ? "#ffffff" : "#666666",
                  }}
                >
                  {m}
                </button>
              );
            })}
          </div>

          {/* Category Filter */}
          <div className="flex gap-1.5">
            {(["all", "index", "stock", "commodity"] as const).map((cat) => {
              const active = categoryFilter === cat;
              return (
                <button
                  key={cat}
                  onClick={() => setCategoryFilter(cat)}
                  className="px-3 py-1 text-[11px] font-semibold uppercase tracking-wider transition-colors rounded-md focus:outline-none"
                  style={{
                    backgroundColor: active ? C.orange : "rgba(0,0,0,0.04)",
                    color: active ? "#ffffff" : "#666666",
                  }}
                >
                  {cat}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {loading ? (
        <div className="flex flex-col items-center justify-center py-20 bg-white rounded-2xl border shadow-sm" style={{ borderColor: C.border }}>
          <span className="w-8 h-8 border-3 border-orange-500 border-t-transparent rounded-full animate-spin mb-4" />
          <p className="text-sm font-semibold text-gray-500">Loading performance data...</p>
        </div>
      ) : error ? (
        <div className="p-6 bg-red-50 text-red-700 rounded-xl border border-red-200">
          <p className="font-semibold text-sm">{error}</p>
        </div>
      ) : (
        <div className="space-y-6">
          {/* Summary Cards */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            {/* Total PnL */}
            <div className="bg-white p-5 rounded-2xl border shadow-sm flex items-center justify-between" style={{ borderColor: C.border }}>
              <div>
                <p className="text-[11px] font-bold text-gray-400 uppercase tracking-wider">Net Profit &amp; Loss</p>
                <h3 className="text-xl font-bold mt-1 tabular-nums" style={{ color: sign(totalPnL) }}>
                  {withSign(totalPnL)}
                </h3>
              </div>
              <div className="p-3 bg-gray-50 rounded-xl">
                <TrendingUp size={20} style={{ color: totalPnL > 0 ? C.green : totalPnL < 0 ? C.red : C.muted }} />
              </div>
            </div>

            {/* Average Win Rate */}
            <div className="bg-white p-5 rounded-2xl border shadow-sm flex items-center justify-between" style={{ borderColor: C.border }}>
              <div>
                <p className="text-[11px] font-bold text-gray-400 uppercase tracking-wider">Average Win Rate</p>
                <h3 className="text-xl font-bold mt-1 text-gray-800 tabular-nums">
                  {averageWinRate.toFixed(1)}%
                </h3>
              </div>
              <div className="p-3 bg-gray-50 rounded-xl">
                <Percent size={20} style={{ color: C.blue }} />
              </div>
            </div>

            {/* Total Trades */}
            <div className="bg-white p-5 rounded-2xl border shadow-sm flex items-center justify-between" style={{ borderColor: C.border }}>
              <div>
                <p className="text-[11px] font-bold text-gray-400 uppercase tracking-wider">Total Trades Closed</p>
                <h3 className="text-xl font-bold mt-1 text-gray-800 tabular-nums">
                  {totalTrades}
                </h3>
              </div>
              <div className="p-3 bg-gray-50 rounded-xl">
                <BarChart3 size={20} style={{ color: C.orange }} />
              </div>
            </div>

            {/* Total Wins */}
            <div className="bg-white p-5 rounded-2xl border shadow-sm flex items-center justify-between" style={{ borderColor: C.border }}>
              <div>
                <p className="text-[11px] font-bold text-gray-400 uppercase tracking-wider">Successful Cycles</p>
                <h3 className="text-xl font-bold mt-1 text-gray-800 tabular-nums">
                  {totalWins}
                </h3>
              </div>
              <div className="p-3 bg-gray-50 rounded-xl">
                <Award size={20} style={{ color: C.orange }} />
              </div>
            </div>
          </div>

          {/* Graphs Section */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            {/* Top Performers Chart */}
            <div className="bg-white p-5 rounded-2xl border shadow-sm lg:col-span-2" style={{ borderColor: C.border }}>
              <h3 className="text-sm font-semibold text-gray-700 mb-4 flex items-center gap-1.5">
                <TrendingUp size={16} style={{ color: C.green }} />
                Top Performers (Total Net P&amp;L)
              </h3>
              
              {topPerformers.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-12 text-gray-400">
                  <BarChart3 size={32} strokeWidth={1.5} className="mb-2" />
                  <p className="text-xs">No profitable runs to display in the chart.</p>
                </div>
              ) : (
                <div className="space-y-4">
                  {topPerformers.map((r, i) => {
                    const widthPct = Math.max(8, (r.total_pnl / maxPnLForChart) * 100);
                    return (
                      <div key={i} className="space-y-1">
                        <div className="flex justify-between text-xs font-semibold text-gray-600">
                          <span className="flex items-center gap-1">
                            <span className="w-5 h-5 rounded-full bg-gray-100 flex items-center justify-center text-[10px] text-gray-500 font-bold">
                              #{i + 1}
                            </span>
                            {r.strategy} ({r.symbol})
                          </span>
                          <span className="text-green-600">{withSign(r.total_pnl)}</span>
                        </div>
                        <div className="w-full bg-gray-100 h-6 rounded-lg overflow-hidden flex">
                          <div 
                            className="h-full rounded-lg transition-all duration-500"
                            style={{ 
                              width: `${widthPct}%`,
                              backgroundColor: C.orange
                            }}
                          />
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {/* Distribution Graph */}
            <div className="bg-white p-5 rounded-2xl border shadow-sm" style={{ borderColor: C.border }}>
              <h3 className="text-sm font-semibold text-gray-700 mb-4 flex items-center gap-1.5">
                <Users size={16} style={{ color: C.blue }} />
                Asset Distribution
              </h3>

              <div className="flex flex-col items-center justify-center">
                {/* Custom SVG Donut Chart */}
                {filteredRows.length === 0 ? (
                  <div className="py-12 text-gray-400 text-xs">No trade distribution to display.</div>
                ) : (() => {
                  const counts = filteredRows.reduce(
                    (acc, r) => {
                      acc[r.category] = (acc[r.category] || 0) + r.trades;
                      return acc;
                    },
                    { stock: 0, index: 0, commodity: 0 } as Record<string, number>
                  );
                  const total = counts.stock + counts.index + counts.commodity;
                  const stockPct = total > 0 ? (counts.stock / total) * 100 : 0;
                  const indexPct = total > 0 ? (counts.index / total) * 100 : 0;
                  const commPct = total > 0 ? (counts.commodity / total) * 100 : 0;

                  // Simple SVG Pie calculation helper
                  const getCircumference = (radius: number) => 2 * Math.PI * radius;
                  const radius = 35;
                  const circ = getCircumference(radius);
                  
                  const stroke1 = circ * (stockPct / 100);
                  const stroke2 = circ * (indexPct / 100);
                  const stroke3 = circ * (commPct / 100);

                  return (
                    <div className="w-full flex flex-col items-center">
                      <svg width="120" height="120" viewBox="0 0 100 100" className="transform -rotate-90">
                        {/* Circle background */}
                        <circle cx="50" cy="50" r={radius} fill="transparent" stroke="#f3f4f6" strokeWidth="15" />
                        
                        {/* Stock slice */}
                        {stroke1 > 0 && (
                          <circle 
                            cx="50" cy="50" r={radius} fill="transparent" 
                            stroke={C.green} strokeWidth="15" 
                            strokeDasharray={`${stroke1} ${circ}`} 
                            strokeDashoffset={0}
                          />
                        )}
                        {/* Index slice */}
                        {stroke2 > 0 && (
                          <circle 
                            cx="50" cy="50" r={radius} fill="transparent" 
                            stroke={C.blue} strokeWidth="15" 
                            strokeDasharray={`${stroke2} ${circ}`} 
                            strokeDashoffset={-stroke1}
                          />
                        )}
                        {/* Commodity slice */}
                        {stroke3 > 0 && (
                          <circle 
                            cx="50" cy="50" r={radius} fill="transparent" 
                            stroke={C.orange} strokeWidth="15" 
                            strokeDasharray={`${stroke3} ${circ}`} 
                            strokeDashoffset={-(stroke1 + stroke2)}
                          />
                        )}
                      </svg>

                      {/* Legend */}
                      <div className="w-full grid grid-cols-3 gap-2 mt-6 text-center">
                        <div className="p-2 rounded-xl border" style={{ backgroundColor: C.green + "10", borderColor: C.green + "30" }}>
                          <span className="block text-[10px] font-bold" style={{ color: C.green }}>STOCKS</span>
                          <span className="block text-xs font-bold text-gray-700 mt-0.5">{counts.stock} ({stockPct.toFixed(0)}%)</span>
                        </div>
                        <div className="p-2 rounded-xl border" style={{ backgroundColor: C.blue + "10", borderColor: C.blue + "30" }}>
                          <span className="block text-[10px] font-bold" style={{ color: C.blue }}>INDICES</span>
                          <span className="block text-xs font-bold text-gray-700 mt-0.5">{counts.index} ({indexPct.toFixed(0)}%)</span>
                        </div>
                        <div className="p-2 rounded-xl border" style={{ backgroundColor: C.orange + "10", borderColor: C.orange + "30" }}>
                          <span className="block text-[10px] font-bold" style={{ color: C.orange }}>COMMODITIES</span>
                          <span className="block text-xs font-bold text-gray-700 mt-0.5">{counts.commodity} ({commPct.toFixed(0)}%)</span>
                        </div>
                      </div>
                    </div>
                  );
                })()}
              </div>
            </div>
          </div>

          {/* Leaderboard Table Card */}
          <div className="bg-white rounded-2xl border shadow-sm overflow-hidden" style={{ borderColor: C.border }}>
            <div className="px-5 py-4 border-b flex items-center justify-between" style={{ borderColor: C.border }}>
              <h3 className="text-sm font-semibold text-gray-700 flex items-center gap-1.5">
                <Trophy size={16} style={{ color: C.orange }} />
                Performance Rankings ({filteredRows.length} Strategies)
              </h3>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full border-collapse">
                <thead>
                  <tr>
                    <th className="px-5 py-3.5 text-left text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">Rank</th>
                    <th className="px-5 py-3.5 text-left text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">Strategy</th>
                    <th className="px-5 py-3.5 text-left text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">Symbol</th>
                    <th className="px-5 py-3.5 text-left text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">Mode</th>
                    <th className="px-5 py-3.5 text-left text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">Asset Class</th>
                    <th className="px-5 py-3.5 text-right text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">Trades</th>
                    <th className="px-5 py-3.5 text-right text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">Win Rate</th>
                    <th className="px-5 py-3.5 text-right text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">Avg P&amp;L</th>
                    <th className="px-5 py-3.5 text-right text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">Total Net P&amp;L</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {filteredRows.length === 0 ? (
                    <tr>
                      <td colSpan={9} className="text-center py-10 text-xs font-semibold text-gray-400">
                        No strategies matching the current filters.
                      </td>
                    </tr>
                  ) : (
                    filteredRows.map((r, idx) => {
                      const isTop3 = idx < 3;
                      const badgeStyle = idx === 0 ? { backgroundColor: '#fffbe6', color: '#d97706', borderColor: '#fef3c7' } :
                                         idx === 1 ? { backgroundColor: '#f1f5f9', color: '#475569', borderColor: '#cbd5e1' } :
                                         idx === 2 ? { backgroundColor: '#fff7ed', color: C.orange, borderColor: '#ffedd5' } : {};
                      return (
                        <tr key={idx} className="hover:bg-gray-50 transition-colors">
                          <td className="px-5 py-4 text-xs font-semibold text-gray-600">
                            {isTop3 ? (
                              <span 
                                className="inline-flex items-center justify-center w-6 h-6 rounded-full border text-[10px] font-bold"
                                style={badgeStyle}
                              >
                                {idx + 1}
                              </span>
                            ) : (
                              <span className="pl-2">{idx + 1}</span>
                            )}
                          </td>
                          <td className="px-5 py-4 text-xs font-bold text-gray-700">{r.strategy}</td>
                          <td className="px-5 py-4 text-xs font-semibold text-gray-500">{r.symbol}</td>
                          <td className="px-5 py-4 text-xs">
                            <span 
                              className="inline-block px-2 py-0.5 text-[9px] font-bold rounded uppercase tracking-wider border"
                              style={{
                                backgroundColor: r.mode === "live" ? C.sellBg : C.buyBg,
                                color: r.mode === "live" ? C.sellText : C.buyText,
                                borderColor: C.border,
                              }}
                            >
                              {r.mode}
                            </span>
                          </td>
                          <td className="px-5 py-4 text-xs font-semibold text-gray-500 uppercase tracking-wide">{r.category}</td>
                          <td className="px-5 py-4 text-xs font-semibold text-gray-700 text-right tabular-nums">{r.trades}</td>
                          <td className="px-5 py-4 text-xs font-semibold text-gray-700 text-right tabular-nums">{r.win_rate_pct}%</td>
                          <td className="px-5 py-4 text-xs font-semibold text-right tabular-nums" style={{ color: sign(r.avg_pnl) }}>
                            {withSign(r.avg_pnl)}
                          </td>
                          <td className="px-5 py-4 text-xs font-bold text-right tabular-nums" style={{ color: sign(r.total_pnl) }}>
                            {withSign(r.total_pnl)}
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
