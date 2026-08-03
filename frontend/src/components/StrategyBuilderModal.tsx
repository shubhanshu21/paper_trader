import { useState, useEffect, useRef, lazy, Suspense } from "react";
import { X, ChevronRight, Check, Search, LayoutTemplate, AlertTriangle, RefreshCw, Trash2, Zap, Plus, GitBranch, Table2 } from "lucide-react";
import { Select } from "./Common";
import { C, FONT, formatTime12h, fmtDate } from "../lib/format";
import type { CustomStrategy, CustomStrategyRules } from "../types/customStrategy";
import {
  type LegForm, newLeg, type ConditionForm, newCondition, type EntryMode, type StrikeMode, strikeLabel,
  type ExpiryModeOverride, type ConditionType,
} from "../types/strategyBuilder";

// @xyflow/react (the node canvas) is a sizeable dependency only needed
// once a user actually reaches Step 2 of the builder — code-split so it's
// not in the app's main bundle for every page load (dashboard, orders,
// etc. never touch it). Types/constructors above come from
// types/strategyBuilder.ts, NOT this lazy import, so initial form state
// doesn't have to wait on the chunk loading.
const StrategyFlowCanvas = lazy(() => import("./StrategyFlowCanvas"));

type EditableStrategy = Pick<CustomStrategy, "id" | "name" | "instrument_type" | "symbols" | "rules">;

interface StrategyBuilderModalProps {
  onClose: () => void;
  onSuccess: () => void;
  editStrategy?: EditableStrategy | null;
  // Dispatch the redux thunk (see router/routes/StrategiesRoute.tsx) —
  // this modal never touches fetch()/the store directly, same boundary
  // Advanced Orders draws (AdvancedOrdersView's onCreateOco/etc. props).
  // Rejects with the server's per-field validation messages (string[]) via
  // ApiError.detail — see store/thunks/customStrategiesThunks.ts.
  onCreate: (payload: { name: string; instrument_type: string; symbols: string[]; rules: CustomStrategyRules }) => Promise<CustomStrategy>;
  onUpdate: (id: number, payload: { name: string; symbols: string[]; rules: CustomStrategyRules }) => Promise<CustomStrategy>;
}

function legFromEditable(l: NonNullable<EditableStrategy["rules"]>["legs"][number]): LegForm {
  const exit = l.exit;
  const trailing = exit?.trailing;
  const isEquity = (l.instrument_type ?? "OPTION") === "EQUITY";
  return {
    instrument_type: isEquity ? "EQUITY" : "OPTION",
    action: l.action,
    option_type: l.option_type ?? "CE",
    strike_mode: l.strike_selection?.mode ?? "ATM",
    strike_value: l.strike_selection?.value != null ? String(l.strike_selection.value) : "",
    lots: l.lots,
    expiry_mode: l.expiry_mode ?? "",
    sizing_mode: l.sizing?.mode === "RISK_PCT" ? "RISK_PCT" : "LOTS",
    risk_pct: l.sizing?.risk_pct != null ? String(l.sizing.risk_pct) : "",
    leg_take_profit_pct: exit?.take_profit_pct != null ? String(exit.take_profit_pct) : "",
    leg_stop_loss_pct: exit?.stop_loss_pct != null ? String(exit.stop_loss_pct) : "",
    trailing_enabled: !!trailing?.enabled,
    trail_amount: trailing?.trail_amount != null ? String(trailing.trail_amount) : "",
    trail_type: trailing?.trail_type ?? "points",
  };
}

function conditionFromEditable(entry: EditableStrategy["rules"] extends null ? never : NonNullable<EditableStrategy["rules"]>["entry"] | undefined): ConditionForm {
  const c = newCondition();
  const condition = entry?.condition;
  if (!condition) return c;
  if (condition.type === "MA_CROSSOVER") {
    return { ...c, type: "MA_CROSSOVER", ma_period_days: String(condition.period_days), ma_direction: condition.direction };
  }
  return { ...c, type: "IV_RANK", iv_operator: condition.operator, iv_threshold: String(condition.threshold) };
}

interface StrategyTemplate {
  type: string;
  description: string;
  risk_level: string;
  legs: { action: "BUY" | "SELL"; option_type: "CE" | "PE"; strike_selection: { mode: StrikeMode; value: number | null }; lots: number }[];
}

const TEMPLATE_LABELS: Record<string, string> = {
  STRADDLE: "Straddle", STRANGLE: "Strangle", IRON_CONDOR: "Iron Condor", BUTTERFLY: "Butterfly", CUSTOM: "Custom",
};

function templateLegsToForm(legs: StrategyTemplate["legs"]): LegForm[] {
  return legs.map((l) => ({
    ...newLeg(),
    action: l.action,
    option_type: l.option_type,
    strike_mode: l.strike_selection.mode,
    strike_value: l.strike_selection.value != null ? String(l.strike_selection.value) : "",
    lots: l.lots,
  }));
}

/** Two legs are duplicates if action/option_type/strike mode+value all match — mirrors rule_schema.py's validate_rules() check, surfaced early instead of only after a submit round-trip. */
function findDuplicateLegPairs(legs: LegForm[]): [number, number][] {
  const pairs: [number, number][] = [];
  for (let i = 0; i < legs.length; i++) {
    for (let j = i + 1; j < legs.length; j++) {
      const a = legs[i], b = legs[j];
      if (a.instrument_type !== b.instrument_type) continue;
      if (a.instrument_type === "EQUITY") {
        // Matches rule_schema.py: two EQUITY legs are the same instruction
        // once their action matches — no strike to disambiguate.
        if (a.action === b.action) pairs.push([i, j]);
        continue;
      }
      const sameStrike = a.strike_mode === "ATM"
        ? b.strike_mode === "ATM"
        : b.strike_mode === a.strike_mode && parseFloat(a.strike_value || "0") === parseFloat(b.strike_value || "0");
      if (a.action === b.action && a.option_type === b.option_type && sameStrike) pairs.push([i, j]);
    }
  }
  return pairs;
}

function legPhrase(leg: LegForm): string {
  const isEquity = leg.instrument_type === "EQUITY";
  const size = leg.sizing_mode === "RISK_PCT"
    ? `sized to risk ${leg.risk_pct || "?"}% of capital`
    : isEquity ? `${leg.lots} share${leg.lots === 1 ? "" : "s"}` : `${leg.lots} ${leg.lots === 1 ? "lot" : "lots"}`;
  let phrase: string;
  if (isEquity) {
    phrase = `${leg.action} ${size} of the underlying (equity)`;
  } else {
    let strike = "ATM (at-the-money)";
    if (leg.strike_mode === "OTM_PERCENT") strike = `${leg.strike_value || "?"}% OTM`;
    else if (leg.strike_mode === "OTM_POINTS") strike = `${leg.strike_value || "?"} points OTM`;
    else if (leg.strike_mode === "FIXED") strike = `strike ${leg.strike_value || "?"}`;
    phrase = `${leg.action} ${size} ${strike} ${leg.option_type}`;
    if (leg.expiry_mode) phrase += ` (${leg.expiry_mode.toLowerCase()} expiry, own cycle)`;
  }
  const exitBits: string[] = [];
  if (leg.leg_take_profit_pct) exitBits.push(`+${leg.leg_take_profit_pct}%`);
  if (leg.leg_stop_loss_pct) exitBits.push(`-${leg.leg_stop_loss_pct}%`);
  if (leg.trailing_enabled) exitBits.push(`trailing ${leg.trail_amount || "?"}${leg.trail_type === "percent" ? "%" : "pt"}`);
  if (exitBits.length) phrase += `, own exit: ${exitBits.join("/")}`;
  return phrase;
}

function conditionPhrase(condition: ConditionForm): string {
  if (condition.type === "MA_CROSSOVER") {
    return `price is ${condition.ma_direction === "ABOVE" ? "above" : "below"} its ${condition.ma_period_days}-day moving average`;
  }
  return `IV rank is ${condition.iv_operator === "ABOVE" ? "above" : "below"} ${condition.iv_threshold}`;
}

export default function StrategyBuilderModal({ onClose, onSuccess, editStrategy, onCreate, onUpdate }: StrategyBuilderModalProps) {
  const isEditing = !!editStrategy;
  const [step, setStep] = useState(1);
  const [canvasFullscreen, setCanvasFullscreen] = useState(false);
  const [editorMode, setEditorMode] = useState<"canvas" | "table">("canvas");
  const modalRef = useRef<HTMLDivElement>(null);

  // Real browser Fullscreen API (hides the tab/address bar too — CSS
  // 100vh/100vw alone only fills the browser's own viewport, leaving the
  // chrome visible) rather than a CSS-only "fullscreen". Synced back from
  // the OS/browser side too (e.g. the user presses Esc, or a browser
  // fullscreen indicator) so our toggle button's icon never drifts out of
  // sync with the real state.
  useEffect(() => {
    const onFullscreenChange = () => setCanvasFullscreen(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", onFullscreenChange);
    return () => {
      document.removeEventListener("fullscreenchange", onFullscreenChange);
      // Closing/unmounting the modal (e.g. clicking away or submitting)
      // while still browser-fullscreened must not leave the whole tab
      // stuck fullscreen behind it.
      if (document.fullscreenElement) document.exitFullscreen();
    };
  }, []);

  const toggleCanvasFullscreen = () => {
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else if (canvasFullscreen) {
      // Was in the CSS-only fallback (Fullscreen API unavailable) — just collapse it back.
      setCanvasFullscreen(false);
    } else if (modalRef.current?.requestFullscreen) {
      modalRef.current.requestFullscreen();
    } else {
      // Fullscreen API unavailable (e.g. iframe without allow="fullscreen") — fall back to the CSS-only expand.
      setCanvasFullscreen(true);
    }
  };
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string[]>([]);

  // Template picker — only for brand-new strategies, shown before Step 1.
  const [showTemplatePicker, setShowTemplatePicker] = useState(!isEditing);
  const [templates, setTemplates] = useState<StrategyTemplate[]>([]);
  useEffect(() => {
    if (!showTemplatePicker) return;
    (async () => {
      try {
        const response = await fetch("/api/custom-strategies/templates/strategy-types", { credentials: "include" });
        if (response.ok) setTemplates((await response.json()).strategy_types || []);
      } catch {
        // Picker still works with "Start from scratch" even if templates fail to load.
      }
    })();
  }, [showTemplatePicker]);

  const [name, setName] = useState(editStrategy?.name ?? "");
  const [instrumentType, setInstrumentType] = useState<"INDEX" | "STOCK" | "COMMODITY">((editStrategy?.instrument_type as "INDEX" | "STOCK" | "COMMODITY") ?? "INDEX");
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>(editStrategy?.symbols ?? []);
  const [searchQuery, setSearchQuery] = useState("");
  const [symbolsList, setSymbolsList] = useState<{ stocks: string[]; indices: string[]; commodities: string[] }>({ stocks: [], indices: [], commodities: [] });
  const [showDropdown, setShowDropdown] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const [legs, setLegs] = useState<LegForm[]>(editStrategy?.rules?.legs.map(legFromEditable) ?? [newLeg()]);
  const [entryMode, setEntryMode] = useState<EntryMode>(editStrategy?.rules?.entry.mode ?? "IMMEDIATE");
  const [entryTime, setEntryTime] = useState(editStrategy?.rules?.entry.time ?? "09:20");
  const [condition, setCondition] = useState<ConditionForm>(conditionFromEditable(editStrategy?.rules?.entry));
  const [expiryMode, setExpiryMode] = useState<"WEEKLY" | "MONTHLY">(editStrategy?.rules?.expiry?.mode ?? "WEEKLY");
  const [expiryPreview, setExpiryPreview] = useState<{ date: string; label: string }[]>([]);
  const [takeProfitPct, setTakeProfitPct] = useState(editStrategy?.rules?.exit.take_profit_pct != null ? String(editStrategy.rules.exit.take_profit_pct) : "");
  const [stopLossPct, setStopLossPct] = useState(editStrategy?.rules?.exit.stop_loss_pct != null ? String(editStrategy.rules.exit.stop_loss_pct) : "");
  const [exitTime, setExitTime] = useState(editStrategy?.rules?.exit.exit_time ?? "");
  const [exitDaysBeforeExpiry, setExitDaysBeforeExpiry] = useState(editStrategy?.rules?.exit.exit_days_before_expiry ?? 1);

  const [symbolsError, setSymbolsError] = useState("");

  const pickTemplate = (template: StrategyTemplate | null) => {
    if (template) setLegs(templateLegsToForm(template.legs));
    setShowTemplatePicker(false);
  };

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

  const firstSelectedSymbol = selectedSymbols[0];
  useEffect(() => {
    if (!firstSelectedSymbol) {
      setExpiryPreview([]);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const response = await fetch(`/api/custom-strategies/templates/expiries?symbol=${encodeURIComponent(firstSelectedSymbol)}`, { credentials: "include" });
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
  }, [firstSelectedSymbol]);

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
      instrument_type: l.instrument_type,
      action: l.action,
      lots: l.lots,
      ...(l.instrument_type === "EQUITY"
        ? {}
        : {
            option_type: l.option_type,
            strike_selection: {
              mode: l.strike_mode,
              value: l.strike_mode === "ATM" ? null : parseFloat(l.strike_value) || 0,
            },
            ...(l.expiry_mode ? { expiry_mode: l.expiry_mode } : {}),
          }),
      ...(l.sizing_mode === "RISK_PCT" && l.risk_pct
        ? { sizing: { mode: "RISK_PCT", risk_pct: parseFloat(l.risk_pct) || 0 } }
        : {}),
      ...(l.leg_take_profit_pct || l.leg_stop_loss_pct || l.trailing_enabled
        ? {
            exit: {
              take_profit_pct: l.leg_take_profit_pct ? parseFloat(l.leg_take_profit_pct) : null,
              stop_loss_pct: l.leg_stop_loss_pct ? parseFloat(l.leg_stop_loss_pct) : null,
              trailing: l.trailing_enabled
                ? { enabled: true, trail_amount: parseFloat(l.trail_amount) || 0, trail_type: l.trail_type }
                : null,
            },
          }
        : {}),
    })),
    entry:
      entryMode === "CONDITIONAL"
        ? {
            mode: "CONDITIONAL", time: null,
            condition:
              condition.type === "MA_CROSSOVER"
                ? { type: "MA_CROSSOVER", period_days: parseInt(condition.ma_period_days) || 20, direction: condition.ma_direction }
                : { type: "IV_RANK", operator: condition.iv_operator, threshold: parseFloat(condition.iv_threshold) || 50 },
          }
        : { mode: entryMode, time: entryMode === "AT_TIME" ? entryTime : null },
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
    let s = `${legsTxt} on ${selectedSymbols.join(", ") || "..."} (${expiryMode.toLowerCase()} expiry by default)`;
    if (entryMode === "AT_TIME" && entryTime) s += `, enter at ${formatTime12h(entryTime)}`;
    else if (entryMode === "CONDITIONAL") s += `, enter when ${conditionPhrase(condition)}`;
    else s += ", enter immediately when the strategy goes live";
    const bits: string[] = [];
    if (takeProfitPct) bits.push(`+${takeProfitPct}% profit`);
    if (stopLossPct) bits.push(`-${stopLossPct}% loss`);
    if (exitTime) bits.push(`${formatTime12h(exitTime)} time exit`);
    if (exitDaysBeforeExpiry) bits.push(`${exitDaysBeforeExpiry} day${exitDaysBeforeExpiry !== 1 ? "s" : ""} before expiry`);
    s += ", exit (legs without their own exit) on " + (bits.length ? bits.join(" or ") : "expiry only");
    return s + ".";
  };

  const canProceed = () => {
    switch (step) {
      case 1: return name.trim() && instrumentType && selectedSymbols.length > 0;
      case 2:
        return legs.length > 0 && legs.every((l) =>
          (l.instrument_type === "EQUITY" || l.strike_mode === "ATM" || l.strike_value !== "") &&
          (l.sizing_mode === "LOTS" || l.risk_pct !== "") &&
          (!l.trailing_enabled || l.trail_amount !== "")
        );
      case 3: return true;
      default: return true;
    }
  };

  const handleSubmit = async () => {
    setLoading(true);
    setError([]);
    try {
      if (isEditing) {
        await onUpdate(editStrategy!.id, { name, symbols: selectedSymbols, rules: buildRules() as CustomStrategyRules });
      } else {
        await onCreate({ name, instrument_type: instrumentType, symbols: selectedSymbols, rules: buildRules() as CustomStrategyRules });
      }
      onSuccess();
    } catch (err) {
      // onCreate/onUpdate reject with either the server's per-field
      // validation messages (string[] — see rule_schema.validate_rules())
      // or a single message string, via ApiError.detail through
      // rejectWithValue (see customStrategiesThunks.ts) — never a plain
      // serialized RTK error here since these thunks always use
      // rejectWithValue on failure.
      setError(Array.isArray(err) ? err : [typeof err === "string" ? err : "Failed to save strategy. Please try again."]);
    } finally {
      setLoading(false);
    }
  };
  const renderTableEditor = () => {
    return (
      <div className="space-y-4 overflow-y-auto flex-1 min-h-0 pr-1">
        {/* Entry & Exit Cards */}
        <div className="grid grid-cols-2 gap-4">
          {/* Entry Trigger Card */}
          <div className="rounded-xl border p-4 bg-white shadow-sm" style={{ borderColor: C.border2 }}>
            <div className="flex items-center gap-1.5 text-xs font-bold text-indigo-700 uppercase tracking-wider mb-3">
              <Zap size={13} /> Entry Trigger
            </div>
            <div className="space-y-3">
              <div>
                <label style={{ fontSize: 10, color: C.muted, marginBottom: 4, display: "block" }}>Entry Mode</label>
                <Select
                  value={entryMode}
                  onChange={(v) => setEntryMode(v as EntryMode)}
                  options={[
                    { value: "IMMEDIATE", label: "Immediately" },
                    { value: "AT_TIME", label: "At a specific time" },
                    { value: "CONDITIONAL", label: "On a condition" },
                  ]}
                />
              </div>

              {entryMode === "AT_TIME" && (
                <div>
                  <label style={{ fontSize: 10, color: C.muted, marginBottom: 4, display: "block" }}>Entry Time (IST)</label>
                  <input
                    type="time"
                    value={entryTime}
                    onChange={(e) => setEntryTime(e.target.value)}
                    className="w-full px-2.5 py-1.5 border rounded-lg text-xs font-semibold outline-none focus:border-orange-500"
                    style={{ borderColor: C.border2 }}
                  />
                </div>
              )}

              {entryMode === "CONDITIONAL" && (
                <div className="space-y-3 pt-2.5 border-t" style={{ borderColor: C.border }}>
                  <div className="text-[11px] font-semibold text-indigo-600">Trigger Condition</div>
                  <Select
                    value={condition.type}
                    onChange={(v) => setCondition(c => ({ ...c, type: v as ConditionType }))}
                    options={[
                      { value: "MA_CROSSOVER", label: "Moving-average crossover" },
                      { value: "IV_RANK", label: "IV rank" },
                    ]}
                  />
                  {condition.type === "MA_CROSSOVER" ? (
                    <div className="grid grid-cols-2 gap-2">
                      <div>
                        <label style={{ fontSize: 10, color: C.muted, marginBottom: 4, display: "block" }}>Period (days)</label>
                        <input
                          type="number"
                          min={2}
                          value={condition.ma_period_days}
                          onChange={(e) => setCondition(c => ({ ...c, ma_period_days: e.target.value }))}
                          className="w-full px-2 py-1.5 border rounded-lg text-xs font-semibold outline-none focus:border-orange-500"
                          style={{ borderColor: C.border2 }}
                        />
                      </div>
                      <div>
                        <label style={{ fontSize: 10, color: C.muted, marginBottom: 4, display: "block" }}>Price direction</label>
                        <Select
                          value={condition.ma_direction}
                          onChange={(v) => setCondition(c => ({ ...c, ma_direction: v as "ABOVE" | "BELOW" }))}
                          options={[
                            { value: "ABOVE", label: "Above MA" },
                            { value: "BELOW", label: "Below MA" },
                          ]}
                        />
                      </div>
                    </div>
                  ) : (
                    <div className="grid grid-cols-2 gap-2">
                      <div>
                        <label style={{ fontSize: 10, color: C.muted, marginBottom: 4, display: "block" }}>IV rank is</label>
                        <Select
                          value={condition.iv_operator}
                          onChange={(v) => setCondition(c => ({ ...c, iv_operator: v as "ABOVE" | "BELOW" }))}
                          options={[
                            { value: "ABOVE", label: "Above" },
                            { value: "BELOW", label: "Below" },
                          ]}
                        />
                      </div>
                      <div>
                        <label style={{ fontSize: 10, color: C.muted, marginBottom: 4, display: "block" }}>Threshold (0-100)</label>
                        <input
                          type="number"
                          min={0}
                          max={100}
                          value={condition.iv_threshold}
                          onChange={(e) => setCondition(c => ({ ...c, iv_threshold: e.target.value }))}
                          className="w-full px-2 py-1.5 border rounded-lg text-xs font-semibold outline-none focus:border-orange-500"
                          style={{ borderColor: C.border2 }}
                        />
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>

          {/* Combined Exit Rules Card */}
          <div className="rounded-xl border p-4 bg-white shadow-sm" style={{ borderColor: C.border2 }}>
            <div className="flex items-center gap-1.5 text-xs font-bold text-emerald-700 uppercase tracking-wider mb-3">
              <ChevronRight size={13} className="rotate-90" /> Combined Exit Rules
            </div>
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <label style={{ fontSize: 10, color: C.muted, marginBottom: 4, display: "block" }}>Take Profit %</label>
                  <input
                    type="number"
                    value={takeProfitPct}
                    onChange={(e) => setTakeProfitPct(e.target.value)}
                    placeholder="None"
                    className="w-full px-2.5 py-1.5 border rounded-lg text-xs font-semibold outline-none focus:border-orange-500"
                    style={{ borderColor: C.border2 }}
                  />
                </div>
                <div>
                  <label style={{ fontSize: 10, color: C.muted, marginBottom: 4, display: "block" }}>Stop Loss %</label>
                  <input
                    type="number"
                    value={stopLossPct}
                    onChange={(e) => setStopLossPct(e.target.value)}
                    placeholder="None"
                    className="w-full px-2.5 py-1.5 border rounded-lg text-xs font-semibold outline-none focus:border-orange-500"
                    style={{ borderColor: C.border2 }}
                  />
                </div>
              </div>

              <div>
                <label style={{ fontSize: 10, color: C.muted, marginBottom: 4, display: "block" }}>Exit at time (optional)</label>
                <input
                  type="time"
                  value={exitTime}
                  onChange={(e) => setExitTime(e.target.value)}
                  className="w-full px-2.5 py-1.5 border rounded-lg text-xs font-semibold outline-none focus:border-orange-500"
                  style={{ borderColor: C.border2 }}
                />
              </div>

              <div>
                <label style={{ fontSize: 10, color: C.muted, marginBottom: 4, display: "block" }}>Exit N days before expiry</label>
                <input
                  type="number"
                  min={0}
                  value={exitDaysBeforeExpiry}
                  onChange={(e) => setExitDaysBeforeExpiry(parseInt(e.target.value) || 0)}
                  className="w-full px-2.5 py-1.5 border rounded-lg text-xs font-semibold outline-none focus:border-orange-500"
                  style={{ borderColor: C.border2 }}
                />
              </div>
            </div>
          </div>
        </div>

        {/* Option Legs Table */}
        <div className="rounded-xl border bg-white shadow-sm overflow-hidden" style={{ borderColor: C.border2 }}>
          <div className="px-4 py-3 bg-gray-50 border-b font-semibold text-xs text-gray-700 uppercase tracking-wide flex items-center justify-between" style={{ borderColor: C.border2 }}>
            <span>Option / Future Legs</span>
            <button
              onClick={addLeg}
              disabled={legs.length >= 8}
              className="flex items-center gap-1 px-2.5 py-1 rounded bg-orange-500 hover:bg-orange-600 text-white text-[10px] font-bold transition-colors disabled:opacity-40"
            >
              <Plus size={11} /> Add Leg
            </button>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="bg-slate-50 border-b text-[10px] font-bold text-gray-400 uppercase tracking-wider" style={{ borderColor: C.border2 }}>
                  <th className="py-2.5 px-3">Leg</th>
                  <th className="py-2.5 px-2">Type / Action</th>
                  <th className="py-2.5 px-2">Strike Selection</th>
                  <th className="py-2.5 px-2">Sizing</th>
                  <th className="py-2.5 px-2">Expiry Override</th>
                  <th className="py-2.5 px-2">Per-Leg Exits</th>
                  <th className="py-2.5 px-3 text-center">Remove</th>
                </tr>
              </thead>
              <tbody>
                {legs.map((leg, idx) => {
                  const isEquity = leg.instrument_type === "EQUITY";
                  const hasCalendarSpread = legs.length > 1;

                  return (
                    <tr key={idx} className="border-b text-xs hover:bg-slate-50 transition-colors" style={{ borderColor: C.border }}>
                      {/* Leg # label */}
                      <td className="py-3 px-3 font-bold text-gray-700">
                        #{idx + 1}
                      </td>

                      {/* Instrument, Buy/Sell, CE/PE Toggles */}
                      <td className="py-3 px-2">
                        <div className="space-y-1.5">
                          {/* Option/Equity Selector */}
                          <div className="flex rounded border overflow-hidden" style={{ borderColor: C.border2, width: 140 }}>
                            {(["OPTION", "EQUITY"] as const).map((t) => (
                              <button
                                key={t}
                                onClick={() => updateLeg(idx, { instrument_type: t })}
                                className={`flex-1 py-1 text-[10px] font-semibold ${leg.instrument_type === t ? "bg-gray-700 text-white" : "bg-white text-gray-600"}`}
                              >
                                {t === "OPTION" ? "Option" : "Equity"}
                              </button>
                            ))}
                          </div>

                          <div className="flex gap-1">
                            {/* Buy/Sell */}
                            <div className="flex rounded border overflow-hidden" style={{ borderColor: C.border2, width: 80 }}>
                              {(["BUY", "SELL"] as const).map((a) => (
                                <button
                                  key={a}
                                  onClick={() => updateLeg(idx, { action: a })}
                                  className={`flex-1 py-0.5 text-[10px] font-bold ${leg.action === a ? (a === "BUY" ? "bg-blue-500 text-white" : "bg-red-500 text-white") : "bg-white text-gray-600"}`}
                                >
                                  {a}
                                </button>
                              ))}
                            </div>

                            {/* CE/PE */}
                            {!isEquity && (
                              <div className="flex rounded border overflow-hidden" style={{ borderColor: C.border2, width: 60 }}>
                                {(["CE", "PE"] as const).map((o) => (
                                  <button
                                    key={o}
                                    onClick={() => updateLeg(idx, { option_type: o })}
                                    className={`flex-1 py-0.5 text-[10px] font-bold ${leg.option_type === o ? "bg-orange-500 text-white" : "bg-white text-gray-600"}`}
                                  >
                                    {o}
                                  </button>
                                ))}
                              </div>
                            )}
                          </div>
                        </div>
                      </td>

                      {/* Strike Mode and Value */}
                      <td className="py-3 px-2">
                        {isEquity ? (
                          <span className="text-gray-400 italic text-[11px]">N/A (Equity Spot)</span>
                        ) : (
                          <div className="space-y-1.5" style={{ width: 140 }}>
                            <Select
                              value={leg.strike_mode}
                              onChange={(v) => updateLeg(idx, { strike_mode: v as StrikeMode })}
                              options={[
                                { value: "ATM", label: "ATM" },
                                { value: "OTM_PERCENT", label: "% OTM" },
                                { value: "OTM_POINTS", label: "Points OTM" },
                                { value: "FIXED", label: "Exact strike" },
                              ]}
                            />
                            {leg.strike_mode !== "ATM" ? (
                              <input
                                type="number"
                                value={leg.strike_value}
                                onChange={(e) => updateLeg(idx, { strike_value: e.target.value })}
                                placeholder={leg.strike_mode === "FIXED" ? "Price" : "Offset"}
                                className="w-full px-2 py-1 border rounded text-[11px] outline-none focus:border-orange-500"
                                style={{ borderColor: C.border2 }}
                              />
                            ) : (
                              <div className="text-[10px] text-gray-400 italic">{strikeLabel(leg)}</div>
                            )}
                          </div>
                        )}
                      </td>

                      {/* Sizing (Lots/Risk %) */}
                      <td className="py-3 px-2">
                        <div className="space-y-1.5" style={{ width: 120 }}>
                          <div className="flex rounded border overflow-hidden" style={{ borderColor: C.border2 }}>
                            {(["LOTS", "RISK_PCT"] as const).map((m) => (
                              <button
                                key={m}
                                onClick={() => updateLeg(idx, { sizing_mode: m })}
                                className={`flex-1 py-0.5 text-[9px] font-bold ${leg.sizing_mode === m ? "bg-gray-700 text-white" : "bg-white text-gray-600"}`}
                              >
                                {m === "RISK_PCT" ? "Risk %" : (isEquity ? "Shares" : "Lots")}
                              </button>
                            ))}
                          </div>
                          {leg.sizing_mode === "LOTS" ? (
                            <input
                              type="number"
                              min={1}
                              value={leg.lots}
                              onChange={(e) => updateLeg(idx, { lots: parseInt(e.target.value) || 1 })}
                              className="w-full px-2 py-1 border rounded text-[11px] outline-none focus:border-orange-500"
                              style={{ borderColor: C.border2 }}
                            />
                          ) : (
                            <input
                              type="number"
                              min={0.1}
                              step={0.1}
                              value={leg.risk_pct}
                              placeholder="% cap"
                              onChange={(e) => updateLeg(idx, { risk_pct: e.target.value })}
                              className="w-full px-2 py-1 border rounded text-[11px] outline-none focus:border-orange-500"
                              style={{ borderColor: C.border2 }}
                            />
                          )}
                        </div>
                      </td>

                      {/* Expiry mode override */}
                      <td className="py-3 px-2">
                        {!isEquity && hasCalendarSpread ? (
                          <div style={{ width: 120 }}>
                            <Select
                              value={leg.expiry_mode || "__default"}
                              onChange={(v) => updateLeg(idx, { expiry_mode: (v === "__default" ? "" : v) as ExpiryModeOverride })}
                              options={[
                                { value: "__default", label: "Strategy default" },
                                { value: "WEEKLY", label: "Weekly cycle" },
                                { value: "MONTHLY", label: "Monthly cycle" },
                              ]}
                            />
                          </div>
                        ) : (
                          <span className="text-gray-400 italic text-[11px]">Default</span>
                        )}
                      </td>

                      {/* Per-leg exits */}
                      <td className="py-3 px-2">
                        <div className="space-y-1.5" style={{ width: 160 }}>
                          <div className="grid grid-cols-2 gap-1">
                            <input
                              type="number"
                              placeholder="TP %"
                              value={leg.leg_take_profit_pct}
                              onChange={(e) => updateLeg(idx, { leg_take_profit_pct: e.target.value })}
                              className="px-2 py-0.5 border rounded text-[10px] outline-none focus:border-orange-500"
                              style={{ borderColor: C.border2 }}
                            />
                            <input
                              type="number"
                              placeholder="SL %"
                              value={leg.leg_stop_loss_pct}
                              onChange={(e) => updateLeg(idx, { leg_stop_loss_pct: e.target.value })}
                              className="px-2 py-0.5 border rounded text-[10px] outline-none focus:border-orange-500"
                              style={{ borderColor: C.border2 }}
                            />
                          </div>
                          <label className="flex items-center gap-1 text-[10px] text-gray-600 cursor-pointer">
                            <input
                              type="checkbox"
                              checked={leg.trailing_enabled}
                              onChange={(e) => updateLeg(idx, { trailing_enabled: e.target.checked })}
                            />
                            <span>Trailing Stop</span>
                          </label>
                          {leg.trailing_enabled && (
                            <div className="grid grid-cols-2 gap-1">
                              <input
                                type="number"
                                placeholder="Amt"
                                value={leg.trail_amount}
                                onChange={(e) => updateLeg(idx, { trail_amount: e.target.value })}
                                className="px-2 py-0.5 border rounded text-[10px] outline-none focus:border-orange-500"
                                style={{ borderColor: C.border2 }}
                              />
                              <Select
                                value={leg.trail_type}
                                onChange={(v) => updateLeg(idx, { trail_type: v as "points" | "percent" })}
                                options={[
                                  { value: "points", label: "Pts" },
                                  { value: "percent", label: "%" },
                                ]}
                              />
                            </div>
                          )}
                        </div>
                      </td>

                      {/* Remove button */}
                      <td className="py-3 px-3 text-center">
                        <button
                          onClick={() => removeLeg(idx)}
                          disabled={legs.length <= 1}
                          className="text-red-500 hover:text-red-700 disabled:opacity-30 focus:outline-none transition-colors"
                        >
                          <Trash2 size={14} />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    );
  };
  const availableOptions = instrumentType === "INDEX" ? symbolsList.indices : instrumentType === "COMMODITY" ? symbolsList.commodities : symbolsList.stocks;
  const instrumentTypeNoun = instrumentType === "INDEX" ? "Indices" : instrumentType === "COMMODITY" ? "Commodities" : "Stocks";
  const filteredOptions = availableOptions.filter(
    (s) =>
      s.toLowerCase().includes(searchQuery.toLowerCase()) &&
      !selectedSymbols.includes(s)
  ).slice(0, 10);

  const canvasIsFullscreen = step === 2 && editorMode === "canvas" && canvasFullscreen;

  return (
    <div className={`fixed inset-0 bg-black flex items-center justify-center z-50 ${canvasIsFullscreen ? "bg-opacity-100 p-0" : "bg-opacity-50"}`}>
      <div ref={modalRef} className={`bg-white w-full overflow-hidden flex flex-col ${
        canvasIsFullscreen ? "h-screen max-w-none rounded-none" : step === 2 ? "rounded-lg max-w-[96vw] h-[92vh]" : "rounded-lg max-w-2xl max-h-[90vh]"
      }`} style={FONT}>
        <div className="px-6 py-4 border-b flex items-center justify-between shrink-0" style={{ borderColor: C.border2 }}>
          <div>
            <h2 className="text-lg font-semibold text-gray-800">{showTemplatePicker ? "Choose a Starting Point" : isEditing ? "Edit Strategy" : "Build an Options Strategy"}</h2>
            {!showTemplatePicker && (
              <div className="flex items-center gap-2 mt-1">
                {[1, 2, 3].map((s) => (
                  <div key={s} className={`w-6 h-6 rounded-full flex items-center justify-center text-xs ${s <= step ? "bg-orange-500 text-white" : "bg-gray-200 text-gray-600"}`}>
                    {s < step ? <Check size={12} /> : s}
                  </div>
                ))}
              </div>
            )}
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600"><X size={20} /></button>
        </div>

        {showTemplatePicker ? (
          <div className="p-6 overflow-y-auto flex-1 space-y-4">
            <p className="text-xs text-gray-500">Start from a common strategy shape, or build one leg at a time from scratch. Either way, every leg is fully editable in the next step.</p>
            <div className="grid grid-cols-2 gap-3">
              {templates.filter((t) => t.type !== "CUSTOM").map((t) => (
                <button key={t.type} onClick={() => pickTemplate(t)}
                  className="text-left p-4 rounded-lg border-2 border-gray-200 hover:border-orange-300 transition-colors focus:outline-none"
                >
                  <div className="flex items-center gap-2 mb-1.5">
                    <LayoutTemplate size={14} style={{ color: C.orange }} />
                    <span className="text-sm font-semibold text-gray-800">{TEMPLATE_LABELS[t.type] || t.type}</span>
                    <span className="ml-auto text-[10px] uppercase font-bold px-1.5 py-0.5 rounded"
                      style={{
                        background: t.risk_level === "high" ? "#fdeceb" : t.risk_level === "medium" ? "#fff6df" : "#e8f7ec",
                        color: t.risk_level === "high" ? "#c22b26" : t.risk_level === "medium" ? "#a16a00" : "#1f8a3d",
                      }}>
                      {t.risk_level}
                    </span>
                  </div>
                  <p className="text-xs text-gray-500 leading-relaxed">{t.description}</p>
                  <p className="text-[11px] text-gray-400 mt-2">{t.legs.length} leg{t.legs.length !== 1 ? "s" : ""}</p>
                </button>
              ))}
            </div>
            <button onClick={() => pickTemplate(null)}
              className="w-full text-left p-4 rounded-lg border-2 border-dashed border-gray-200 hover:border-orange-300 transition-colors focus:outline-none">
              <span className="text-sm font-semibold text-gray-700">Start from scratch</span>
              <p className="text-xs text-gray-500 mt-1">Build any combination of legs yourself, one at a time.</p>
            </button>
          </div>
        ) : (
        <div className={step === 2 ? "p-4 flex-1 flex flex-col overflow-hidden min-h-0" : "p-6 overflow-y-auto flex-1"}>
          {error.length > 0 && (
            <div className="mb-4 px-4 py-3 rounded bg-red-50 border border-red-200 text-red-600 text-xs space-y-1 shrink-0">
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
                    setInstrumentType(v as "INDEX" | "STOCK" | "COMMODITY");
                    setSelectedSymbols([]);
                    setSearchQuery("");
                  }}
                  options={[
                    { value: "INDEX", label: "Index Options" },
                    { value: "STOCK", label: "Stock Options" },
                    { value: "COMMODITY", label: "Commodity Options (MCX)", description: "Live/paper trading only — backtesting isn't available yet" },
                  ]}
                />
                {isEditing && <p className="text-xs text-gray-500 mt-1">Instrument type can't be changed after creation — delete and rebuild if you need a different type.</p>}
              </div>

              <div className="relative" ref={dropdownRef}>
                <label className="block text-sm font-medium text-gray-700 mb-2">
                  Select {instrumentTypeNoun} (Multiple Allowed)
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
                    placeholder={`Search and select ${instrumentTypeNoun.toLowerCase()}...`}
                    className="w-full pl-9 pr-3 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-orange-500"
                    style={{ borderColor: C.border2 }}
                  />
                </div>

                {symbolsError && (
                  <p className="text-xs text-red-500 mt-1">{symbolsError}</p>
                )}
                {!symbolsError && showDropdown && searchQuery && filteredOptions.length === 0 && (
                  <p className="text-xs text-gray-400 mt-1">No {instrumentTypeNoun.toLowerCase()} match "{searchQuery}".</p>
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
            <div className="flex flex-col h-full min-h-0">
              <div className="flex items-start justify-between gap-4 shrink-0 pb-3">
                <div>
                  <h3 className="text-sm font-semibold text-gray-700">Step 2 — Design your strategy</h3>
                  <p className="text-xs text-gray-500 mt-0.5">
                    {editorMode === "canvas"
                      ? "Drag nodes around, add legs, and edit any field inline. Symbol → Entry → each Leg → Combined exit. A leg's own exit/trailing overrides the combined exit for that leg only."
                      : "Edit entry/exit rules and every leg in a compact table. A leg's own exit/trailing overrides the combined exit for that leg only."}
                  </p>
                </div>
                <div className="flex items-start gap-3 shrink-0">
                  <div>
                    <div className="text-[11px] text-gray-500 mb-1 text-right">Layout</div>
                    <div className="flex rounded overflow-hidden border" style={{ borderColor: C.border2 }}>
                      <button onClick={() => setEditorMode("canvas")}
                        className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold ${editorMode === "canvas" ? "bg-orange-500 text-white" : "bg-white text-gray-600"}`}>
                        <GitBranch size={13} /> Canvas
                      </button>
                      <button onClick={() => setEditorMode("table")}
                        className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold ${editorMode === "table" ? "bg-orange-500 text-white" : "bg-white text-gray-600"}`}>
                        <Table2 size={13} /> Table
                      </button>
                    </div>
                  </div>
                  <div>
                    <div className="text-[11px] text-gray-500 mb-1 text-right">Default expiry</div>
                    <div className="flex rounded overflow-hidden border" style={{ borderColor: C.border2 }}>
                      {(["WEEKLY", "MONTHLY"] as const).map((mode) => (
                        <button key={mode} onClick={() => setExpiryMode(mode)}
                          className={`px-3 py-1.5 text-xs font-semibold ${expiryMode === mode ? "bg-orange-500 text-white" : "bg-white text-gray-600"}`}>
                          {mode === "WEEKLY" ? "Weekly" : "Monthly"}
                        </button>
                      ))}
                    </div>
                    {expiryPreview[0] && <div className="text-[10px] text-gray-400 mt-1 text-right">Next: {fmtDate((expiryMode === "WEEKLY" ? expiryPreview[0] : expiryPreview.find((e) => e.label === "Monthly")) ?.date ?? expiryPreview[0].date)}</div>}
                  </div>
                </div>
              </div>

              <div className="flex flex-col flex-1 min-h-0">
                {editorMode === "table" ? renderTableEditor() : (
                  <Suspense fallback={
                    <div className="flex items-center justify-center h-full" style={{ background: "#fafafa", borderRadius: 12, border: `1px solid ${C.border2}` }}>
                      <RefreshCw size={20} className="animate-spin" style={{ color: C.orange }} />
                    </div>
                  }>
                    <StrategyFlowCanvas
                      symbols={selectedSymbols}
                      legs={legs}
                      onUpdateLeg={updateLeg}
                      onRemoveLeg={removeLeg}
                      onAddLeg={addLeg}
                      entryMode={entryMode}
                      onEntryModeChange={setEntryMode}
                      entryTime={entryTime}
                      onEntryTimeChange={setEntryTime}
                      condition={condition}
                      onConditionChange={(patch) => setCondition((c) => ({ ...c, ...patch }))}
                      takeProfitPct={takeProfitPct}
                      stopLossPct={stopLossPct}
                      exitTime={exitTime}
                      exitDaysBeforeExpiry={exitDaysBeforeExpiry}
                      onExitChange={(patch) => {
                        if (patch.takeProfitPct !== undefined) setTakeProfitPct(patch.takeProfitPct);
                        if (patch.stopLossPct !== undefined) setStopLossPct(patch.stopLossPct);
                        if (patch.exitTime !== undefined) setExitTime(patch.exitTime);
                        if (patch.exitDaysBeforeExpiry !== undefined) setExitDaysBeforeExpiry(patch.exitDaysBeforeExpiry);
                      }}
                      fullscreen={canvasFullscreen}
                      onToggleFullscreen={toggleCanvasFullscreen}
                    />
                  </Suspense>
                )}
              </div>

              {findDuplicateLegPairs(legs).length > 0 && (
                <div className="shrink-0 pt-3 space-y-1.5">
                  {findDuplicateLegPairs(legs).map(([i, j]) => (
                    <div key={`${i}-${j}`} className="flex items-start gap-2 px-3 py-2 rounded-lg text-xs" style={{ background: "#fff9e6", color: "#a16a00" }}>
                      <AlertTriangle size={13} className="shrink-0 mt-0.5" />
                      <span>Leg {i + 1} and Leg {j + 1} are identical — combine them into one leg with a higher lot count instead of two separate legs.</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {step === 3 && (
            <div className="space-y-4">
              <h3 className="text-sm font-semibold text-gray-700">Step 3 — Review</h3>
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
        )}

        {!showTemplatePicker && (
        <div className="px-6 py-4 border-t flex items-center justify-between shrink-0" style={{ borderColor: C.border2 }}>
          {step > 1 ? (
            <button onClick={() => setStep(step - 1)} className="px-4 py-2 rounded-lg text-sm font-medium bg-gray-100 text-gray-700 hover:bg-gray-200">Back</button>
          ) : <div />}
          {step < 3 ? (
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
        )}
      </div>
    </div>
  );
}
