import { useCallback, useEffect, useState } from "react";
import { TrendingUp, TrendingDown, RefreshCw, Layers } from "lucide-react";
import { api, ApiError, OiScannerResponse, OiSignal } from "../api";
import { C, FONT, fmtDate } from "../lib/format";
import SymbolSearchInput from "./SymbolSearchInput";

const SIGNAL_LABELS: Record<OiSignal, string> = {
  LONG_BUILDUP: "Long Build-up",
  SHORT_BUILDUP: "Short Build-up",
  LONG_UNWINDING: "Long Unwinding",
  SHORT_COVERING: "Short Covering",
  NEUTRAL: "Neutral",
};

function signalStyle(signal: OiSignal): { bg: string; fg: string } {
  switch (signal) {
    case "LONG_BUILDUP": return { bg: C.buyBg, fg: C.buyText };
    case "SHORT_COVERING": return { bg: "#eef0fd", fg: "#6b6fd1" };
    case "SHORT_BUILDUP": return { bg: C.sellBg, fg: C.sellText };
    case "LONG_UNWINDING": return { bg: "#fff7ed", fg: C.orange };
    default: return { bg: C.hover, fg: C.muted };
  }
}

function SignalBadge({ signal }: { signal: OiSignal }) {
  const { bg, fg } = signalStyle(signal);
  return (
    <span className="inline-block px-2 py-0.5 text-[10px] font-bold rounded uppercase tracking-wider" style={{ background: bg, color: fg }}>
      {SIGNAL_LABELS[signal]}
    </span>
  );
}

export default function OiScannerView() {
  const [symbolInput, setSymbolInput] = useState("NIFTY");
  const [data, setData] = useState<OiScannerResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const scan = useCallback(async (symbol: string) => {
    if (!symbol.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const result = await api.getOiScanner(symbol.trim().toUpperCase());
      setData(result);
    } catch (err) {
      const detail = err instanceof ApiError && err.detail ? (Array.isArray(err.detail) ? err.detail.join(" ") : err.detail) : null;
      setError(detail || (err instanceof Error ? err.message : "Could not load OI scan data."));
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    scan("NIFTY");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="flex-1 overflow-y-auto p-4 md:p-8 bg-gray-50 min-h-screen" style={FONT}>
      <div className="flex flex-col md:flex-row md:items-center justify-between mb-8 gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-800 flex items-center gap-2">
            <Layers size={24} style={{ color: C.orange }} />
            OI Build-up / Unwinding Scanner
          </h1>
          <p className="text-xs text-gray-500 mt-1 max-w-2xl">
            End-of-day open-interest change classified against price direction (the standard Long/Short Build-up vs
            Unwinding/Covering read). Sourced from the same daily NSE bhavcopy the backtest engine uses — <strong>not
            a live/intraday feed</strong>, so this reflects whichever trading day the bhavcopy sync last ran for.
          </p>
        </div>
      </div>

      <div className="flex items-center gap-2 mb-6">
        <SymbolSearchInput
          value={symbolInput}
          onChange={(s) => { setSymbolInput(s); scan(s); }}
          placeholder="Symbol (e.g. NIFTY)"
          className="flex-1 max-w-xs"
          scope="NSE_FO"
        />
        <button
          onClick={() => scan(symbolInput)}
          disabled={loading}
          className="flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-semibold text-white disabled:opacity-50 hover:opacity-90 focus:outline-none"
          style={{ backgroundColor: C.orange }}
        >
          <RefreshCw size={14} className={loading ? "animate-spin" : ""} /> Scan
        </button>
      </div>

      {error && (
        <div className="p-6 rounded-xl border flex items-center justify-between gap-4 mb-6" style={{ backgroundColor: C.sellBg, borderColor: C.red, color: C.red }}>
          <p className="font-semibold text-sm">{error}</p>
          <button onClick={() => scan(symbolInput)} className="shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold text-white hover:opacity-90" style={{ backgroundColor: C.red }}>
            <RefreshCw size={12} /> Retry
          </button>
        </div>
      )}

      {data && (
        <div className="space-y-6">
          <p className="text-xs text-gray-400">
            As of {fmtDate(data.as_of)}{data.compared_to && <> · compared to {fmtDate(data.compared_to)}</>}
          </p>

          {data.futures && (
            <div className="bg-white p-5 rounded-2xl border shadow-sm" style={{ borderColor: C.border }}>
              <h3 className="text-sm font-semibold text-gray-700 mb-3 flex items-center gap-1.5">
                {data.futures.price_change >= 0 ? <TrendingUp size={16} style={{ color: C.green }} /> : <TrendingDown size={16} style={{ color: C.red }} />}
                Underlying Futures Trend ({data.futures.expiry})
              </h3>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                <div>
                  <div className="text-[10px] text-gray-400 uppercase tracking-wide">Close</div>
                  <div className="text-sm font-bold text-gray-800 tabular-nums">{data.futures.close.toFixed(2)}</div>
                </div>
                <div>
                  <div className="text-[10px] text-gray-400 uppercase tracking-wide">Price Change</div>
                  <div className="text-sm font-bold tabular-nums" style={{ color: data.futures.price_change >= 0 ? C.green : C.red }}>
                    {data.futures.price_change >= 0 ? "+" : ""}{data.futures.price_change.toFixed(2)} ({data.futures.price_change_pct.toFixed(2)}%)
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-gray-400 uppercase tracking-wide">OI Change</div>
                  <div className="text-sm font-bold tabular-nums" style={{ color: data.futures.chg_in_oi >= 0 ? C.blue : C.muted }}>
                    {data.futures.chg_in_oi >= 0 ? "+" : ""}{data.futures.chg_in_oi.toLocaleString("en-IN")}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-gray-400 uppercase tracking-wide mb-1">Signal</div>
                  <SignalBadge signal={data.futures.signal} />
                </div>
              </div>
            </div>
          )}

          <div className="bg-white rounded-2xl border shadow-sm overflow-hidden" style={{ borderColor: C.border }}>
            <div className="px-5 py-4 border-b" style={{ borderColor: C.border }}>
              <h3 className="text-sm font-semibold text-gray-700">Biggest Strike-Level OI Moves ({data.top_strike_moves.length})</h3>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full border-collapse">
                <thead>
                  <tr>
                    <th className="px-5 py-3 text-left text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">Strike</th>
                    <th className="px-5 py-3 text-left text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">Type</th>
                    <th className="px-5 py-3 text-right text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">Close</th>
                    <th className="px-5 py-3 text-right text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">Price Chg</th>
                    <th className="px-5 py-3 text-right text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">OI Change</th>
                    <th className="px-5 py-3 text-left text-[11px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50 border-b border-gray-100">Signal</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {data.top_strike_moves.length === 0 ? (
                    <tr><td colSpan={6} className="text-center py-10 text-xs font-semibold text-gray-400">No comparable data for the nearest expiry.</td></tr>
                  ) : (
                    data.top_strike_moves.map((r, i) => (
                      <tr key={i} className="hover:bg-gray-50 transition-colors">
                        <td className="px-5 py-3 text-xs font-bold text-gray-700 tabular-nums">{r.strike}</td>
                        <td className="px-5 py-3 text-xs font-semibold text-gray-500">{r.option_type}</td>
                        <td className="px-5 py-3 text-xs text-right text-gray-700 tabular-nums">{r.close.toFixed(2)}</td>
                        <td className="px-5 py-3 text-xs text-right font-semibold tabular-nums" style={{ color: r.price_change >= 0 ? C.green : C.red }}>
                          {r.price_change >= 0 ? "+" : ""}{r.price_change.toFixed(2)}
                        </td>
                        <td className="px-5 py-3 text-xs text-right font-semibold tabular-nums text-gray-700">
                          {r.chg_in_oi >= 0 ? "+" : ""}{r.chg_in_oi.toLocaleString("en-IN")}
                        </td>
                        <td className="px-5 py-3"><SignalBadge signal={r.signal} /></td>
                      </tr>
                    ))
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
