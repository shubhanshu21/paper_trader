import { useCallback, useEffect, useMemo, useState } from "react";
import { Search, History, RefreshCw } from "lucide-react";
import { api, ApiError, ChainReplayResponse } from "../api";
import { C, FONT, fmtDate } from "../lib/format";
import { Select } from "./Common";

function fmtNum(n: number | null | undefined): string {
  return n == null ? "—" : n.toLocaleString("en-IN");
}

export default function ChainReplayView() {
  const [symbolInput, setSymbolInput] = useState("NIFTY");
  const [symbol, setSymbol] = useState("NIFTY");
  const [dates, setDates] = useState<string[]>([]);
  const [selectedDate, setSelectedDate] = useState("");
  const [expiries, setExpiries] = useState<string[]>([]);
  const [selectedExpiry, setSelectedExpiry] = useState("");
  const [chain, setChain] = useState<ChainReplayResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const errMsg = (err: unknown, fallback: string) => {
    const detail = err instanceof ApiError && err.detail ? (Array.isArray(err.detail) ? err.detail.join(" ") : err.detail) : null;
    return detail || (err instanceof Error ? err.message : fallback);
  };

  // Step 1: symbol -> available dates
  const loadDates = useCallback(async (sym: string) => {
    setLoading(true);
    setError(null);
    setChain(null);
    setSelectedExpiry("");
    setExpiries([]);
    try {
      const res = await api.getChainReplayDates(sym);
      setDates(res.dates);
      setSelectedDate(res.dates[0] || "");
      if (!res.dates.length) setError(`No bhavcopy history available for ${sym}.`);
    } catch (err) {
      setError(errMsg(err, "Could not load available dates."));
      setDates([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadDates("NIFTY"); }, [loadDates]);

  // Step 2: date -> available expiries
  useEffect(() => {
    if (!selectedDate) return;
    let cancelled = false;
    (async () => {
      setError(null);
      try {
        const res = await api.getChainReplayExpiries(symbol, selectedDate);
        if (cancelled) return;
        setExpiries(res.expiries);
        setSelectedExpiry(res.expiries[0] || "");
      } catch (err) {
        if (!cancelled) setError(errMsg(err, "Could not load expiries for this date."));
      }
    })();
    return () => { cancelled = true; };
  }, [symbol, selectedDate]);

  // Step 3: expiry -> the chain itself
  useEffect(() => {
    if (!selectedDate || !selectedExpiry) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await api.getChainReplay(symbol, selectedDate, selectedExpiry);
        if (!cancelled) setChain(res);
      } catch (err) {
        if (!cancelled) setError(errMsg(err, "Could not load the chain."));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [symbol, selectedDate, selectedExpiry]);

  const atmStrike = useMemo(() => {
    if (!chain || chain.underlying_close == null || chain.chain.length === 0) return null;
    return chain.chain.reduce((closest, r) => (Math.abs(r.strike - chain.underlying_close!) < Math.abs(closest - chain.underlying_close!) ? r.strike : closest), chain.chain[0].strike);
  }, [chain]);

  return (
    <div className="flex-1 overflow-y-auto p-4 md:p-8 bg-gray-50 min-h-screen" style={FONT}>
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-gray-800 flex items-center gap-2">
          <History size={24} style={{ color: C.orange }} />
          Option Chain Replay
        </h1>
        <p className="text-xs text-gray-500 mt-1">
          Scrub to any past trading day and expiry to see the full CE/PE chain (close, OI, volume) as EOD bhavcopy recorded it.
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-3 mb-6">
        <div>
          <label className="block text-[11px] font-semibold text-gray-500 mb-1.5">Symbol</label>
          <div className="relative">
            <Search size={13} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
            <input
              value={symbolInput}
              onChange={(e) => setSymbolInput(e.target.value.toUpperCase())}
              onKeyDown={(e) => { if (e.key === "Enter") { setSymbol(symbolInput); loadDates(symbolInput); } }}
              className="pl-8 pr-3 py-2 border rounded-lg text-sm w-40 focus:outline-none focus:ring-2 focus:ring-orange-500"
              style={{ borderColor: C.border2 }}
            />
          </div>
        </div>
        <button
          onClick={() => { setSymbol(symbolInput); loadDates(symbolInput); }}
          className="px-4 py-2 rounded-lg text-sm font-semibold text-white hover:opacity-90 focus:outline-none"
          style={{ backgroundColor: C.orange }}
        >
          Load
        </button>
        {dates.length > 0 && (
          <div>
            <label className="block text-[11px] font-semibold text-gray-500 mb-1.5">Trade Date</label>
            <Select value={selectedDate} onChange={setSelectedDate} options={dates.map((d) => ({ value: d, label: fmtDate(d) }))} className="w-40" />
          </div>
        )}
        {expiries.length > 0 && (
          <div>
            <label className="block text-[11px] font-semibold text-gray-500 mb-1.5">Expiry</label>
            <Select value={selectedExpiry} onChange={setSelectedExpiry} options={expiries.map((e) => ({ value: e, label: fmtDate(e) }))} className="w-40" />
          </div>
        )}
        {loading && <RefreshCw size={16} className="animate-spin mb-2" style={{ color: C.orange }} />}
      </div>

      {error && (
        <div className="p-4 rounded-xl border text-sm font-semibold mb-6" style={{ backgroundColor: C.sellBg, borderColor: C.red, color: C.red }}>
          {error}
        </div>
      )}

      {chain && (
        <div className="bg-white rounded-2xl border shadow-sm overflow-hidden" style={{ borderColor: C.border }}>
          <div className="px-5 py-4 border-b flex items-center justify-between" style={{ borderColor: C.border }}>
            <h3 className="text-sm font-semibold text-gray-700">
              {chain.symbol} · {fmtDate(chain.date)} · Expiry {fmtDate(chain.expiry)}
            </h3>
            {chain.underlying_close != null && (
              <span className="text-xs font-semibold text-gray-500">Futures close: <span className="text-gray-800">{chain.underlying_close.toFixed(2)}</span></span>
            )}
          </div>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-xs">
              <thead>
                <tr>
                  <th colSpan={4} className="px-3 py-2 text-center font-bold uppercase tracking-wide bg-green-50 text-green-700 border-b border-gray-100">Calls</th>
                  <th className="px-3 py-2 text-center font-bold uppercase tracking-wide bg-gray-100 text-gray-600 border-b border-gray-100">Strike</th>
                  <th colSpan={4} className="px-3 py-2 text-center font-bold uppercase tracking-wide bg-red-50 text-red-700 border-b border-gray-100">Puts</th>
                </tr>
                <tr className="text-[10px] uppercase tracking-wide text-gray-400 bg-gray-50">
                  <th className="px-3 py-2 text-right font-medium border-b border-gray-100">OI</th>
                  <th className="px-3 py-2 text-right font-medium border-b border-gray-100">Chg OI</th>
                  <th className="px-3 py-2 text-right font-medium border-b border-gray-100">Vol</th>
                  <th className="px-3 py-2 text-right font-medium border-b border-gray-100">Close</th>
                  <th className="px-3 py-2 text-center font-medium border-b border-gray-100"> </th>
                  <th className="px-3 py-2 text-left font-medium border-b border-gray-100">Close</th>
                  <th className="px-3 py-2 text-left font-medium border-b border-gray-100">Vol</th>
                  <th className="px-3 py-2 text-left font-medium border-b border-gray-100">Chg OI</th>
                  <th className="px-3 py-2 text-left font-medium border-b border-gray-100">OI</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {chain.chain.map((row) => {
                  const isAtm = row.strike === atmStrike;
                  return (
                    <tr key={row.strike} className="hover:bg-gray-50" style={isAtm ? { background: "#fff7ed" } : undefined}>
                      <td className="px-3 py-1.5 text-right tabular-nums text-gray-600">{fmtNum(row.ce?.open_interest)}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums" style={{ color: (row.ce?.chg_in_oi ?? 0) >= 0 ? C.green : C.red }}>{fmtNum(row.ce?.chg_in_oi)}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums text-gray-600">{fmtNum(row.ce?.volume)}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums font-semibold text-gray-800">{row.ce?.close != null ? row.ce.close.toFixed(2) : "—"}</td>
                      <td className="px-3 py-1.5 text-center font-bold tabular-nums" style={isAtm ? { color: C.orange } : { color: C.text }}>{row.strike}</td>
                      <td className="px-3 py-1.5 text-left tabular-nums font-semibold text-gray-800">{row.pe?.close != null ? row.pe.close.toFixed(2) : "—"}</td>
                      <td className="px-3 py-1.5 text-left tabular-nums text-gray-600">{fmtNum(row.pe?.volume)}</td>
                      <td className="px-3 py-1.5 text-left tabular-nums" style={{ color: (row.pe?.chg_in_oi ?? 0) >= 0 ? C.green : C.red }}>{fmtNum(row.pe?.chg_in_oi)}</td>
                      <td className="px-3 py-1.5 text-left tabular-nums text-gray-600">{fmtNum(row.pe?.open_interest)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
