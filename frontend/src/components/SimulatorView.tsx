import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, History, RefreshCw, Trash2, Target, Play, Pause, ChevronLeft, ChevronRight } from "lucide-react";
import {
  api, ApiError, SimChainRow, SimulatorEvaluateResponse, ChainReplayRow, ReplayEvaluateResponse,
  SimLegInput, ExpiriesResponse,
} from "../api";
import { C, FONT, fmtDate, inr, withSign, sign } from "../lib/format";
import SymbolSearchInput from "./SymbolSearchInput";
import { Select } from "./Common";

type Mode = "live" | "eod";
type RightTab = "payoff" | "greeks";

// A leg the user has added to their simulated position. `lots` (not raw
// contract quantity) is what the UI shows/edits — converted to a real
// quantity (lots * lot_size) only when calling the backend, same
// convention every strategy engine in this app uses. `entry_ltp` is the
// price the chain showed at the moment this leg was added — sent to the
// backend as a fallback if a FRESH quote isn't available at evaluate time
// (common for a thin/deep-OTM contract, especially after market close),
// so one stale contract doesn't block evaluating the whole position.
interface StagedLeg extends Omit<SimLegInput, "quantity"> {
  lots: number;
  instrument_key?: string;
  entry_ltp?: number | null;
}

function dte(dateStr: string): number {
  const ms = new Date(dateStr).getTime() - Date.now();
  return Math.max(0, Math.ceil(ms / 86400000));
}

function PayoffChart({ curve, markerPrice, mtm }: { curve: { price: number; pnl: number }[]; markerPrice?: number | null; mtm?: number | null }) {
  if (curve.length < 2) {
    return <div className="flex items-center justify-center h-64 text-xs text-gray-400 border rounded-xl" style={{ borderColor: C.border2 }}>Add legs and evaluate to see the payoff diagram.</div>;
  }
  const width = 720, height = 280, padding = 40;
  const minPrice = curve[0].price, maxPrice = curve[curve.length - 1].price;
  const minPnl = Math.min(0, ...curve.map((p) => p.pnl));
  const maxPnl = Math.max(0, ...curve.map((p) => p.pnl));
  const xScale = (price: number) => padding + ((price - minPrice) / (maxPrice - minPrice || 1)) * (width - 2 * padding);
  const yScale = (pnl: number) => height - padding - ((pnl - minPnl) / (maxPnl - minPnl || 1)) * (height - 2 * padding);
  const zeroY = yScale(0);
  const points = curve.map((p) => `${xScale(p.price)},${yScale(p.pnl)}`).join(" ");
  const profitPath = curve.map((p) => `${xScale(p.price)},${yScale(Math.max(p.pnl, 0))}`).join(" ");
  const lossPath = curve.map((p) => `${xScale(p.price)},${yScale(Math.min(p.pnl, 0))}`).join(" ");

  return (
    <div className="relative">
      {(markerPrice != null || mtm != null) && (
        <div className="absolute top-0 right-0 text-[11px] font-semibold text-gray-500 flex items-center gap-3">
          {mtm != null && <span>MTM: <span style={{ color: sign(mtm) }}>{withSign(mtm)}</span></span>}
          {markerPrice != null && <span>Spot: <span className="text-gray-800">{inr(markerPrice)}</span></span>}
        </div>
      )}
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto mt-5">
        <line x1={padding} y1={zeroY} x2={width - padding} y2={zeroY} stroke={C.border2} strokeWidth={1} />
        <polygon points={`${padding},${zeroY} ${profitPath} ${width - padding},${zeroY}`} fill={C.green} opacity={0.15} />
        <polygon points={`${padding},${zeroY} ${lossPath} ${width - padding},${zeroY}`} fill={C.red} opacity={0.15} />
        <polyline points={points} fill="none" stroke={C.blue} strokeWidth={2} />
        {markerPrice != null && markerPrice >= minPrice && markerPrice <= maxPrice && (
          <line x1={xScale(markerPrice)} y1={padding - 10} x2={xScale(markerPrice)} y2={height - padding} stroke={C.orange} strokeWidth={1} strokeDasharray="4,3" />
        )}
        <text x={padding} y={height - 10} fontSize={10} fill={C.muted}>{minPrice.toFixed(0)}</text>
        <text x={xScale((minPrice + maxPrice) / 2)} y={height - 10} fontSize={10} fill={C.muted} textAnchor="middle">{((minPrice + maxPrice) / 2).toFixed(0)}</text>
        <text x={width - padding} y={height - 10} fontSize={10} fill={C.muted} textAnchor="end">{maxPrice.toFixed(0)}</text>
      </svg>
    </div>
  );
}

function Metric({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <div className="text-[10px] text-gray-400 uppercase tracking-wide">{label}</div>
      <div className="text-sm font-bold tabular-nums" style={{ color: color || C.text }}>{value}</div>
    </div>
  );
}

export default function SimulatorView() {
  const [mode, setMode] = useState<Mode>("live");
  const [rightTab, setRightTab] = useState<RightTab>("payoff");
  const [multiplyByLots, setMultiplyByLots] = useState(true);

  const [symbol, setSymbol] = useState("NIFTY");
  const [expiries, setExpiries] = useState<ExpiriesResponse["expiries"]>([]);
  const [expiry, setExpiry] = useState("");
  const [legs, setLegs] = useState<StagedLeg[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loadingChain, setLoadingChain] = useState(false);
  const atmRowRef = useRef<HTMLTableRowElement>(null);

  // Live-mode state
  const [liveChain, setLiveChain] = useState<SimChainRow[]>([]);
  const [spotPrice, setSpotPrice] = useState<number | null>(null);
  const [lotSize, setLotSize] = useState<number | null>(null);
  const [liveResult, setLiveResult] = useState<SimulatorEvaluateResponse | null>(null);
  const [evaluating, setEvaluating] = useState(false);

  // EOD-replay-mode state
  const [availableDates, setAvailableDates] = useState<string[]>([]);
  const [entryDate, setEntryDate] = useState("");
  const [evalDate, setEvalDate] = useState("");
  const [eodChain, setEodChain] = useState<ChainReplayRow[]>([]);
  const [eodUnderlying, setEodUnderlying] = useState<number | null>(null);
  const [replayResult, setReplayResult] = useState<ReplayEvaluateResponse | null>(null);

  const errMsg = (err: unknown, fallback: string) => {
    const detail = err instanceof ApiError && err.detail ? (Array.isArray(err.detail) ? err.detail.join(" ") : err.detail) : null;
    return detail || (err instanceof Error ? err.message : fallback);
  };

  const resetPosition = () => { setLegs([]); setLiveResult(null); setReplayResult(null); };

  // Expiries (live: real upcoming; eod: derived from entryDate below)
  useEffect(() => {
    if (mode !== "live") return;
    api.getCustomStrategyTemplateExpiries(symbol).then((res) => {
      setExpiries(res.expiries);
      if (res.expiries[0]) setExpiry(res.expiries[0].date);
    }).catch(() => setExpiries([]));
  }, [symbol, mode]);

  const loadLiveChain = useCallback(async () => {
    if (!symbol || !expiry) return;
    setLoadingChain(true);
    setError(null);
    try {
      const res = await api.getSimulatorChain(symbol, expiry);
      setLiveChain(res.chain);
      setSpotPrice(res.spot_price);
      setLotSize(res.lot_size);
    } catch (err) {
      setError(errMsg(err, "Could not load the live chain."));
    } finally {
      setLoadingChain(false);
    }
  }, [symbol, expiry]);

  useEffect(() => { if (mode === "live") loadLiveChain(); }, [mode, loadLiveChain]);

  // EOD: load available dates for the symbol, default entryDate. evalDate
  // starts EQUAL to entryDate (not the latest available date) — a freshly
  // built position should show "as of entry" (MTM = 0) first, then the
  // user scrubs forward from there via Next Day. Previously this defaulted
  // evalDate to the latest date independently of entryDate, which both
  // silently evaluated the position 7+ days forward before the user asked
  // for that AND broke the scrub-range calculation (see scrubDates below).
  useEffect(() => {
    if (mode !== "eod") return;
    api.getChainReplayDates(symbol).then((res) => {
      setAvailableDates(res.dates);
      const entry = res.dates[5] || res.dates[0]; // a few days back by default, so there's room to scrub forward
      if (entry) {
        setEntryDate(entry);
        setEvalDate(entry);
      }
    }).catch(() => setAvailableDates([]));
  }, [symbol, mode]);

  useEffect(() => {
    if (mode !== "eod" || !symbol || !entryDate) return;
    api.getChainReplayExpiries(symbol, entryDate).then((res) => {
      setExpiries(res.expiries.map((e) => ({ date: e, label: "Weekly" })));
      if (res.expiries[0]) setExpiry(res.expiries[0]);
    }).catch(() => setExpiries([]));
  }, [symbol, entryDate, mode]);

  const loadEodChain = useCallback(async () => {
    if (!symbol || !expiry || !entryDate) return;
    setLoadingChain(true);
    setError(null);
    try {
      const res = await api.getChainReplay(symbol, entryDate, expiry);
      setEodChain(res.chain);
      setEodUnderlying(res.underlying_close);
      setLotSize(res.lot_size);
    } catch (err) {
      setError(errMsg(err, "Could not load the historical chain."));
    } finally {
      setLoadingChain(false);
    }
  }, [symbol, expiry, entryDate]);

  useEffect(() => { if (mode === "eod") loadEodChain(); }, [mode, loadEodChain]);

  // Click B/S on any strike to add that leg immediately — the position
  // panel already holds every leg added this way, so building a multi-leg
  // strategy is just clicking B/S across several strikes in turn (same
  // interaction Sensibull/Opstra/AlgoTest's own chains use — no separate
  // "select then batch-add" step).
  const addLeg = (strike: number, optionType: "CE" | "PE", action: "BUY" | "SELL", instrumentKey?: string, ltp?: number | null) => {
    setLegs((prev) => [...prev, { strike, option_type: optionType, action, lots: 1, instrument_key: instrumentKey, entry_ltp: ltp ?? null }]);
    setLiveResult(null);
    setReplayResult(null);
  };
  const removeLeg = (idx: number) => {
    setLegs((prev) => prev.filter((_, i) => i !== idx));
    setLiveResult(null);
    setReplayResult(null);
  };
  const setLots = (idx: number, lots: number) => {
    setLegs((prev) => prev.map((l, i) => (i === idx ? { ...l, lots: Math.max(1, lots) } : l)));
    setLiveResult(null);
    setReplayResult(null);
  };

  const effectiveLotSize = lotSize || 1;

  const evaluateLive = useCallback(async () => {
    if (legs.length === 0) return;
    setEvaluating(true);
    setError(null);
    try {
      const res = await api.evaluateSimulator({
        symbol, expiry, instrument_type: "INDEX",
        legs: legs.map((l) => ({ instrument_key: l.instrument_key!, strike: l.strike, option_type: l.option_type, action: l.action, quantity: l.lots * effectiveLotSize, fallback_premium: l.entry_ltp ?? null })),
      });
      setLiveResult(res);
    } catch (err) {
      setError(errMsg(err, "Could not evaluate this position."));
    } finally {
      setEvaluating(false);
    }
  }, [symbol, expiry, legs, effectiveLotSize]);

  const evaluateReplay = useCallback(async (targetEvalDate: string) => {
    if (legs.length === 0 || !targetEvalDate) return;
    setEvaluating(true);
    setError(null);
    try {
      const res = await api.evaluateChainReplay({
        symbol, expiry, entry_date: entryDate, eval_date: targetEvalDate,
        legs: legs.map((l) => ({ strike: l.strike, option_type: l.option_type, action: l.action, quantity: l.lots * effectiveLotSize })),
      });
      setReplayResult(res);
      setRightTab("payoff");
    } catch (err) {
      setError(errMsg(err, "Could not evaluate this position on that date."));
    } finally {
      setEvaluating(false);
    }
  }, [symbol, expiry, entryDate, legs, effectiveLotSize]);

  // evalDate kept in a ref (not a dependency below) so the leg/lot-change
  // effect always reads the CURRENT scrub position without re-firing every
  // time the date itself changes — date changes are already evaluated
  // directly by stepDate/scrubToIndex/autoplay. Including evalDate in the
  // deps array previously caused a duplicate evaluateReplay call ~400ms
  // after every single scrub step (one from stepDate itself, one from this
  // debounce reacting to the resulting evalDate change) — doubling network
  // load, worst during autoplay where every step compounded it.
  const evalDateRef = useRef(evalDate);
  useEffect(() => { evalDateRef.current = evalDate; }, [evalDate]);

  // Auto-evaluate on every leg/lot change (debounced) — matches how
  // AlgoTest's own simulator panel and OptionAlpha's payoff diagrams work
  // ("constantly update to reflect real-time changes"), rather than
  // requiring an explicit click every time a leg is added or a lot count
  // is tweaked. The manual buttons below stay as an explicit "refresh
  // right now" (e.g. re-pull live prices without changing the position).
  useEffect(() => {
    if (legs.length === 0) return;
    // liveResult/replayResult deliberately excluded from deps — they're
    // only read here to decide the FIRST-evaluation tab jump, and each
    // evaluate call sets one of them, so including them would re-fire this
    // effect (and re-evaluate) every time evaluation completes.
    if (mode === "live" && !liveResult) setRightTab("payoff");
    if (mode === "eod" && !replayResult) setRightTab("payoff");
    const timer = setTimeout(() => {
      if (mode === "live") evaluateLive();
      else evaluateReplay(evalDateRef.current || entryDate);
    }, 400);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [legs, mode, entryDate, evaluateLive, evaluateReplay]);

  // Every available date from entryDate onward (ascending) — the actual
  // scrubbable range. Previously this filtered by `d <= evalDate`, which —
  // since evalDate defaulted to the LATEST available date independently of
  // entryDate — trivially matched the entire ~180-day history on load
  // (misreporting e.g. "day 180 of 180" for a position entered a week ago).
  const scrubDates = useMemo(() => availableDates.filter((d) => d >= entryDate).slice().reverse(), [availableDates, entryDate]);
  const evalDateIndex = scrubDates.indexOf(evalDate);

  const stepDate = (dir: 1 | -1) => {
    const next = evalDateIndex + dir;
    if (next >= 0 && next < scrubDates.length) {
      const d = scrubDates[next];
      setEvalDate(d);
      evaluateReplay(d);
    }
  };

  // Autoplay — step forward one day at a time on a timer, same "play through
  // the days" control AlgoTest's own replay scrubber has. Stops itself at
  // the last available day, or if the user pauses/switches mode/leaves EOD.
  const [autoplay, setAutoplay] = useState(false);
  const [autoplaySpeedMs, setAutoplaySpeedMs] = useState(1000);
  useEffect(() => {
    if (!autoplay || mode !== "eod") return;
    if (evalDateIndex >= scrubDates.length - 1) { setAutoplay(false); return; }
    // Wait for any in-flight evaluate to finish before scheduling the next
    // step — without this, a slow network response can arrive AFTER a
    // later step's response and stomp it, silently showing a stale day's
    // MTM. This effect re-runs the instant `evaluating` flips back to
    // false (it's a dependency below), so the real step cadence is
    // max(autoplaySpeedMs, actual evaluate latency), not a fixed timer.
    if (evaluating) return;
    const timer = setTimeout(() => stepDate(1), autoplaySpeedMs);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoplay, mode, evalDateIndex, scrubDates.length, autoplaySpeedMs, evaluating]);
  useEffect(() => { if (mode !== "eod") setAutoplay(false); }, [mode]);

  const scrubToIndex = (idx: number) => {
    const d = scrubDates[idx];
    if (!d) return;
    setAutoplay(false);
    setEvalDate(d);
    evaluateReplay(d);
  };

  const chainRows = mode === "live" ? liveChain : eodChain;
  const activeResult = mode === "live" ? liveResult : replayResult;
  const activeMarkerPrice = mode === "live" ? spotPrice : (replayResult?.eval_underlying_close ?? eodUnderlying);
  const lotMultiplier = multiplyByLots ? effectiveLotSize : 1;

  const goToAtm = () => atmRowRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });

  return (
    <div className="flex-1 overflow-y-auto p-4 md:p-6 bg-gray-50 min-h-screen" style={FONT}>
      <div className="flex flex-col md:flex-row md:items-center justify-between mb-4 gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-800 flex items-center gap-2">
            {mode === "live" ? <Activity size={24} style={{ color: C.orange }} /> : <History size={24} style={{ color: C.orange }} />}
            Options Simulator
          </h1>
          <p className="text-xs text-gray-500 mt-1 max-w-2xl">
            {mode === "live"
              ? "Build a position from the real live option chain and see live payoff/Greeks/margin/POP — never places an order."
              : "Build a position from a past date's real EOD chain, then scrub day-by-day through bhavcopy history to see how MTM evolved. No intraday minute-level data exists in this system — day granularity only."}
          </p>
        </div>
        <div className="flex rounded-lg overflow-hidden border shrink-0" style={{ borderColor: C.border2 }}>
          <button onClick={() => { setMode("live"); resetPosition(); }} className={`px-4 py-2 text-xs font-semibold ${mode === "live" ? "text-white" : "text-gray-600 bg-white"}`} style={mode === "live" ? { backgroundColor: C.orange } : undefined}>Live</button>
          <button onClick={() => { setMode("eod"); resetPosition(); }} className={`px-4 py-2 text-xs font-semibold ${mode === "eod" ? "text-white" : "text-gray-600 bg-white"}`} style={mode === "eod" ? { backgroundColor: C.orange } : undefined}>EOD Replay</button>
        </div>
      </div>

      {error && (
        <div className="p-4 rounded-xl border text-sm font-semibold mb-4" style={{ backgroundColor: C.sellBg, borderColor: C.red, color: C.red }}>{error}</div>
      )}

      {/* Symbol / spot header bar */}
      <div className="bg-white rounded-2xl border shadow-sm px-5 py-3 mb-4 flex flex-wrap items-center gap-4" style={{ borderColor: C.border }}>
        <SymbolSearchInput value={symbol} onChange={(s) => { setSymbol(s); resetPosition(); }} className="w-40" scope={mode === "eod" ? "NSE_FO" : "ALL"} />
        {mode === "live" && lotSize != null && <span className="text-xs font-semibold text-gray-500">Lot size: <span className="text-gray-800">{lotSize}</span></span>}
        {mode === "live" && spotPrice != null && <span className="text-xs font-semibold text-gray-500">Spot: <span className="text-gray-800">{spotPrice.toFixed(2)}</span></span>}
        {mode === "eod" && (
          <>
            <div>
              <label className="text-[10px] font-semibold text-gray-400 uppercase mr-1.5">Entry Date</label>
              <select value={entryDate} onChange={(e) => { setEntryDate(e.target.value); resetPosition(); }} className="text-xs font-semibold border rounded-md px-2 py-1" style={{ borderColor: C.border2 }}>
                {availableDates.map((d) => <option key={d} value={d}>{fmtDate(d)}</option>)}
              </select>
            </div>
            {eodUnderlying != null && <span className="text-xs font-semibold text-gray-500">Entry Spot: <span className="text-gray-800">{eodUnderlying.toFixed(2)}</span></span>}
          </>
        )}
        <button onClick={() => (mode === "live" ? loadLiveChain() : loadEodChain())} disabled={loadingChain} className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg bg-gray-50 border hover:bg-gray-100 disabled:opacity-50 ml-auto" style={{ borderColor: C.border2 }}>
          <RefreshCw size={12} className={loadingChain ? "animate-spin" : ""} /> Refresh
        </button>
      </div>

      {/* Expiry dropdown */}
      {expiries.length > 0 && (
        <div className="mb-4 w-56">
          <Select
            value={expiry}
            onChange={(v) => { setExpiry(v); resetPosition(); }}
            options={expiries.map((e) => ({ value: e.date, label: `${fmtDate(e.date)} (${dte(e.date)}d)` }))}
          />
        </div>
      )}

      {/* EOD scrub bar — day-granularity only (this system has no intraday
          historical archive, just once-daily EOD bhavcopy closes), so
          unlike a minute-level replay slider, one slider step = one day. */}
      {mode === "eod" && legs.length > 0 && replayResult && (
        <div className="bg-white rounded-2xl border shadow-sm px-5 py-3 mb-4" style={{ borderColor: C.border }}>
          <div className="flex items-center justify-between text-[10px] text-gray-400 mb-1 px-1">
            <span>{fmtDate(scrubDates[0])} (entry {fmtDate(replayResult.entry_date)})</span>
            <span className="text-xs font-bold text-gray-800">{fmtDate(evalDate)} &middot; day {evalDateIndex + 1} of {scrubDates.length}</span>
            <span>{fmtDate(scrubDates[scrubDates.length - 1])}</span>
          </div>
          <input
            type="range" min={0} max={Math.max(0, scrubDates.length - 1)} value={Math.max(0, evalDateIndex)}
            onChange={(e) => scrubToIndex(parseInt(e.target.value))}
            className="w-full accent-orange-500 mb-2"
            style={{ accentColor: C.orange }}
          />
          <div className="flex items-center gap-2">
            <button
              onClick={() => setAutoplay((a) => !a)}
              disabled={!autoplay && evalDateIndex >= scrubDates.length - 1}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold text-white disabled:opacity-40"
              style={{ backgroundColor: C.orange }}
            >
              {autoplay ? <Pause size={12} /> : <Play size={12} />} {autoplay ? "Pause" : "Autoplay"}
            </button>
            <button onClick={() => stepDate(-1)} disabled={evalDateIndex <= 0} className="p-1.5 rounded-lg bg-gray-100 disabled:opacity-30"><ChevronLeft size={14} /></button>
            <button onClick={() => stepDate(1)} disabled={evalDateIndex >= scrubDates.length - 1} className="p-1.5 rounded-lg bg-gray-100 disabled:opacity-30"><ChevronRight size={14} /></button>
            <select
              value={autoplaySpeedMs}
              onChange={(e) => setAutoplaySpeedMs(parseInt(e.target.value))}
              className="ml-auto text-[11px] font-semibold border rounded-md px-2 py-1"
              style={{ borderColor: C.border2 }}
            >
              <option value={2000}>1 day / 2s</option>
              <option value={1000}>1 day / 1s</option>
              <option value={400}>1 day / 0.4s</option>
            </select>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
        {/* LEFT: Option Chain */}
        <div className="lg:col-span-3 bg-white rounded-2xl border shadow-sm overflow-hidden" style={{ borderColor: C.border }}>
          <div className="flex items-center justify-between border-b px-4 py-2.5" style={{ borderColor: C.border }}>
            <span className="text-xs font-semibold text-gray-700">Option Chain</span>
            <button onClick={goToAtm} className="flex items-center gap-1 text-[11px] font-semibold" style={{ color: C.blue }}>
              <Target size={12} /> Go to ATM
            </button>
          </div>

          {(
            <div className="overflow-x-auto max-h-[560px] overflow-y-auto">
              <table className="w-full border-collapse text-xs">
                <thead className="sticky top-0 bg-white z-10">
                  <tr>
                    <th className="px-2 py-2 text-right font-medium text-gray-400 border-b" style={{ borderColor: C.border2 }}>Delta</th>
                    <th className="px-2 py-2 text-right font-medium text-gray-400 border-b" style={{ borderColor: C.border2 }}>Call LTP</th>
                    <th className="px-2 py-2 text-right font-medium text-gray-400 border-b" style={{ borderColor: C.border2 }}>IV</th>
                    <th className="px-2 py-2 text-center font-medium text-gray-400 border-b bg-gray-50" style={{ borderColor: C.border2 }}>Strike</th>
                    <th className="px-2 py-2 text-left font-medium text-gray-400 border-b" style={{ borderColor: C.border2 }}>IV</th>
                    <th className="px-2 py-2 text-left font-medium text-gray-400 border-b" style={{ borderColor: C.border2 }}>Put LTP</th>
                    <th className="px-2 py-2 text-left font-medium text-gray-400 border-b" style={{ borderColor: C.border2 }}>Delta</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {chainRows.map((row) => {
                    const ceLtp = mode === "live" ? (row as SimChainRow).ce?.ltp : (row as ChainReplayRow).ce?.close;
                    const peLtp = mode === "live" ? (row as SimChainRow).pe?.ltp : (row as ChainReplayRow).pe?.close;
                    const ceIv = mode === "live" ? (row as SimChainRow).ce?.iv : null;
                    const peIv = mode === "live" ? (row as SimChainRow).pe?.iv : null;
                    const ceDelta = mode === "live" ? (row as SimChainRow).ce?.delta : null;
                    const peDelta = mode === "live" ? (row as SimChainRow).pe?.delta : null;
                    const ceKey = mode === "live" ? (row as SimChainRow).ce?.instrument_key : undefined;
                    const peKey = mode === "live" ? (row as SimChainRow).pe?.instrument_key : undefined;
                    const isAtm = activeMarkerPrice != null && Math.abs(row.strike - activeMarkerPrice) < (effectiveLotSize ? 30 : 30);
                    return (
                      <tr key={row.strike} ref={isAtm ? atmRowRef : undefined} style={isAtm ? { background: "#fff7ed" } : undefined}>
                        <td className="px-2 py-1.5 text-right text-gray-400 tabular-nums">{ceDelta != null ? ceDelta.toFixed(2) : "—"}</td>
                        <td className="px-2 py-1.5 text-right tabular-nums">
                          {ceLtp ? (
                            <div className="flex items-center justify-end gap-1">
                              <button onClick={() => addLeg(row.strike, "CE", "SELL", ceKey, ceLtp)} className="px-1.5 py-0.5 rounded text-[10px] font-bold" style={{ background: C.sellBg, color: C.sellText }}>S</button>
                              <button onClick={() => addLeg(row.strike, "CE", "BUY", ceKey, ceLtp)} className="px-1.5 py-0.5 rounded text-[10px] font-bold" style={{ background: C.buyBg, color: C.buyText }}>B</button>
                              <span className="font-semibold text-gray-700 w-14 text-right">{ceLtp.toFixed(2)}</span>
                            </div>
                          ) : ceLtp === 0 ? (
                            <span className="text-gray-300" title="No live/last-traded price for this contract right now">0.00 —</span>
                          ) : <span className="text-gray-300">—</span>}
                        </td>
                        <td className="px-2 py-1.5 text-right text-gray-400 tabular-nums">{ceIv != null ? ceIv.toFixed(1) : "—"}</td>
                        <td className="px-2 py-1.5 text-center font-bold tabular-nums bg-gray-50" style={isAtm ? { color: C.orange } : { color: C.text }}>{row.strike}</td>
                        <td className="px-2 py-1.5 text-left text-gray-400 tabular-nums">{peIv != null ? peIv.toFixed(1) : "—"}</td>
                        <td className="px-2 py-1.5 text-left tabular-nums">
                          {peLtp ? (
                            <div className="flex items-center gap-1">
                              <span className="font-semibold text-gray-700 w-14">{peLtp.toFixed(2)}</span>
                              <button onClick={() => addLeg(row.strike, "PE", "SELL", peKey, peLtp)} className="px-1.5 py-0.5 rounded text-[10px] font-bold" style={{ background: C.sellBg, color: C.sellText }}>S</button>
                              <button onClick={() => addLeg(row.strike, "PE", "BUY", peKey, peLtp)} className="px-1.5 py-0.5 rounded text-[10px] font-bold" style={{ background: C.buyBg, color: C.buyText }}>B</button>
                            </div>
                          ) : peLtp === 0 ? (
                            <span className="text-gray-300" title="No live/last-traded price for this contract right now">— 0.00</span>
                          ) : <span className="text-gray-300">—</span>}
                        </td>
                        <td className="px-2 py-1.5 text-left text-gray-400 tabular-nums">{peDelta != null ? peDelta.toFixed(2) : "—"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* RIGHT: Payoff / Greeks */}
        <div className="lg:col-span-2 bg-white rounded-2xl border shadow-sm overflow-hidden" style={{ borderColor: C.border }}>
          <div className="flex border-b" style={{ borderColor: C.border }}>
            {(["payoff", "greeks"] as RightTab[]).map((t) => (
              <button
                key={t}
                onClick={() => setRightTab(t)}
                className="px-4 py-2.5 text-xs font-semibold border-b-2 -mb-px capitalize"
                style={rightTab === t ? { borderColor: C.orange, color: C.orange } : { borderColor: "transparent", color: C.muted }}
              >
                {t}
              </button>
            ))}
          </div>

          <div className="p-4">
            {!activeResult ? (
              <p className="text-xs text-gray-400 py-8 text-center">Add legs and evaluate to see {rightTab === "payoff" ? "the payoff diagram" : "Greeks"}.</p>
            ) : rightTab === "payoff" ? (
              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-3">
                  <Metric label={mode === "eod" ? "Current MTM" : "Net Premium"} value={inr(mode === "eod" ? (replayResult?.mtm ?? 0) : (liveResult?.net_premium ?? 0))} color={sign(mode === "eod" ? (replayResult?.mtm ?? 0) : (liveResult?.net_premium ?? 0))} />
                  <Metric label="Max Profit" value={activeResult.max_profit != null ? withSign(activeResult.max_profit) : "Unlimited"} color={C.green} />
                  <Metric label="Max Loss" value={activeResult.max_loss != null ? withSign(activeResult.max_loss) : "Unlimited"} color={C.red} />
                  <Metric label="Breakeven" value={activeResult.breakevens.length ? activeResult.breakevens.map((b) => b.toFixed(0)).join(", ") : "—"} />
                  {mode === "live" && liveResult && (
                    <>
                      <Metric label="Margin Approx" value={liveResult.margin_required != null ? inr(liveResult.margin_required) : "—"} />
                      <Metric label="POP" value={liveResult.probability_of_profit_pct != null ? `${liveResult.probability_of_profit_pct}%` : "N/A"} />
                    </>
                  )}
                  {mode === "eod" && replayResult && (
                    <>
                      <Metric label="Entry Spot" value={replayResult.entry_underlying_close != null ? replayResult.entry_underlying_close.toFixed(2) : "—"} />
                      <Metric label="Eval Spot" value={replayResult.eval_underlying_close != null ? replayResult.eval_underlying_close.toFixed(2) : "—"} />
                    </>
                  )}
                </div>
                {mode === "live" && liveResult && !liveResult.margin_is_real && liveResult.margin_required != null && (
                  <p className="text-[10px] text-gray-400">Margin is a flat-rate estimate — the real Upstox Margin Calculator basket call wasn't available.</p>
                )}
                {mode === "live" && liveResult && (liveResult.stale_legs?.length ?? 0) > 0 && (
                  <p className="text-[10px] font-semibold" style={{ color: C.orange }}>
                    {liveResult.stale_legs.length} leg{liveResult.stale_legs.length > 1 ? "s" : ""} priced off the last-seen quote ({liveResult.stale_legs.join(", ")}) — no fresh live price right now.
                  </p>
                )}
                <PayoffChart
                  curve={activeResult.payoff_curve}
                  markerPrice={mode === "live" ? spotPrice : replayResult?.eval_underlying_close}
                  mtm={mode === "eod" ? replayResult?.mtm : null}
                />
              </div>
            ) : (
              liveResult ? (
                <div className="space-y-4">
                  <label className="flex items-center gap-2 text-[11px] font-semibold text-gray-500">
                    <input type="checkbox" checked={multiplyByLots} onChange={(e) => setMultiplyByLots(e.target.checked)} />
                    Multiply by lot size
                  </label>
                  <div className="grid grid-cols-2 gap-4">
                    <Metric label="Delta" value={(liveResult.greeks.delta * lotMultiplier).toFixed(2)} />
                    <Metric label="Gamma" value={(liveResult.greeks.gamma * lotMultiplier).toFixed(4)} />
                    <Metric label="Theta" value={(liveResult.greeks.theta * lotMultiplier).toFixed(2)} />
                    <Metric label="Vega" value={(liveResult.greeks.vega * lotMultiplier).toFixed(2)} />
                  </div>
                </div>
              ) : (
                <p className="text-xs text-gray-400 py-8 text-center">Greeks are only available in Live mode (self-solved from live premiums).</p>
              )
            )}
          </div>

          {/* Positions — a persistent block below the Payoff/Greeks tab
              content (not a separate tab) so the legs the user just added
              stay visible no matter which right-tab is active. */}
          <div className="border-t px-4 py-3" style={{ borderColor: C.border }}>
            <div className="text-xs font-semibold text-gray-700 mb-2">Positions {legs.length > 0 && `(${legs.length})`}</div>
            {legs.length === 0 ? (
              <p className="text-xs text-gray-400 py-4 text-center">No legs yet — click B/S next to a strike in the chain.</p>
            ) : (
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-[10px] uppercase tracking-wide text-gray-400 border-b" style={{ borderColor: C.border2 }}>
                    <th className="text-left py-2 font-medium">Leg</th>
                    <th className="text-right py-2 font-medium">Lots</th>
                    <th className="text-right py-2 font-medium"></th>
                  </tr>
                </thead>
                <tbody className="divide-y" style={{ borderColor: C.border2 }}>
                  {legs.map((leg, i) => (
                    <tr key={i}>
                      <td className="py-2">
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded font-bold" style={leg.action === "SELL" ? { background: C.sellBg, color: C.sellText } : { background: C.buyBg, color: C.buyText }}>
                          {leg.action} {leg.strike} {leg.option_type}
                        </span>
                      </td>
                      <td className="text-right py-2">
                        <input
                          type="number" min={1} value={leg.lots}
                          onChange={(e) => setLots(i, parseInt(e.target.value) || 1)}
                          className="w-14 px-2 py-1 border rounded text-right text-xs font-semibold"
                          style={{ borderColor: C.border2 }}
                        />
                      </td>
                      <td className="text-right py-2">
                        <button onClick={() => removeLeg(i)} className="p-1 rounded hover:bg-red-50 text-red-500"><Trash2 size={13} /></button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {legs.length > 0 && (
              <button
                onClick={() => (mode === "live" ? evaluateLive() : evaluateReplay(entryDate))}
                disabled={evaluating}
                className="w-full mt-3 py-2 rounded-lg text-xs font-semibold text-gray-600 bg-gray-50 border disabled:opacity-40 hover:bg-gray-100 flex items-center justify-center gap-1.5"
                style={{ borderColor: C.border2 }}
              >
                <RefreshCw size={12} className={evaluating ? "animate-spin" : ""} />
                {evaluating ? "Evaluating..." : mode === "live" ? "Refresh Prices" : "Re-lock Entry"}
              </button>
            )}
            {legs.length > 0 && !evaluating && (
              <p className="text-[10px] text-gray-400 text-center mt-2">Payoff/Greeks update automatically as you add or edit legs.</p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
