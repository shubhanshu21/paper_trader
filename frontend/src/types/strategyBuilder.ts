// types/strategyBuilder.ts — the strategy builder's per-leg/entry/exit FORM
// shape (Phase 3: per-leg exit/trailing, per-leg expiry_mode calendar
// spreads, risk-based sizing, conditional MA-crossover/IV-rank entry).
// Distinct from types/customStrategy.ts's CustomStrategyRules: these are
// flat, string-valued React form fields (e.g. "10" not 10, "" meaning
// unset) that StrategyBuilderModal.tsx's buildRules() converts INTO a
// CustomStrategyRules on submit — never sent to the API directly.
//
// Deliberately its own module, separate from components/StrategyFlowCanvas.tsx:
// that file statically imports @xyflow/react (a sizeable dependency only
// ever needed once a user opens the canvas step of the builder), while
// StrategyBuilderModal.tsx needs these types/constructors immediately on
// render (initial useState values) — importing them from the xyflow-heavy
// module would defeat StrategyBuilderModal's React.lazy() split of
// StrategyFlowCanvas and pull xyflow back into the main bundle.
export type LegInstrumentType = "OPTION" | "EQUITY";
export type StrikeMode = "ATM" | "OTM_PERCENT" | "OTM_POINTS" | "FIXED" | "PREMIUM_OFFSET" | "PREMIUM_BAND";
export type ExpiryModeOverride = "" | "WEEKLY" | "MONTHLY"; // "" = inherit the strategy default
export type SizingMode = "LOTS" | "RISK_PCT";
export type EntryMode = "IMMEDIATE" | "AT_TIME" | "CONDITIONAL" | "BEFORE_EXPIRY";
export type ConditionType = "MA_CROSSOVER" | "IV_RANK";
export type Weekday = "MON" | "TUE" | "WED" | "THU" | "FRI" | "SAT" | "SUN";

export interface LegForm {
  instrument_type: LegInstrumentType;
  action: "BUY" | "SELL";
  option_type: "CE" | "PE";
  strike_mode: StrikeMode;
  strike_value: string; // ATM/OTM_PERCENT/OTM_POINTS/FIXED distance-or-price, or PREMIUM_OFFSET's divisor
  band_min: string;     // PREMIUM_BAND only — premium band lower bound (₹)
  band_max: string;     // PREMIUM_BAND only — premium band upper bound (₹)
  lots: number;
  expiry_mode: ExpiryModeOverride;
  sizing_mode: SizingMode;
  risk_pct: string;
  leg_take_profit_pct: string;
  leg_stop_loss_pct: string;
  trailing_enabled: boolean;
  trail_amount: string;
  trail_type: "points" | "percent";
}

export const newLeg = (): LegForm => ({
  instrument_type: "OPTION",
  action: "SELL", option_type: "CE", strike_mode: "ATM", strike_value: "", band_min: "", band_max: "", lots: 1,
  expiry_mode: "", sizing_mode: "LOTS", risk_pct: "",
  leg_take_profit_pct: "", leg_stop_loss_pct: "", trailing_enabled: false, trail_amount: "", trail_type: "points",
});

export interface ConditionForm {
  type: ConditionType;
  ma_period_days: string;
  ma_direction: "ABOVE" | "BELOW";
  iv_operator: "ABOVE" | "BELOW";
  iv_threshold: string;
}

export const newCondition = (): ConditionForm => ({
  type: "MA_CROSSOVER", ma_period_days: "20", ma_direction: "ABOVE", iv_operator: "ABOVE", iv_threshold: "50",
});

export interface BeforeExpiryForm {
  days_before_expiry: string;
  weekday: Weekday | "";   // "" = no weekday preference — enter on the first eligible day
  time: string;            // "" = no intraday gate — enter as soon as the day is eligible
}

export const newBeforeExpiry = (): BeforeExpiryForm => ({ days_before_expiry: "10", weekday: "", time: "" });

export const strikeLabel = (leg: LegForm): string => {
  if (leg.strike_mode === "ATM") return "ATM";
  if (leg.strike_mode === "OTM_PERCENT") return `${leg.strike_value || "?"}% OTM`;
  if (leg.strike_mode === "OTM_POINTS") return `${leg.strike_value || "?"}pt OTM`;
  if (leg.strike_mode === "PREMIUM_OFFSET") return `straddle premium / ${leg.strike_value || "?"} OTM`;
  if (leg.strike_mode === "PREMIUM_BAND") return `₹${leg.band_min || "?"}-₹${leg.band_max || "?"} premium`;
  return `@${leg.strike_value || "?"}`;
};
