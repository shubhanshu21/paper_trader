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
  strike_selection?: { mode: 'ATM' | 'OTM_PERCENT' | 'OTM_POINTS' | 'FIXED'; value: number | null } | null;
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
    mode: 'IMMEDIATE' | 'AT_TIME' | 'CONDITIONAL';
    time: string | null;
    condition?:
      | { type: 'MA_CROSSOVER'; period_days: number; direction: 'ABOVE' | 'BELOW' }
      | { type: 'IV_RANK'; operator: 'ABOVE' | 'BELOW'; threshold: number }
      | null;
  };
  expiry?: { mode: 'WEEKLY' | 'MONTHLY' };
  exit: { take_profit_pct: number | null; stop_loss_pct: number | null; exit_time: string | null; exit_days_before_expiry: number };
}

export interface CustomStrategy {
  id: number;
  name: string;
  description: string;
  instrument_type: string;
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
