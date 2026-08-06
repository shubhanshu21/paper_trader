// api.ts — API client for paper trading application
import type { CustomStrategy, CustomStrategyRules, EngineRules, PortfolioGreeksResponse } from './types/customStrategy';

// Builds a same-origin WebSocket URL (nginx proxies /ws/ to the API, see
// deploy/automate-nginx.conf) so this works unmodified in dev and prod.
export function wsUrl(path: string): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}${path}`;
}

export interface BacktestCycle {
  entry_date: string;
  expiry: string;
  exit_date: string;
  exit_reason: string;
  net_pnl: number;
  pnl_pct_of_premium: number;
  won: boolean;
  liquid: boolean;
  symbol?: string;
}

export interface PerSymbolBreakdown {
  cycles_tested: number;
  avg_return_pct: number;
  win_rate_pct: number;
}

export interface BacktestResult {
  run_id?: number;
  strategy_id?: number;
  cycles_tested: number;
  avg_return_pct_of_premium: number;
  win_rate_pct: number;
  cycles: BacktestCycle[];
  from_date?: string | null;
  to_date?: string | null;
  run_at?: string;
  total_net_pnl?: number;
  max_drawdown_pct?: number;
  profit_factor?: number | null;
  max_consecutive_wins?: number;
  max_consecutive_losses?: number;
  best_cycle_pct?: number | null;
  worst_cycle_pct?: number | null;
  avg_win_pct?: number | null;
  avg_loss_pct?: number | null;
  equity_curve?: number[];
  // Added for the "world-class" backtest pass — Indian-market-specific
  // (NIFTY 50 benchmark, India risk-free rate) risk/return stats.
  equity_curve_compounded?: number[];
  total_return_pct?: number | null;
  cagr_pct?: number | null;
  sharpe_ratio?: number | null;
  sortino_ratio?: number | null;
  calmar_ratio?: number | null;
  max_drawdown_duration_days?: number | null;
  max_drawdown_ongoing?: boolean;
  exposure_pct?: number | null;
  benchmark_return_pct?: number | null;
  alpha_pct?: number | null;
  sample_size_warning?: "limited" | "very_limited" | null;
  per_symbol?: Record<string, PerSymbolBreakdown>;
  skipped_symbols?: Record<string, string>;
}

export interface BacktestRunSummary {
  run_id: number;
  strategy_id: number;
  status: "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";
  from_date: string | null;
  to_date: string | null;
  progress_current: number;
  progress_total: number | null;
  error_message: string | null;
  created_at: string | null;
  completed_at: string | null;
}

export interface BacktestRunDetail extends BacktestRunSummary {
  result: BacktestResult | null;
}

export interface PayoffSymbolResult {
  max_profit: number | null;
  max_profit_pct: number | null;
  max_loss: number | null;
  capital_basis?: number | null;
  breakevens: number[];
  breakevens_detail?: { price: number; pct_from_spot: number }[];
  payoff_curve?: { price: number; pnl: number }[];
  risk_reward_ratio: number | null;
  probability_of_profit_pct: number | null;
  net_premium: number;
  spot_price?: number;
  expiry?: string;
  legs?: {
    strike: number;
    option_type: "CE" | "PE";
    action: "BUY" | "SELL";
    quantity: number;
    current_price: number;
  }[];
  error?: string;
}

export interface PayoffResponse {
  strategy_id: number;
  symbols: Record<string, PayoffSymbolResult>;
}

export interface MarginPoolStatus {
  available_balance: number | null;
  sufficient: boolean | null; // null = couldn't determine (e.g. live funds API unavailable right now)
}

export interface MarginResponse {
  strategy_id: number;
  symbols: Record<string, { margin_required: number; is_real: boolean; error?: string }>;
  margin_required: number;
  is_real: boolean; // true iff at least one symbol's figure came from the real broker Margin Calculator, not the flat-rate estimate
  paper: MarginPoolStatus;
  live: MarginPoolStatus;
}

export interface PortfolioMarginPool {
  margin_required: number | null; // null = no open legs in this mode, or the real broker basket call is unavailable right now (never guessed/estimated)
  open_legs: number;
}

export interface PortfolioMarginResponse {
  paper: PortfolioMarginPool;
  live: PortfolioMarginPool;
}

export interface AdjustmentPreviewLeg {
  option_type: "CE" | "PE";
  strike: number | null;
  action?: "SELL" | "BUY";
  role?: string | null;
  estimated_premium?: number | null;
}

export interface AdjustmentPreviewResponse {
  triggered: boolean;
  trigger_detail: string;
  current_margin: number | null;
  projected_margin: number | null;
  margin_delta?: number | null;
  legs_closed?: AdjustmentPreviewLeg[];
  new_legs?: AdjustmentPreviewLeg[];
}

export interface ExpiriesResponse {
  symbol: string;
  expiries: { date: string; label: "Weekly" | "Monthly" }[];
}

export interface PositionLeg {
  id: number;
  leg_index: number;
  mode: "paper" | "live";
  instrument_key: string;
  instrument_type: string;
  option_type: string | null;
  strike: number | null;
  expiry: string | null;
  transaction_type: "BUY" | "SELL";
  quantity: number;
  entry_price: number;
  exit_price: number | null;
  order_id: string | null;
  exit_order_id: string | null;
  status: "OPEN" | "CLOSED";
  exit_reason: string | null;
  opened_at: string;
  closed_at: string | null;
}

export interface User {
  id: number;
  username: string;
  email: string;
  role: string;
  is_active: boolean;
  mfa_enabled: boolean;
  created_at: string | null;
  info_message?: string;
}

export type LoginResult =
  | { user: User; message: string }
  | { mfa_required: true; mfa_token: string };

export interface WalletSummary {
  starting_capital: number;
  available_balance: number;
  margin_blocked: number;
  total_charges_lifetime: number;
  realized_pnl_lifetime: number;
  total_adjustments: number;
  open_positions_count: number;
}

export interface ChargeRates {
  brokerage_per_order: number;
  exchange_charge_pct: number;
  gst_pct: number;
  stt_pct: number;
  sebi_charge_pct: number;
  stamp_duty_pct: number;
}

export interface ChargeRatesResponse {
  rates: ChargeRates;
  defaults: ChargeRates;
}

export interface WalletResetResult extends WalletSummary {
  deleted_positions: number;
  deleted_equity: number;
  deleted_backtests: number;
  deleted_candles: number;
}

export interface LedgerItem {
  date: string;
  position_id: number | null;
  type: string;
  description: string;
  debit: number;
  credit: number;
  balance: number;
}

export interface Order {
  order_id: string | null;
  date: string;
  position_id: number | null;
  strategy_name: string;
  mode: string;
  symbol: string;
  display_symbol?: string;
  leg: string;
  strike: number | null;
  transaction_type: string;
  quantity: number;
  price: number;
  charges: number;
  status: string;
  product: string;  // real margin product ('NRML' | 'MIS' | 'CNC') — see utils/orders.py
}

export interface OptionsPosition {
  id: number;
  strategy_name: string;
  mode: string;
  symbol: string;
  entry_date: string;
  expiry: string;
  call_token: string;
  call_strike: number;
  call_entry_price: number;
  call_order_id: string | null;
  put_token: string;
  put_strike: number;
  put_entry_price: number;
  put_order_id: string | null;
  quantity: number;
  product: string;
  take_profit_pct: number | null;
  stop_loss_pct: number | null;
  exit_days_before_expiry: number;
  status: string;
  exit_date: string | null;
  exit_reason: string | null;
  call_exit_price: number | null;
  put_exit_price: number | null;
  call_exit_order_id: string | null;
  put_exit_order_id: string | null;
  mtm: number | null;
  net_mtm: number | null;
  charges: number | null;
  call_ltp?: number | null;
  put_ltp?: number | null;
}

export interface EquityPosition {
  id: number;
  strategy_name: string;
  mode: string;
  symbol: string;
  display_symbol?: string;
  direction: string;
  product: string;
  entry_date: string;
  entry_price: number;
  quantity: number;
  entry_order_id: string | null;
  status: string;
  exit_date: string | null;
  exit_price: number | null;
  exit_reason: string | null;
  exit_order_id: string | null;
  gross_pnl: number | null;
  net_pnl: number | null;
  charges: number | null;
  current_price: number | null;
  unrealized_pnl: number | null;
  price_updated_at: string | null;
}

export interface MarketDepthLevel {
  price: number;
  qty: number;
  orders: number;
}

export interface MarketDepth {
  instrument_key: string;
  last_price: number;
  ohlc: { open: number | null; high: number | null; low: number | null; close: number | null };
  volume: number;
  average_price: number;
  lower_circuit_limit: number | null;
  upper_circuit_limit: number | null;
  last_trade_time: string | null;
  total_buy_quantity: number;
  total_sell_quantity: number;
  buy: MarketDepthLevel[];
  sell: MarketDepthLevel[];
}

export interface Instrument {
  instrument_key: string;
  exchange_token: string | null;
  symbol: string;
  name: string | null;
  last_price: number | null;
  expiry: string | null;
  strike: number | null;
  tick_size: number | null;
  lot_size: number | null;
  instrument_type: string | null;
  option_type: string | null;
  exchange: string;
}

export interface WatchlistItem extends Instrument {
  watchlist_id: number;
  added_at: string | null;
  order_index: number | null;
  page?: number | null;
}

export interface WatchlistConfig {
  symbols: string[];
  strategy: string;
  mode: string;
  product: string;
  short_window: number;
  long_window: number;
  num_shares: number;
}

export interface LeaderboardRow {
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

export interface IvScreenerRow {
  symbol: string;
  current_iv: number | null;   // % (e.g. 18.5), null if a live solve wasn't possible right now
  iv_rank: number | null;      // 0-100, null until history_required days have accumulated
  history_days: number;
  history_required: number;
  sufficient: boolean;
}

export type OiSignal = "LONG_BUILDUP" | "SHORT_BUILDUP" | "LONG_UNWINDING" | "SHORT_COVERING" | "NEUTRAL";

export interface OiFuturesSignal {
  expiry: string;
  close: number;
  price_change: number;
  price_change_pct: number;
  open_interest: number;
  chg_in_oi: number;
  signal: OiSignal;
}

export interface OiStrikeMove {
  strike: number;
  option_type: string;
  close: number;
  price_change: number;
  open_interest: number;
  chg_in_oi: number;
  signal: OiSignal;
}

export interface OiScannerResponse {
  symbol: string;
  as_of: string;         // the actual bhavcopy date this reflects — NOT necessarily today, see backend docstring
  compared_to: string | null;
  futures: OiFuturesSignal | null;
  top_strike_moves: OiStrikeMove[];
}

export interface ChainReplayLeg {
  close: number | null;
  open_interest: number;
  chg_in_oi: number;
  volume: number;
}

export interface ChainReplayRow {
  strike: number;
  ce: ChainReplayLeg | null;
  pe: ChainReplayLeg | null;
}

export interface ChainReplayResponse {
  symbol: string;
  date: string;
  expiry: string;
  underlying_close: number | null;
  chain: ChainReplayRow[];
}

export interface BacktestRequest {
  symbol: string;
  type?: string;
  strategy: string;
  from_date: string;
  to_date: string;
  num_lots?: number;
  take_profit_pct?: number;
  stop_loss_pct?: number;
  exit_days_before_expiry?: number;
}

export interface ChargesBreakdown {
  brokerage: number;
  exchange_charges: number;
  gst: number;
  stt: number;
  sebi_charges: number;
  stamp_duty: number;
  total: number;
}

export interface BacktestStats {
  cycles: number;
  wins: number;
  win_rate_pct: number;
  total_pnl: number;
  average_pnl: number;
  best_pnl: number;
  worst_pnl: number;
  capital_needed: number;
  total_return_pct: number;
  total_gross_pnl: number;
  total_charges: number;
  charges: ChargesBreakdown;
}

// Distinct from BacktestResult above (the Custom Strategy Builder's
// backtest shape) — this is the OLD legacy hand-written-strategy /api/backtest
// endpoint's shape (a subprocess-wrapped CLI run: mode/meta/trades/stdout/
// stderr). These two were previously both named `BacktestResult`, which
// TypeScript silently merged via declaration merging instead of erroring —
// neither shape's fields actually apply to the other endpoint's response.
export interface LegacyBacktestResult {
  mode: string;
  meta: {
    symbol: string;
    type: string;
    strategy: string;
    from_date: string;
    to_date: string;
  };
  trades?: Array<{
    index: number;
    entry_date: string;
    exit_date: string;
    call_strike: number;
    put_strike: number;
    capital_needed: number;
    gross_pnl: number;
    net_pnl: number;
    charges: ChargesBreakdown;
    return_pct: number;
    exit_reason: string;
    liquid: boolean;
  }>;
  stats_all?: BacktestStats;
  stats_liquid?: BacktestStats;
  command?: string;
  exit_code?: number;
  stdout?: string;
  stderr?: string;
}

// ---------------------------------------------------------------------------
// Helpers for cookies & fetch
// ---------------------------------------------------------------------------

function getCookie(name: string): string {
  const value = `; ${document.cookie}`;
  const parts = value.split(`; ${name}=`);
  if (parts.length === 2) return parts.pop()?.split(';').shift() || '';
  return '';
}

// For the handful of call sites that use a raw fetch() instead of
// request() above (e.g. to stream/poll without the shared error-parsing
// wrapper) — request() already attaches this automatically for every
// POST/PUT/DELETE/PATCH, but a raw fetch() must do it itself or the
// backend's CSRF double-submit check (api/auth.py::validate_csrf) 400s
// it. Spread this into a raw fetch's `headers` for any state-changing call.
export function csrfHeaders(): Record<string, string> {
  const token = getCookie('csrf_token');
  return token ? { 'X-CSRF-Token': token } : {};
}

// Carries the raw (possibly per-field-list) `detail` from a FastAPI error
// response alongside a flattened `.message`, so callers that want the
// original list (e.g. StrategyBuilderModal's per-line validation errors —
// rule_schema.validate_rules() returns List[str]) don't have to re-parse
// a stringified error. Thrown by request() below on any non-2xx response.
export class ApiError extends Error {
  detail?: string | string[];
  constructor(message: string, detail?: string | string[]) {
    super(message);
    this.name = 'ApiError';
    this.detail = detail;
  }
}

async function request<T>(url: string, options: RequestInit = {}): Promise<T> {
  const method = options.method || 'GET';
  const headers = new Headers(options.headers || {});

  // For state-changing requests, read and append the CSRF double-submit token
  if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(method)) {
    const csrfToken = getCookie('csrf_token');
    if (csrfToken) {
      headers.set('X-CSRF-Token', csrfToken);
    }
  }

  // Include credentials (cookies) in all calls
  options.credentials = 'include';
  options.headers = headers;

  const response = await fetch(url, options);

  if (!response.ok) {
    let errMsg = `Request failed: ${response.statusText}`;
    let detail: string | string[] | undefined;
    try {
      const data = await response.json();
      detail = data.detail;
      errMsg = Array.isArray(detail) ? detail.join(' ') : detail || errMsg;
    } catch {
      // ignore
    }
    throw new ApiError(errMsg, detail);
  }

  return response.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// API Methods
// ---------------------------------------------------------------------------

export const api = {
  // Health
  checkHealth: () => request<{ ok: boolean; auth: 'enabled' | 'disabled' }>('/api/health'),

  // Auth
  me: () => request<User>('/api/auth/me'),
  login: (username: string, password: string) =>
    request<LoginResult>('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    }),
  mfaVerifyLogin: (mfaToken: string, code: string) =>
    request<{ user: User; message: string }>('/api/auth/mfa/verify-login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mfa_token: mfaToken, code }),
    }),
  mfaSetup: () =>
    request<{ secret: string; otpauth_uri: string; qr_code_data_uri: string }>('/api/auth/mfa/setup', { method: 'POST' }),
  mfaConfirm: (secret: string, code: string) =>
    request<{ message: string; backup_codes: string[] }>('/api/auth/mfa/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ secret, code }),
    }),
  mfaDisable: (password: string, code: string) =>
    request<{ message: string }>('/api/auth/mfa/disable', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password, code }),
    }),
  googleOauthStatus: () =>
    request<{ configured: boolean }>('/api/auth/oauth/google/status'),
  marketStatus: () =>
    request<{ open: boolean; message: string; server_time_ist: string }>('/api/market-status'),
  upstoxTokenStatus: () =>
    request<{ configured: boolean; valid: boolean; expiry_epoch: number | null }>('/api/upstox-token/status'),
  refreshUpstoxToken: () =>
    request<{ success: boolean; configured: boolean; valid: boolean; expiry_epoch: number | null; timed_out?: boolean }>('/api/upstox-token/refresh', { method: 'POST' }),
  register: (username: string, email: string, password: string) =>
    request<User>('/api/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, email, password }),
    }),
  logout: () =>
    request<{ message: string }>('/api/auth/logout', {
      method: 'POST',
    }),
  logoutAll: () =>
    request<{ message: string }>('/api/auth/logout-all', {
      method: 'POST',
    }),

  // Positions (Options)
  getOpenOptions: () => request<OptionsPosition[]>('/api/positions/open'),
  getClosedOptions: (limit = 50) => request<OptionsPosition[]>(`/api/positions/closed?limit=${limit}`),
  closeOption: (id: number) => request<{ status: string }>('/api/positions/' + id + '/close', { method: 'POST' }),

  // Equity / Crossover positions
  getOpenEquity: () => request<EquityPosition[]>('/api/equity/positions/open'),
  getClosedEquity: (limit = 100) => request<EquityPosition[]>(`/api/equity/positions/closed?limit=${limit}`),
  getEquityWatchlist: () => request<WatchlistConfig>('/api/equity/watchlist'),
  closeEquity: (id: number) => request<EquityPosition>('/api/equity/positions/' + id + '/close', { method: 'POST' }),

  // Manual Trading Terminal
  searchInstruments: (query: string, exchange = '', type = '', limit = 50) =>
    request<Instrument[]>(`/api/terminal/search?query=${encodeURIComponent(query)}&exchange=${exchange}&type=${type}&limit=${limit}`),
  getLtp: (key: string, mode = 'paper') => request<{ instrument_key: string; ltp: number }>(`/api/terminal/ltp/${encodeURIComponent(key.replace(/\|/g, ':'))}?mode=${mode}`),
  getMarketDepth: (key: string, mode = 'paper') => request<MarketDepth>(`/api/terminal/depth/${encodeURIComponent(key.replace(/\|/g, ':'))}?mode=${mode}`),
  getManualPositions: () => request<{ open: EquityPosition[]; closed: EquityPosition[] }>('/api/terminal/positions'),
  executeManualTrade: (trade: { instrument_key: string; direction: 'BUY' | 'SELL'; quantity: number; product?: string; mode?: string }) =>
    request<{ status: string; action: string; order_id: string }>('/api/terminal/trade', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        instrument_key: trade.instrument_key,
        direction: trade.direction,
        quantity: trade.quantity,
        product: trade.product || 'CNC',
        mode: trade.mode || 'paper',
      }),
    }),
  closeManualPosition: (id: number) => request<{ status: string; action: string; order_id: string }>('/api/terminal/positions/' + id + '/close', { method: 'POST' }),

  // Wallet
  getWalletSummary: () => request<WalletSummary>('/api/wallet'),
  getWalletLedger: () => request<LedgerItem[]>('/api/wallet/ledger'),
  adjustWallet: (amount: number, note = '') =>
    request<WalletSummary>('/api/wallet/adjust', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ amount, note }),
    }),
  setStartingCapital: (starting_capital: number) =>
    request<WalletSummary>('/api/wallet/capital', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ starting_capital }),
    }),
  resetWallet: () => request<WalletResetResult>('/api/wallet/reset', { method: 'POST' }),
  getChargeRates: () => request<ChargeRatesResponse>('/api/wallet/charge-rates'),
  setChargeRates: (rates: Partial<ChargeRates>) =>
    request<ChargeRatesResponse>('/api/wallet/charge-rates', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(rates),
    }),

  // Orders
  getOrders: (mode?: string, limit = 200) => request<Order[]>(`/api/orders?limit=${limit}${mode ? `&mode=${mode}` : ''}`),

  // Watchlist
  getWatchlist: (page = 1, userId?: number) => request<WatchlistItem[]>(`/api/watchlist?page=${page}${userId ? `&user_id=${userId}` : ''}`),
  addToWatchlist: (instrumentKey: string, page = 1) =>
    request<{ status: string; id: number }>('/api/watchlist/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instrument_key: instrumentKey, page }),
    }),
  removeFromWatchlist: (instrumentKey: string, page = 1) =>
    request<{ status: string }>('/api/watchlist/remove', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instrument_key: instrumentKey, page }),
    }),
  getWatchlistLtp: (instrumentKey: string, mode = 'paper') =>
    request<{ instrument_key: string; ltp: number }>(`/api/watchlist/ltp/${encodeURIComponent(instrumentKey.replace(/\|/g, ':'))}?mode=${mode}`),
  getBatchLtp: (instrumentKeys: string[]) =>
    request<{ [key: string]: number }>('/api/watchlist/ltp/batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(instrumentKeys),
    }),

  // Backtesting
  runBacktest: (req: BacktestRequest) =>
    request<LegacyBacktestResult>('/api/backtest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
    }),

  // Leaderboard
  getLeaderboard: () => request<{ rows: LeaderboardRow[] }>('/api/leaderboard'),

  // IV percentile screener — across the logged-in user's watchlist symbols
  getIvScreener: () => request<{ rows: IvScreenerRow[] }>('/api/iv-screener'),

  // OI build-up/unwinding scanner — EOD bhavcopy-derived, per symbol
  getOiScanner: (symbol: string) => request<OiScannerResponse>(`/api/oi-scanner?symbol=${encodeURIComponent(symbol)}`),

  // Historical option chain replay
  getChainReplayDates: (symbol: string) => request<{ symbol: string; dates: string[] }>(`/api/chain-replay/dates?symbol=${encodeURIComponent(symbol)}`),
  getChainReplayExpiries: (symbol: string, date: string) =>
    request<{ symbol: string; date: string; expiries: string[] }>(`/api/chain-replay/expiries?symbol=${encodeURIComponent(symbol)}&date=${encodeURIComponent(date)}`),
  getChainReplay: (symbol: string, date: string, expiry: string) =>
    request<ChainReplayResponse>(`/api/chain-replay?symbol=${encodeURIComponent(symbol)}&date=${encodeURIComponent(date)}&expiry=${encodeURIComponent(expiry)}`),

  // Custom strategies (the strategy builder) — list/create/update/delete/status
  // only; everything per-selected-strategy and ephemeral (payoff, live greeks,
  // positions, backtest run polling) stays a page-local fetch in
  // StrategiesView.tsx — shared/mutating list state (redux) stays separate
  // from view-local queries.
  listCustomStrategies: () => request<{ strategies: CustomStrategy[] }>('/api/custom-strategies'),
  createCustomStrategy: (payload: { name: string; instrument_type: string; symbols: string[]; rules: CustomStrategyRules | EngineRules; strategy_type?: string }) =>
    request<CustomStrategy>('/api/custom-strategies', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  updateCustomStrategy: (id: number, payload: { name: string; symbols: string[]; rules: CustomStrategyRules | EngineRules }) =>
    request<CustomStrategy>(`/api/custom-strategies/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  updateCustomStrategyStatus: (id: number, status: string) =>
    request<CustomStrategy>(`/api/custom-strategies/${id}/status`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    }),
  deleteCustomStrategy: (id: number) =>
    request<{ status: string }>(`/api/custom-strategies/${id}`, { method: 'DELETE' }),
  getPortfolioGreeks: () => request<PortfolioGreeksResponse>('/api/custom-strategies/portfolio/greeks'),
  getPortfolioMargin: () => request<PortfolioMarginResponse>('/api/custom-strategies/portfolio/margin'),
  closeCustomStrategyPosition: (id: number) =>
    request<{ message: string }>(`/api/custom-strategies/positions/${id}/close`, { method: 'POST' }),
  getCustomStrategyPositions: (id: number) => request<{ open: PositionLeg[]; closed: PositionLeg[] }>(`/api/custom-strategies/${id}/positions`),
  getCustomStrategyPayoff: (id: number) => request<PayoffResponse>(`/api/custom-strategies/${id}/payoff`),
  getCustomStrategyMargin: (id: number) => request<MarginResponse>(`/api/custom-strategies/${id}/margin`),
  getAdjustmentPreview: (id: number) => request<AdjustmentPreviewResponse>(`/api/custom-strategies/${id}/adjustment-preview`),
  getCustomStrategyBacktestRun: (strategyId: number, runId: number, options?: RequestInit) =>
    request<BacktestRunDetail>(`/api/custom-strategies/${strategyId}/backtest/runs/${runId}`, options),
  getCustomStrategyTemplateExpiries: (symbol: string) =>
    request<ExpiriesResponse>(`/api/custom-strategies/templates/expiries?symbol=${encodeURIComponent(symbol)}`),
  getCustomStrategyBacktestStatus: (id: number) =>
    request<BacktestResult>(`/api/custom-strategies/${id}/backtest`),
  getCustomStrategyBacktestRuns: (id: number) =>
    request<{ runs: BacktestRunSummary[] }>(`/api/custom-strategies/${id}/backtest/runs`),
  runCustomStrategyBacktest: (id: number, fromDate: string | null, toDate: string | null, slippagePct: number = 0.1) =>
    request<{ run_id: number }>(`/api/custom-strategies/${id}/backtest`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ from_date: fromDate, to_date: toDate, slippage_pct: slippagePct }),
    }),
};
