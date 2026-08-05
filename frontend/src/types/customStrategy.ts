// types/customStrategy.ts — the custom-strategy-builder domain shape (the
// full Phase 3 per-leg/entry/exit rules: per-leg exit/trailing/expiry_mode/
// sizing, conditional entry). Every field below the top level is optional/
// nullable so a pre-Phase-3 strategy (none of these set) round-trips
// unchanged — see rule_schema.py, the backend's source of truth for this
// shape. Consumed by api.ts (request/response typing) and by every
// component/slice/thunk that touches a CustomStrategy.
export interface CustomStrategyLeg {
  instrument_type?: 'OPTION' | 'EQUITY' | null;  // optional, defaults to OPTION (backward compat)
  action: 'BUY' | 'SELL';
  option_type?: 'CE' | 'PE' | null;              // required iff instrument_type === OPTION
  strike_selection?: {
    mode: 'ATM' | 'OTM_PERCENT' | 'OTM_POINTS' | 'FIXED' | 'PREMIUM_OFFSET' | 'PREMIUM_BAND';
    value: number | null;          // PREMIUM_OFFSET: divisor applied to the live ATM straddle premium
    min?: number | null;           // PREMIUM_BAND only — premium band lower bound (₹)
    max?: number | null;           // PREMIUM_BAND only — premium band upper bound (₹)
  } | null;
  lots: number;                                   // for EQUITY: raw share quantity, not an F&O lot count
  expiry_mode?: 'WEEKLY' | 'MONTHLY' | null;
  sizing?: { mode: 'LOTS' | 'RISK_PCT'; risk_pct?: number } | null;
  exit?: {
    take_profit_pct: number | null;
    stop_loss_pct: number | null;
    trailing?: { enabled: boolean; trail_amount: number; trail_type: 'points' | 'percent' } | null;
  } | null;
}

export interface CustomStrategyRules {
  legs: CustomStrategyLeg[];
  entry: {
    mode: 'IMMEDIATE' | 'AT_TIME' | 'CONDITIONAL' | 'BEFORE_EXPIRY';
    time: string | null;
    condition?:
      | { type: 'MA_CROSSOVER'; period_days: number; direction: 'ABOVE' | 'BELOW' }
      | { type: 'IV_RANK'; operator: 'ABOVE' | 'BELOW'; threshold: number }
      | null;
    before_expiry?: {                                 // required iff mode === 'BEFORE_EXPIRY'
      days_before_expiry: number;                      // entry window opens this many calendar days before the resolved expiry
      weekday?: 'MON' | 'TUE' | 'WED' | 'THU' | 'FRI' | 'SAT' | 'SUN' | null; // soft preference, never skips a whole cycle
      time?: string | null;
    } | null;
  };
  expiry?: { mode: 'WEEKLY' | 'MONTHLY' };
  exit: {
    take_profit_pct: number | null;
    stop_loss_pct: number | null;
    take_profit_amount?: number | null;              // flat ₹ combined MTM profit target, checked alongside take_profit_pct
    take_profit_capital_pct?: number | null;          // % of REAL broker margin blocked for this basket at entry (not premium)
    stop_loss_capital_pct?: number | null;            // same capital base as take_profit_capital_pct
    stop_loss_mode?: 'PCT' | 'BREAKEVEN';             // 'BREAKEVEN' ignores stop_loss_pct — exits on a spot breakeven breach instead
    exit_time: string | null;
    exit_days_before_expiry: number;
  };
}

export interface CustomStrategy {
  id: number;
  name: string;
  description: string;
  instrument_type: string;
  // 'CUSTOM' (the leg-based builder shape, rules: CustomStrategyRules) |
  // 'SUPERTREND_INTRADAY' (strategies/custom/intraday_schema.py's totally
  // different shape — no legs/entry/exit, see that module) | legacy
  // pre-builder values ('STRADDLE'/'STRANGLE'/'IRON_CONDOR'/'BUTTERFLY').
  strategy_type: string;
  symbols: string[];
  rules: CustomStrategyRules | null;
  status: string;
  backtest_return_pct: number | null;
  paper_return_pct: number | null;
  live_return_pct: number | null;
  created_at: string;
  deployed_at: string | null;
}

// Account-wide Greeks — net exposure across EVERY open leg of EVERY
// active strategy the user owns, not just one strategy's own combined
// Greeks (see api/live_greeks.py::compute_portfolio_greeks). "net": null
// plus "message" is a normal empty state (no active strategies, none
// with open legs, or the broker connection isn't ready yet), not an error.
export interface PortfolioGreeksByStrategy {
  strategy_id: number;
  name: string;
  status: string;
  delta: number;
  gamma: number;
  theta: number;
  vega: number;
  open_legs: number;
}

export interface PortfolioGreeksResponse {
  net: { delta: number; gamma: number; theta: number; vega: number } | null;
  by_strategy: PortfolioGreeksByStrategy[];
  open_legs_count: number;
  message?: string;
}
