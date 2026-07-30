import { useState, useEffect, useRef } from "react";
import { X, ChevronRight, Check, Plus, Trash2, Search } from "lucide-react";
import { C, FONT, TimePicker, Select, formatTime12h } from "./Common";


type StrikeMode = "ATM" | "OTM_PERCENT" | "OTM_POINTS" | "FIXED";

interface EditableStrategy {
  id: number;
  name: string;
  instrument_type: string;
  symbols: string[];
  rules: {
    legs: { action: "BUY" | "SELL"; option_type: "CE" | "PE"; strike_selection: { mode: StrikeMode; value: number | null }; lots: number }[];
    entry: { mode: "IMMEDIATE" | "AT_TIME"; time: string | null };
    expiry?: { mode: "WEEKLY" | "MONTHLY" };
    exit: { take_profit_pct: number | null; stop_loss_pct: number | null; exit_time: string | null; exit_days_before_expiry: number };
  } | null;
}

interface StrategyBuilderModalProps {
  onClose: () => void;
  onSuccess: () => void;
  editStrategy?: EditableStrategy | null;
}

interface LegForm {
  action: "BUY" | "SELL";
  option_type: "CE" | "PE";
  strike_mode: StrikeMode;
  strike_value: string;
  lots: number;
}

const newLeg = (): LegForm => ({ action: "SELL", option_type: "CE", strike_mode: "ATM", strike_value: "", lots: 1 });

function legPhrase(leg: LegForm): string {
  const lotWord = leg.lots === 1 ? "lot" : "lots";
  let strike = "ATM (at-the-money)";
  if (leg.strike_mode === "OTM_PERCENT") strike = `${leg.strike_value || "?"}% OTM`;
  else if (leg.strike_mode === "OTM_POINTS") strike = `${leg.strike_value || "?"} points OTM`;
  else if (leg.strike_mode === "FIXED") strike = `strike ${leg.strike_value || "?"}`;
  return `${leg.action} ${leg.lots} ${lotWord} ${strike} ${leg.option_type}`;
}

export default function StrategyBuilderModal({ onClose, onSuccess, editStrategy }: StrategyBuilderModalProps) {
  const isEditing = !!editStrategy;
  const [step, setStep] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string[]>([]);

  const [name, setName] = useState(editStrategy?.name ?? "");
  const [instrumentType, setInstrumentType] = useState<"INDEX" | "STOCK">((editStrategy?.instrument_type as "INDEX" | "STOCK") ?? "INDEX");
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>(editStrategy?.symbols ?? []);
  const [searchQuery, setSearchQuery] = useState("");
  const [symbolsList, setSymbolsList] = useState<{ stocks: string[]; indices: string[] }>({ stocks: [], indices: [] });
  const [showDropdown, setShowDropdown] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const [legs, setLegs] = useState<LegForm[]>(
    editStrategy?.rules?.legs.map((l) => ({
      action: l.action,
      option_type: l.option_type,
      strike_mode: l.strike_selection.mode,
      strike_value: l.strike_selection.value != null ? String(l.strike_selection.value) : "",
      lots: l.lots,
    })) ?? [newLeg()]
  );
  const [entryMode, setEntryMode] = useState<"IMMEDIATE" | "AT_TIME">(editStrategy?.rules?.entry.mode ?? "IMMEDIATE");
  const [entryTime, setEntryTime] = useState(editStrategy?.rules?.entry.time ?? "09:20");
  const [expiryMode, setExpiryMode] = useState<"WEEKLY" | "MONTHLY">(editStrategy?.rules?.expiry?.mode ?? "WEEKLY");
  const [expiryPreview, setExpiryPreview] = useState<{ date: string; label: string }[]>([]);
  const [takeProfitPct, setTakeProfitPct] = useState(editStrategy?.rules?.exit.take_profit_pct != null ? String(editStrategy.rules.exit.take_profit_pct) : "");
  const [stopLossPct, setStopLossPct] = useState(editStrategy?.rules?.exit.stop_loss_pct != null ? String(editStrategy.rules.exit.stop_loss_pct) : "");
  const [exitTime, setExitTime] = useState(editStrategy?.rules?.exit.exit_time ?? "");
  const [exitDaysBeforeExpiry, setExitDaysBeforeExpiry] = useState(editStrategy?.rules?.exit.exit_days_before_expiry ?? 1);

  const [symbolsError, setSymbolsError] = useState("");

  useEffect(() => {
    async function fetchSymbols() {
      try {
        const response = await fetch("/api/custom-strategies/templates/symbols", { credentials: "include" });
        if (response.ok) {
          const data = await response.json();
          setSymbolsList(data);
        } else {
          const err = await response.json().catch(() => null);
          setSymbolsError(err?.detail || `Could not load symbol list (HTTP ${response.status}).`);
        }
      } catch (err) {
        console.error("Failed to fetch symbols", err);
        setSymbolsError("Could not load symbol list — check your connection and try again.");
      }
    }
    fetchSymbols();
  }, []);

  useEffect(() => {
    const symbol = selectedSymbols[0];
    if (!symbol) {
      setExpiryPreview([]);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const response = await fetch(`/api/custom-strategies/templates/expiries?symbol=${encodeURIComponent(symbol)}`, { credentials: "include" });
        if (response.ok && !cancelled) {
          const data = await response.json();
          setExpiryPreview(data.expiries || []);
        } else if (!cancelled) {
          setExpiryPreview([]);
        }
      } catch {
        if (!cancelled) setExpiryPreview([]);
      }
    })();
    return () => { cancelled = true; };
  }, [selectedSymbols.join(",")]);

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setShowDropdown(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const updateLeg = (idx: number, patch: Partial<LegForm>) => {
    setLegs(legs.map((l, i) => (i === idx ? { ...l, ...patch } : l)));
  };
  const removeLeg = (idx: number) => setLegs(legs.filter((_, i) => i !== idx));
  const addLeg = () => { if (legs.length < 8) setLegs([...legs, newLeg()]); };

  const buildRules = () => ({
    legs: legs.map((l) => ({
      action: l.action,
      option_type: l.option_type,
      strike_selection: {
        mode: l.strike_mode,
        value: l.strike_mode === "ATM" ? null : parseFloat(l.strike_value) || 0,
      },
      lots: l.lots,
    })),
    entry: { mode: entryMode, time: entryMode === "AT_TIME" ? entryTime : null },
    expiry: { mode: expiryMode },
    exit: {
      take_profit_pct: takeProfitPct ? parseFloat(takeProfitPct) : null,
      stop_loss_pct: stopLossPct ? parseFloat(stopLossPct) : null,
      exit_time: exitTime || null,
      exit_days_before_expiry: exitDaysBeforeExpiry,
    },
  });

  const reviewSentence = (): string => {
    const legsTxt = legs.map(legPhrase).join(" + ");
    let s = `${legsTxt} on ${selectedSymbols.join(", ") || "..."} (${expiryMode.toLowerCase()} expiry)`;
    s += entryMode === "AT_TIME" && entryTime ? `, enter at ${formatTime12h(entryTime)}` : ", enter immediately when the strategy goes live";
    const bits: string[] = [];
    if (takeProfitPct) bits.push(`+${takeProfitPct}% profit`);
    if (stopLossPct) bits.push(`-${stopLossPct}% loss`);
    if (exitTime) bits.push(`${formatTime12h(exitTime)} time exit`);
    if (exitDaysBeforeExpiry) bits.push(`${exitDaysBeforeExpiry} day${exitDaysBeforeExpiry !== 1 ? "s" : ""} before expiry`);
    s += ", exit on " + (bits.length ? bits.join(" or ") : "expiry only");
    return s + ".";
  };

  const canProceed = () => {
    switch (step) {
      case 1: return name.trim() && instrumentType && selectedSymbols.length > 0;
      case 2: return legs.length > 0 && legs.every((l) => l.strike_mode === "ATM" || l.strike_value !== "");
      case 3: return true;
      default: return true;
    }
  };

  const handleSubmit = async () => {
    setLoading(true);
    setError([]);
    try {
      const response = await fetch(
        isEditing ? `/api/custom-strategies/${editStrategy!.id}` : "/api/custom-strategies",
        {
          method: isEditing ? "PUT" : "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(
            isEditing
              ? { name, symbols: selectedSymbols, rules: buildRules() }
              : { name, instrument_type: instrumentType, symbols: selectedSymbols, rules: buildRules() }
          ),
        }
      );
      if (response.ok) {
        onSuccess();
      } else {
        const err = await response.json();
        setError(Array.isArray(err.detail) ? err.detail : [err.detail || "Unknown error"]);
      }
    } catch {
      setError(["Failed to create strategy. Please try again."]);
    } finally {
      setLoading(false);
    }
  };

  const availableOptions = instrumentType === "INDEX" ? symbolsList.indices : symbolsList.stocks;
  const filteredOptions = availableOptions.filter(
    (s) =>
      s.toLowerCase().includes(searchQuery.toLowerCase()) &&
      !selectedSymbols.includes(s)
  ).slice(0, 10);

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg w-full max-w-2xl max-h-[90vh] overflow-hidden flex flex-col" style={FONT}>
        <div className="px-6 py-4 border-b flex items-center justify-between shrink-0" style={{ borderColor: C.border2 }}>
          <div>
            <h2 className="text-lg font-semibold text-gray-800">{isEditing ? "Edit Strategy" : "Build an Options Strategy"}</h2>
            <div className="flex items-center gap-2 mt-1">
              {[1, 2, 3, 4].map((s) => (
                <div key={s} className={`w-6 h-6 rounded-full flex items-center justify-center text-xs ${s <= step ? "bg-orange-500 text-white" : "bg-gray-200 text-gray-600"}`}>
                  {s < step ? <Check size={12} /> : s}
                </div>
              ))}
            </div>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600"><X size={20} /></button>
        </div>

        <div className="p-6 overflow-y-auto flex-1">
          {error.length > 0 && (
            <div className="mb-4 px-4 py-3 rounded bg-red-50 border border-red-200 text-red-600 text-xs space-y-1">
              {error.map((e, i) => <div key={i}>{e}</div>)}
            </div>
          )}

          {step === 1 && (
            <div className="space-y-4">
              <h3 className="text-sm font-semibold text-gray-700">Step 1 — What are you trading?</h3>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">Strategy Name</label>
                <input className="w-full px-3 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-orange-500"
                  style={{ borderColor: C.border2 }} placeholder="My Weekly Strangle" value={name} onChange={(e) => setName(e.target.value)} />
              </div>
              
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">Instrument Type</label>
                <Select
                  value={instrumentType}
                  disabled={isEditing}
                  onChange={(v) => {
                    setInstrumentType(v as "INDEX" | "STOCK");
                    setSelectedSymbols([]);
                    setSearchQuery("");
                  }}
                  options={[
                    { value: "INDEX", label: "Index Options" },
                    { value: "STOCK", label: "Stock Options" },
                  ]}
                />
                {isEditing && <p className="text-xs text-gray-500 mt-1">Instrument type can't be changed after creation — delete and rebuild if you need a different type.</p>}
              </div>

              <div className="relative" ref={dropdownRef}>
                <label className="block text-sm font-medium text-gray-700 mb-2">
                  Select {instrumentType === "INDEX" ? "Indices" : "Stocks"} (Multiple Allowed)
                </label>
                <div className="relative">
                  <span className="absolute inset-y-0 left-0 pl-3 flex items-center text-gray-400 pointer-events-none">
                    <Search size={16} />
                  </span>
                  <input
                    type="text"
                    value={searchQuery}
                    onFocus={() => setShowDropdown(true)}
                    onChange={(e) => {
                      setSearchQuery(e.target.value);
                      setShowDropdown(true);
                    }}
                    placeholder={`Search and select ${instrumentType === "INDEX" ? "indices" : "stocks"}...`}
                    className="w-full pl-9 pr-3 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-orange-500"
                    style={{ borderColor: C.border2 }}
                  />
                </div>

                {symbolsError && (
                  <p className="text-xs text-red-500 mt-1">{symbolsError}</p>
                )}
                {!symbolsError && showDropdown && searchQuery && filteredOptions.length === 0 && (
                  <p className="text-xs text-gray-400 mt-1">No {instrumentType === "INDEX" ? "indices" : "stocks"} match "{searchQuery}".</p>
                )}

                {showDropdown && searchQuery && filteredOptions.length > 0 && (
                  <div className="absolute z-50 w-full mt-1 bg-white border rounded-lg shadow-lg max-h-48 overflow-y-auto" style={{ borderColor: C.border2 }}>
                    {filteredOptions.map((opt) => (
                      <button
                        key={opt}
                        type="button"
                        onClick={() => {
                          setSelectedSymbols([...selectedSymbols, opt]);
                          setSearchQuery("");
                          setShowDropdown(false);
                        }}
                        className="w-full px-4 py-2 text-left text-xs font-semibold text-gray-700 hover:bg-gray-100 transition-colors"
                      >
                        {opt}
                      </button>
                    ))}
                  </div>
                )}

                {/* Selected Symbols Chips */}
                <div className="flex flex-wrap gap-1.5 mt-3">
                  {selectedSymbols.map((s) => (
                    <span 
                      key={s} 
                      className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold bg-orange-50 text-orange-700 border border-orange-200"
                    >
                      {s}
                      <button 
                        type="button" 
                        onClick={() => setSelectedSymbols(selectedSymbols.filter(x => x !== s))}
                        className="text-orange-400 hover:text-orange-600 focus:outline-none"
                      >
                        <X size={12} />
                      </button>
                    </span>
                  ))}
                </div>
              </div>
            </div>
          )}

          {step === 2 && (
            <div className="space-y-4">
              <h3 className="text-sm font-semibold text-gray-700">Step 2 — What do you want to Buy/Sell?</h3>
              <p className="text-xs text-gray-500">Add as many legs as your strategy needs — a straddle, strangle, iron condor, or anything else is just a combination of legs like these.</p>
              <div className="space-y-3">
                {legs.map((leg, idx) => (
                  <div key={idx} className="border rounded-lg p-4 space-y-3 bg-gray-50" style={{ borderColor: C.border2 }}>
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-semibold text-gray-500">Leg {idx + 1}</span>
                      {legs.length > 1 && (
                        <button onClick={() => removeLeg(idx)} className="text-red-400 hover:text-red-600"><Trash2 size={14} /></button>
                      )}
                    </div>
                    <div className="grid grid-cols-4 gap-2">
                      <div>
                        <label className="block text-[11px] text-gray-500 mb-1">Action</label>
                        <div className="flex rounded overflow-hidden border" style={{ borderColor: C.border2 }}>
                          {(["BUY", "SELL"] as const).map((a) => (
                            <button key={a} onClick={() => updateLeg(idx, { action: a })}
                              className={`flex-1 py-1.5 text-xs font-semibold ${leg.action === a ? (a === "BUY" ? "bg-green-500 text-white" : "bg-red-500 text-white") : "bg-white text-gray-600"}`}>
                              {a}
                            </button>
                          ))}
                        </div>
                      </div>
                      <div>
                        <label className="block text-[11px] text-gray-500 mb-1">Option</label>
                        <div className="flex rounded overflow-hidden border" style={{ borderColor: C.border2 }}>
                          {(["CE", "PE"] as const).map((o) => (
                            <button key={o} onClick={() => updateLeg(idx, { option_type: o })}
                              className={`flex-1 py-1.5 text-xs font-semibold ${leg.option_type === o ? "bg-orange-500 text-white" : "bg-white text-gray-600"}`}>
                              {o}
                            </button>
                          ))}
                        </div>
                      </div>
                      <div>
                        <label className="block text-[11px] text-gray-500 mb-1">Strike</label>
                        <Select value={leg.strike_mode} onChange={(v) => updateLeg(idx, { strike_mode: v as StrikeMode })}
                          options={[
                            { value: "ATM", label: "ATM" },
                            { value: "OTM_PERCENT", label: "% OTM" },
                            { value: "OTM_POINTS", label: "Points OTM" },
                            { value: "FIXED", label: "Exact strike" },
                          ]} />
                      </div>
                      <div>
                        <label className="block text-[11px] text-gray-500 mb-1">Lots</label>
                        <input type="number" min={1} value={leg.lots} onChange={(e) => updateLeg(idx, { lots: parseInt(e.target.value) || 1 })}
                          className="w-full px-2 py-1.5 border rounded text-xs" style={{ borderColor: C.border2 }} />
                      </div>
                    </div>
                    {leg.strike_mode !== "ATM" && (
                      <div className="w-1/4">
                        <label className="block text-[11px] text-gray-500 mb-1">
                          {leg.strike_mode === "OTM_PERCENT" ? "% away from spot" : leg.strike_mode === "OTM_POINTS" ? "Points away from spot" : "Strike price"}
                        </label>
                        <input type="number" value={leg.strike_value} onChange={(e) => updateLeg(idx, { strike_value: e.target.value })}
                          className="w-full px-2 py-1.5 border rounded text-xs" style={{ borderColor: C.border2 }} placeholder={leg.strike_mode === "FIXED" ? "24000" : "5"} />
                      </div>
                    )}
                    <div className="text-xs text-gray-500 italic">{legPhrase(leg)}</div>
                  </div>
                ))}
              </div>
              {legs.length < 8 && (
                <button onClick={addLeg} className="flex items-center gap-2 text-sm font-medium text-orange-600 hover:text-orange-700">
                  <Plus size={16} /> Add another leg
                </button>
              )}
            </div>
          )}

          {step === 3 && (
            <div className="space-y-6">
              <div>
                <h3 className="text-sm font-semibold text-gray-700 mb-3">Step 3 — Which expiry?</h3>
                <div className="flex gap-3">
                  {(["WEEKLY", "MONTHLY"] as const).map((mode) => {
                    const next = mode === "WEEKLY"
                      ? expiryPreview[0]
                      : expiryPreview.find((e) => e.label === "Monthly");
                    return (
                      <button key={mode} onClick={() => setExpiryMode(mode)}
                        className={`flex-1 p-3 rounded-lg border-2 text-sm text-left ${expiryMode === mode ? "border-orange-500 bg-orange-50" : "border-gray-200"}`}>
                        <div className="font-medium">{mode === "WEEKLY" ? "Nearest Weekly" : "Nearest Monthly"}</div>
                        <div className="text-xs text-gray-500 mt-1">
                          {next ? `Next: ${next.date}` : selectedSymbols.length ? "Loading available expiries..." : "Pick a symbol in Step 1 to preview dates"}
                        </div>
                      </button>
                    );
                  })}
                </div>
                <p className="text-xs text-gray-500 mt-2">Always re-resolved to the current nearest date at entry time — never a fixed date, so this works correctly for years.</p>
              </div>
              <div>
                <h3 className="text-sm font-semibold text-gray-700 mb-3">Step 3 — When to enter</h3>
                <div className="flex gap-3">
                  <button onClick={() => setEntryMode("IMMEDIATE")}
                    className={`flex-1 p-3 rounded-lg border-2 text-sm text-left ${entryMode === "IMMEDIATE" ? "border-orange-500 bg-orange-50" : "border-gray-200"}`}>
                    <div className="font-medium">Enter immediately</div>
                    <div className="text-xs text-gray-500 mt-1">As soon as the strategy goes live each trading day</div>
                  </button>
                  <div onClick={() => setEntryMode("AT_TIME")} role="button" tabIndex={0}
                    className={`flex-1 p-3 rounded-lg border-2 text-sm text-left cursor-pointer ${entryMode === "AT_TIME" ? "border-orange-500 bg-orange-50" : "border-gray-200"}`}>
                    <div className="font-medium">Enter at a specific time</div>
                    {entryMode === "AT_TIME" && (
                      <div className="mt-2" onClick={(e) => e.stopPropagation()}>
                        <TimePicker value={entryTime} onChange={setEntryTime} />
                      </div>
                    )}
                  </div>
                </div>
              </div>
              <div>
                <h3 className="text-sm font-semibold text-gray-700 mb-3">Step 3 — When to exit</h3>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-2">Take Profit (%)</label>
                    <input type="number" value={takeProfitPct} onChange={(e) => setTakeProfitPct(e.target.value)}
                      className="w-full px-3 py-2 border rounded-lg text-sm" style={{ borderColor: C.border2 }} placeholder="e.g. 40 (leave blank to disable)" />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-2">Stop Loss (%)</label>
                    <input type="number" value={stopLossPct} onChange={(e) => setStopLossPct(e.target.value)}
                      className="w-full px-3 py-2 border rounded-lg text-sm" style={{ borderColor: C.border2 }} placeholder="e.g. 20 (leave blank to disable)" />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-2">Exit at time (optional)</label>
                    <TimePicker value={exitTime} onChange={setExitTime} allowClear placeholder="No time exit" />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-2">Exit days before expiry</label>
                    <input type="number" min={0} value={exitDaysBeforeExpiry} onChange={(e) => setExitDaysBeforeExpiry(parseInt(e.target.value) || 0)}
                      className="w-full px-3 py-2 border rounded-lg text-sm" style={{ borderColor: C.border2 }} />
                  </div>
                </div>
              </div>
            </div>
          )}

          {step === 4 && (
            <div className="space-y-4">
              <h3 className="text-sm font-semibold text-gray-700">Step 4 — Review</h3>
              <div className="bg-gray-50 rounded-lg p-6 border" style={{ borderColor: C.border2 }}>
                <div className="text-sm text-gray-800 leading-relaxed">{reviewSentence()}</div>
              </div>
              <p className="text-xs text-gray-500">
                {isEditing
                  ? "Saving updates this strategy's legs/entry/exit rules. Re-run Backtest afterward — the old result no longer reflects these changes."
                  : "This creates the strategy in DRAFT status. Run a Backtest before Paper Trading it, and Paper Trade it successfully before you can deploy it Live — Upstox never places a real order until you explicitly go Live."}
              </p>
            </div>
          )}
        </div>

        <div className="px-6 py-4 border-t flex items-center justify-between shrink-0" style={{ borderColor: C.border2 }}>
          {step > 1 ? (
            <button onClick={() => setStep(step - 1)} className="px-4 py-2 rounded-lg text-sm font-medium bg-gray-100 text-gray-700 hover:bg-gray-200">Back</button>
          ) : <div />}
          {step < 4 ? (
            <button onClick={() => setStep(step + 1)} disabled={!canProceed()}
              className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium bg-orange-500 text-white hover:bg-orange-600 disabled:opacity-50">
              Next <ChevronRight size={16} />
            </button>
          ) : (
            <button onClick={handleSubmit} disabled={loading}
              className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium bg-orange-500 text-white hover:bg-orange-600 disabled:opacity-50">
              {loading ? (isEditing ? "Saving..." : "Creating...") : (isEditing ? "Save Changes" : "Create Strategy")}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
