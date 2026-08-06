// lib/engineSpecs.ts — declarative settings-field specs for the 8 non-leg-
// based strategy engines (strategies/custom/*_schema.py on the backend).
// Each spec's `fields` list drives EngineSettingsForm.tsx's generic
// number/time/select renderer; `defaultRules` seeds the form when an engine
// is first selected (mirrors each schema module's own `_DEFAULTS`). Two
// engines (GRAVITY, SESSION_SELLER) also need `blackout_dates` — a
// variable-length list — and SESSION_SELLER additionally needs a per-weekday
// symbol schedule + per-symbol session clocks; those are rendered by
// dedicated sections in EngineSettingsForm.tsx rather than forced into this
// generic field-list shape.

export type FieldType = "number" | "time" | "select";

export interface FieldOption {
  value: string;
  label: string;
}

export interface FieldSpec {
  key: string; // dot-path into the rules object, e.g. "lots.NIFTY"
  label: string;
  type: FieldType;
  min?: number;
  max?: number;
  step?: number;
  options?: FieldOption[]; // required for type "select"
  hint?: string;
}

export interface EngineSpec {
  strategyType: string;
  label: string;
  tagline: string;
  fields: FieldSpec[];
  defaultRules: Record<string, unknown>;
  needsBlackoutDates?: boolean;
  needsSessionSchedule?: boolean; // SESSION_SELLER only
  fixedSymbols?: string[]; // e.g. WEEKEND_GAP_COMBO always trades NIFTY+SENSEX
}

export const WEEKDAY_OPTIONS: FieldOption[] = [
  { value: "MON", label: "Monday" },
  { value: "TUE", label: "Tuesday" },
  { value: "WED", label: "Wednesday" },
  { value: "THU", label: "Thursday" },
  { value: "FRI", label: "Friday" },
  { value: "SAT", label: "Saturday" },
  { value: "SUN", label: "Sunday" },
];

export const ENGINE_SPECS: EngineSpec[] = [
  {
    strategyType: "SUPERTREND_INTRADAY",
    label: "Supertrend Intraday",
    tagline: "Signal-driven intraday futures/options entries off a Supertrend flip, multiple times a day.",
    fields: [
      { key: "lots", label: "Lots per entry", type: "number", min: 1, step: 1, hint: "Contracts entered on each signal." },
      { key: "supertrend_period", label: "Supertrend period", type: "number", min: 1, step: 1, hint: "ATR lookback period for the Supertrend line." },
      { key: "supertrend_multiplier", label: "Supertrend multiplier", type: "number", min: 0.1, step: 0.1, hint: "ATR multiplier for the Supertrend band width." },
      { key: "candle_interval_minutes", label: "Candle interval (min)", type: "number", min: 1, step: 1, hint: "Timeframe driving the signal." },
      { key: "max_trades_per_day", label: "Max trades/day", type: "number", min: 1, step: 1, hint: "Cap on entries per day." },
      { key: "exit_time", label: "Forced square-off time", type: "time" },
    ],
    defaultRules: {
      lots: 1, supertrend_period: 7, supertrend_multiplier: 3, candle_interval_minutes: 5,
      max_trades_per_day: 3, exit_time: "15:15",
    },
  },
  {
    strategyType: "WEEKEND_GAP_COMBO",
    label: "Weekend Gap Combo",
    tagline: "A fixed NIFTY + SENSEX ratio-spread combo betting on opposite weekend-gap directions.",
    fixedSymbols: ["NIFTY", "SENSEX"],
    fields: [
      {
        key: "bias", label: "Bias", type: "select",
        options: [
          { value: "NIFTY_BULLISH_SENSEX_BEARISH", label: "NIFTY bullish / SENSEX bearish" },
          { value: "SENSEX_BULLISH_NIFTY_BEARISH", label: "SENSEX bullish / NIFTY bearish" },
        ],
        hint: "Which symbol takes the bullish leg structure vs the bearish one.",
      },
      { key: "lots.NIFTY", label: "NIFTY lot multiplier", type: "number", min: 1, step: 1 },
      { key: "lots.SENSEX", label: "SENSEX lot multiplier", type: "number", min: 1, step: 1 },
      { key: "entry_weekday", label: "Entry day", type: "select", options: WEEKDAY_OPTIONS },
      { key: "entry_time", label: "Entry time", type: "time" },
      { key: "exit_weekday", label: "Exit day", type: "select", options: WEEKDAY_OPTIONS },
      { key: "exit_time", label: "Exit time", type: "time" },
      { key: "target_amount", label: "Combined profit target (₹)", type: "number", min: 1, step: 1 },
      { key: "stop_loss_amount", label: "Combined loss stop (₹)", type: "number", min: 1, step: 1 },
    ],
    defaultRules: {
      bias: "NIFTY_BULLISH_SENSEX_BEARISH",
      lots: { NIFTY: 1, SENSEX: 1 },
      entry_weekday: "FRI", entry_time: "15:00",
      exit_weekday: "TUE", exit_time: "15:00",
      target_amount: 3000, stop_loss_amount: 3000,
    },
  },
  {
    strategyType: "OTM_PUT_ROLL",
    label: "OTM Put Roll",
    tagline: "A far-month short put that rolls the strike down after a pullback, capturing decay on the way.",
    fields: [
      { key: "lots", label: "Lots", type: "number", min: 1, step: 1 },
      { key: "initial_otm_points", label: "Initial OTM distance (pts)", type: "number", min: 1, step: 1, hint: "First sold put's distance below spot." },
      { key: "expiry_offset", label: "Expiry offset", type: "number", min: 0, step: 1, hint: "0 = nearest monthly, 1 = next monthly." },
      { key: "pullback_lookback_days", label: "Pullback lookback (days)", type: "number", min: 1, step: 1 },
      { key: "pullback_min_points", label: "Min pullback required (pts)", type: "number", min: 1, step: 1 },
      { key: "roll_points", label: "Roll distance (pts)", type: "number", min: 1, step: 1, hint: "Strike-down distance per roll." },
      { key: "max_rolls_per_cycle", label: "Max rolls per cycle", type: "number", min: 0, step: 1 },
      { key: "candle_interval_minutes", label: "Signal candle interval (min)", type: "number", min: 1, step: 1 },
      { key: "target_capital_pct", label: "Close-all target (% of margin)", type: "number", min: 0.1, step: 0.1 },
      { key: "exit_days_before_expiry", label: "Hard exit (days before expiry)", type: "number", min: 0, step: 1 },
    ],
    defaultRules: {
      lots: 1, initial_otm_points: 500, expiry_offset: 1, pullback_lookback_days: 20,
      pullback_min_points: 500, roll_points: 200, max_rolls_per_cycle: 8,
      candle_interval_minutes: 60, target_capital_pct: 5, exit_days_before_expiry: 1,
    },
  },
  {
    strategyType: "SMART_CONDOR",
    label: "Smart Condor",
    tagline: "A weekly iron condor sized off ATM straddle premium, with automatic short-leg adjustment.",
    fields: [
      { key: "lots", label: "Lots", type: "number", min: 1, step: 1 },
      { key: "dte_weeks_offset", label: "Weekly expiry offset", type: "number", min: 0, step: 1, hint: "0 = nearest weekly, 1 = next weekly." },
      { key: "premium_round_points", label: "Premium round (pts)", type: "number", min: 1, step: 1 },
      { key: "hedge_points", label: "Hedge distance (pts)", type: "number", min: 1, step: 1 },
      { key: "entry_weekday", label: "Entry day", type: "select", options: WEEKDAY_OPTIONS },
      { key: "entry_time", label: "Entry time", type: "time" },
      { key: "exit_weekday", label: "Forced exit day", type: "select", options: WEEKDAY_OPTIONS },
      { key: "exit_time", label: "Forced exit time", type: "time" },
      { key: "target_capital_pct", label: "Close-all target (% of margin)", type: "number", min: 0.1, step: 0.1 },
      { key: "stop_loss_capital_pct", label: "Close-all stop (% of margin)", type: "number", min: 0.1, step: 0.1 },
      { key: "premium_ratio_trigger", label: "Adjustment premium ratio", type: "number", min: 1.01, step: 0.1 },
      { key: "max_adjustments_per_cycle", label: "Max adjustments per cycle", type: "number", min: 0, step: 1 },
    ],
    defaultRules: {
      lots: 1, dte_weeks_offset: 1, premium_round_points: 100, hedge_points: 200,
      entry_weekday: "MON", entry_time: "10:15", exit_weekday: "FRI", exit_time: "14:30",
      target_capital_pct: 1, stop_loss_capital_pct: 1, premium_ratio_trigger: 1.8, max_adjustments_per_cycle: 2,
    },
  },
  {
    strategyType: "GRAVITY",
    label: "Gravity",
    tagline: "Sells a monthly credit spread after a Camarilla-level fakeout snaps back inside.",
    needsBlackoutDates: true,
    fields: [
      { key: "lots", label: "Lots", type: "number", min: 1, step: 1 },
      { key: "expiry_offset", label: "Monthly expiry offset", type: "number", min: 0, step: 1, hint: "0 = nearest monthly." },
      { key: "extreme_lookback_days", label: "Fakeout lookback (days)", type: "number", min: 1, step: 1 },
      { key: "hedge_strikes_away", label: "Hedge distance (strike steps)", type: "number", min: 1, step: 1 },
      { key: "signal_check_time", label: "Signal check time", type: "time" },
      { key: "target_credit_pct", label: "Close-all target (% of credit)", type: "number", min: 0.1, step: 0.1 },
      { key: "min_roi_pct", label: "Min ROI to enter (%)", type: "number", min: 0.1, step: 0.1 },
      { key: "exit_days_before_expiry", label: "Hard exit (days before expiry)", type: "number", min: 0, step: 1 },
    ],
    defaultRules: {
      lots: 1, expiry_offset: 0, extreme_lookback_days: 10, hedge_strikes_away: 2,
      signal_check_time: "15:20", target_credit_pct: 90, min_roi_pct: 3,
      exit_days_before_expiry: 2, blackout_dates: [],
    },
  },
  {
    strategyType: "SESSION_SELLER",
    label: "Session Seller",
    tagline: "Two intraday sessions a day (morning + afternoon), alternating between NIFTY and SENSEX by weekday.",
    needsBlackoutDates: true,
    needsSessionSchedule: true,
    fields: [
      { key: "lots", label: "Lots", type: "number", min: 1, step: 1 },
      { key: "otm_points", label: "Short strike OTM distance (pts)", type: "number", min: 1, step: 1 },
      { key: "hedge_premium_min", label: "Hedge premium band min (₹)", type: "number", min: 0.1, step: 0.1 },
      { key: "hedge_premium_max", label: "Hedge premium band max (₹)", type: "number", min: 0.1, step: 0.1 },
      { key: "stop_loss_pct", label: "Per-leg stop loss (%)", type: "number", min: 1, step: 1 },
    ],
    defaultRules: {
      lots: 1,
      symbol_schedule: { MON: "NIFTY", TUE: "NIFTY", WED: "SENSEX", THU: "SENSEX", FRI: "NIFTY" },
      sessions: {
        NIFTY: { morning_entry: "09:20", morning_exit: "11:30", afternoon_entry: "12:30", afternoon_exit: "15:15" },
        SENSEX: { morning_entry: "09:20", morning_exit: "11:30", afternoon_entry: "12:30", afternoon_exit: "15:15" },
      },
      otm_points: 100, hedge_premium_min: 1, hedge_premium_max: 2, stop_loss_pct: 50, blackout_dates: [],
    },
  },
  {
    strategyType: "MACD_CREDIT_SPREAD",
    label: "MACD Credit Spread",
    tagline: "A credit spread sized to a target premium band, entered/reversed off an hourly MACD signal.",
    fields: [
      { key: "lots", label: "Lots", type: "number", min: 1, step: 1 },
      { key: "macd_fast", label: "MACD fast period", type: "number", min: 1, step: 1 },
      { key: "macd_slow", label: "MACD slow period", type: "number", min: 1, step: 1 },
      { key: "macd_signal", label: "MACD signal period", type: "number", min: 1, step: 1 },
      { key: "credit_min", label: "Target credit min (₹)", type: "number", min: 0.1, step: 0.1 },
      { key: "credit_max", label: "Target credit max (₹)", type: "number", min: 0.1, step: 0.1 },
      { key: "credit_search_max_steps", label: "Strike search cap (steps)", type: "number", min: 1, step: 1 },
      { key: "rollover_day_of_month", label: "Expiry rollover day (1-28)", type: "number", min: 1, max: 28, step: 1 },
      { key: "exit_days_before_expiry", label: "Hard exit (days before expiry)", type: "number", min: 0, step: 1 },
    ],
    defaultRules: {
      lots: 1, macd_fast: 12, macd_slow: 26, macd_signal: 9, credit_min: 90, credit_max: 140,
      credit_search_max_steps: 40, rollover_day_of_month: 15, exit_days_before_expiry: 2,
    },
  },
  {
    strategyType: "DELTA_NEUTRAL_STRANGLE",
    label: "Delta-Neutral Strangle",
    tagline: "A monthly short strangle sized to a target delta, rebalanced on premium skew and delta drift.",
    fields: [
      { key: "lots", label: "Lots", type: "number", min: 1, step: 1 },
      { key: "target_delta", label: "Target |delta| per short leg", type: "number", min: 0.01, max: 0.99, step: 0.01 },
      { key: "strike_grid", label: "Strike grid (pts)", type: "number", min: 1, step: 1 },
      { key: "hedge_premium_min", label: "Hedge premium band min (₹)", type: "number", min: 0.1, step: 0.1 },
      { key: "hedge_premium_max", label: "Hedge premium band max (₹)", type: "number", min: 0.1, step: 0.1 },
      { key: "entry_time", label: "Entry window opens", type: "time" },
      { key: "entry_time_end", label: "Entry window closes", type: "time" },
      { key: "premium_ratio_trigger", label: "Stage-1 rebalance premium ratio", type: "number", min: 1.01, step: 0.1 },
      { key: "delta_trigger_min", label: "Stage-2 reset delta range min", type: "number", min: 0.01, max: 0.99, step: 0.01 },
      { key: "delta_trigger_max", label: "Stage-2 reset delta range max", type: "number", min: 0.01, max: 0.99, step: 0.01 },
      { key: "reset_premium_pct", label: "Reset premium target (% of losing leg)", type: "number", min: 0.1, step: 0.1 },
      { key: "target_capital_pct", label: "Close-all target (% of margin)", type: "number", min: 0.1, step: 0.1 },
      { key: "third_weekly_exit_time", label: "Forced close time (3rd weekly expiry)", type: "time" },
    ],
    defaultRules: {
      lots: 1, target_delta: 0.15, strike_grid: 100, hedge_premium_min: 1, hedge_premium_max: 5,
      entry_time: "09:20", entry_time_end: "10:00", premium_ratio_trigger: 2.0,
      delta_trigger_min: 0.45, delta_trigger_max: 0.50, reset_premium_pct: 50,
      target_capital_pct: 5, third_weekly_exit_time: "15:15",
    },
  },
  {
    strategyType: "WEEKLY_DIRECTIONAL",
    label: "Weekly Directional",
    tagline: "An asymmetric Reverse Iron Fly: a bought ATM straddle funded by an asymmetric OTM sell (2x lots on whichever side an EMA crossover favors), plus a delta-targeted tail hedge on the heavier-sold side.",
    fields: [
      { key: "lots", label: "Lots", type: "number", min: 1, step: 1 },
      { key: "ema_fast", label: "Fast EMA period", type: "number", min: 1, step: 1 },
      { key: "ema_slow", label: "Slow EMA period", type: "number", min: 1, step: 1, hint: "Must be greater than the fast period." },
      { key: "expiry_offset", label: "Weekly expiry offset", type: "number", min: 0, step: 1, hint: "0 = nearest weekly." },
      { key: "short_otm_points", label: "Short strike OTM distance (pts)", type: "number", min: 1, step: 1 },
      { key: "tail_hedge_target_delta", label: "Tail hedge target |delta|", type: "number", min: 0.01, max: 0.99, step: 0.01 },
      { key: "entry_weekday", label: "Entry day", type: "select", options: WEEKDAY_OPTIONS },
      { key: "entry_time", label: "Entry time", type: "time" },
      { key: "target_capital_pct", label: "Close-all target (% of margin)", type: "number", min: 0.1, step: 0.1 },
      { key: "stop_loss_capital_pct", label: "Close-all stop (% of margin)", type: "number", min: 0.1, step: 0.1 },
      { key: "exit_days_before_expiry", label: "Hard exit (days before expiry)", type: "number", min: 0, step: 1, hint: "0 = hold through the traded expiry itself." },
    ],
    defaultRules: {
      lots: 1, ema_fast: 20, ema_slow: 50, expiry_offset: 0, short_otm_points: 250,
      tail_hedge_target_delta: 0.05, entry_weekday: "MON", entry_time: "09:20",
      target_capital_pct: 10, stop_loss_capital_pct: 5, exit_days_before_expiry: 0,
    },
  },
  {
    strategyType: "MATRIX_CALENDAR",
    label: "Matrix Calendar",
    tagline: "A zero-adjustment hybrid Ratio Calendar / Iron Condor: a delta-targeted weekly short strangle, weekly outer hedges, plus same-strike MONTHLY calendar hedges for a Vega-positive profile against IV spikes and gaps.",
    fields: [
      { key: "lots", label: "Lots", type: "number", min: 1, step: 1, hint: "Short legs trade 2x this; every hedge leg trades 1x." },
      { key: "short_target_delta", label: "Short strike target |delta|", type: "number", min: 0.01, max: 0.99, step: 0.01 },
      { key: "strike_grid", label: "Strike grid for delta search (pts)", type: "number", min: 1, step: 1, hint: "100pt increments for liquidity, not the raw 50pt exchange step." },
      { key: "weekly_hedge_points", label: "Weekly outer hedge distance (pts)", type: "number", min: 1, step: 1 },
      { key: "weekly_expiry_offset", label: "Weekly expiry offset", type: "number", min: 0, step: 1, hint: "0 = nearest weekly, 1 = the one after (~8 DTE off a Monday entry)." },
      { key: "entry_weekday", label: "Entry day", type: "select", options: WEEKDAY_OPTIONS },
      { key: "entry_time", label: "Entry time", type: "time" },
      { key: "max_hold_days", label: "Max hold (days)", type: "number", min: 1, step: 1, hint: "Forced exit this many calendar days after entry, regardless of P&L." },
      { key: "target_capital_pct", label: "Close-all target (% of margin)", type: "number", min: 0.1, step: 0.1 },
      { key: "stop_loss_capital_pct", label: "Close-all stop (% of margin)", type: "number", min: 0.1, step: 0.1 },
      { key: "exit_days_before_expiry", label: "Safety-net hard exit (days before weekly expiry)", type: "number", min: 0, step: 1 },
    ],
    defaultRules: {
      lots: 1, short_target_delta: 0.23, strike_grid: 100, weekly_hedge_points: 500,
      weekly_expiry_offset: 1, entry_weekday: "MON", entry_time: "15:16", max_hold_days: 2,
      target_capital_pct: 1.5, stop_loss_capital_pct: 2, exit_days_before_expiry: 1,
    },
  },
];

export function getEngineSpec(strategyType: string): EngineSpec | undefined {
  return ENGINE_SPECS.find((e) => e.strategyType === strategyType);
}

export function getFieldValue(rules: Record<string, unknown>, path: string): unknown {
  const parts = path.split(".");
  let cur: unknown = rules;
  for (const p of parts) {
    if (cur == null || typeof cur !== "object") return undefined;
    cur = (cur as Record<string, unknown>)[p];
  }
  return cur;
}

export function setFieldValue(rules: Record<string, unknown>, path: string, value: unknown): Record<string, unknown> {
  const parts = path.split(".");
  const next: Record<string, unknown> = { ...rules };
  let cur = next;
  for (let i = 0; i < parts.length - 1; i++) {
    const p = parts[i];
    cur[p] = { ...(cur[p] as Record<string, unknown> | undefined) };
    cur = cur[p] as Record<string, unknown>;
  }
  cur[parts[parts.length - 1]] = value;
  return next;
}
