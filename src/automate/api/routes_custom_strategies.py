"""
api/routes_custom_strategies.py — Custom strategy CRUD and deployment API.

Supports creating, updating, deploying custom strategies with full
workflow from draft → backtesting → paper trading → live deployment.
"""
import json
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from automate.api.auth import get_current_user
from automate.db.engine import get_db
from automate.db.models import CustomStrategy
from automate.strategies.custom.rule_schema import validate_rules, describe_rules

router = APIRouter(prefix="/api/custom-strategies", tags=["custom-strategies"])


def _current_user_id(user: dict) -> int:
    return int(user["sub"])


def _get_owned_strategy(db: Session, strategy_id: int, user_id: int) -> CustomStrategy:
    """
    Fetch a strategy the caller owns, or 404 — same response whether the
    id doesn't exist at all or belongs to someone else, so this can't be
    used to enumerate other users' strategy ids by status-code probing.
    """
    strategy = db.query(CustomStrategy).filter(
        CustomStrategy.id == strategy_id, CustomStrategy.user_id == user_id
    ).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return strategy


class CustomStrategyCreate(BaseModel):
    """
    Request model for creating a custom strategy.

    `rules` (legs/entry/exit — see strategies/custom/rule_schema.py) is
    what actually drives backtest/paper/live execution now. The
    strategy_type/option_type/strike_offset/expiry_days fields below are
    kept only so old rows created before rules_json existed still
    round-trip; new creates should leave them at their defaults and use
    `rules` instead.
    """
    name: str
    description: Optional[str] = None
    instrument_type: str  # 'INDEX' | 'STOCK' | 'COMMODITY'
    symbols: List[str]
    rules: Dict[str, Any]
    strategy_type: str = "CUSTOM"
    option_type: str = "BOTH"
    strike_offset: Optional[float] = None
    expiry_days: Optional[int] = None
    num_lots: int = 1
    take_profit_pct: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    exit_days_before_expiry: int = 1


class CustomStrategyUpdate(BaseModel):
    """Request model for updating a custom strategy."""
    name: Optional[str] = None
    description: Optional[str] = None
    symbols: Optional[List[str]] = None
    rules: Optional[Dict[str, Any]] = None
    strategy_type: Optional[str] = None
    option_type: Optional[str] = None
    strike_offset: Optional[float] = None
    expiry_days: Optional[int] = None
    num_lots: Optional[int] = None
    take_profit_pct: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    exit_days_before_expiry: Optional[int] = None


class StrategyStatusUpdate(BaseModel):
    """Request model for updating strategy status."""
    status: str  # 'DRAFT' | 'BACKTESTING' | 'PAPER_TRADING' | 'LIVE' | 'PAUSED' | 'STOPPED'


class AutomationSettings(BaseModel):
    """Request model for updating automation settings."""
    auto_roll: bool = False
    roll_threshold_pct: Optional[float] = None
    auto_adjust: bool = False
    greek_threshold_delta: Optional[float] = None
    greek_threshold_theta: Optional[float] = None


class PerformanceUpdate(BaseModel):
    """Request model for updating performance metrics."""
    backtest_return_pct: Optional[float] = None
    paper_return_pct: Optional[float] = None
    live_return_pct: Optional[float] = None


@router.post("")
def create_strategy(strategy: CustomStrategyCreate, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Create a new custom strategy."""
    if not strategy.symbols:
        raise HTTPException(status_code=422, detail="At least one symbol is required.")
    if strategy.instrument_type not in ("INDEX", "STOCK"):
        raise HTTPException(status_code=422, detail="This strategy builder only supports INDEX and STOCK options.")
    errors = validate_rules(strategy.rules)
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    description = strategy.description or describe_rules(strategy.rules, strategy.symbols[0])

    db_strategy = CustomStrategy(
        user_id=_current_user_id(user),
        name=strategy.name,
        description=description,
        instrument_type=strategy.instrument_type,
        symbols=json.dumps(strategy.symbols),
        rules_json=json.dumps(strategy.rules),
        strategy_type=strategy.strategy_type,
        option_type=strategy.option_type,
        strike_offset=strategy.strike_offset,
        expiry_days=strategy.expiry_days,
        num_lots=strategy.num_lots,
        take_profit_pct=strategy.take_profit_pct,
        stop_loss_pct=strategy.stop_loss_pct,
        exit_days_before_expiry=strategy.exit_days_before_expiry,
        status="DRAFT"
    )

    db.add(db_strategy)
    db.commit()
    db.refresh(db_strategy)

    return db_strategy.to_dict()


@router.get("")
def list_strategies(
    status: Optional[str] = None,
    instrument_type: Optional[str] = None,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """List custom strategies owned by the logged-in user, with optional filters."""
    query = db.query(CustomStrategy).filter(CustomStrategy.user_id == _current_user_id(user))

    if status:
        query = query.filter(CustomStrategy.status == status)
    
    if instrument_type:
        query = query.filter(CustomStrategy.instrument_type == instrument_type)
    
    strategies = query.order_by(CustomStrategy.created_at.desc()).all()
    
    return {"strategies": [s.to_dict() for s in strategies]}


@router.get("/{strategy_id}")
def get_strategy(strategy_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Get a specific custom strategy by ID (must be owned by the caller)."""
    strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))
    return strategy.to_dict()


@router.put("/{strategy_id}")
def update_strategy(strategy_id: int, strategy: CustomStrategyUpdate, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Update a custom strategy."""
    db_strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))

    # Editable in any state that isn't actively executing/holding real
    # positions — PAPER_TRADING/LIVE must be paused or stopped first
    # (see routes_strategy_deployment.py's pause/stop, which square off
    # any open position before status leaves those two, so DRAFT/
    # BACKTESTING/PAUSED/STOPPED are all guaranteed flat here).
    if db_strategy.status not in ("DRAFT", "PAUSED", "BACKTESTING", "STOPPED"):
        raise HTTPException(status_code=400, detail="Can only update DRAFT, BACKTESTING, PAUSED, or STOPPED strategies")

    update_data = strategy.dict(exclude_unset=True)

    if "symbols" in update_data:
        update_data["symbols"] = json.dumps(update_data["symbols"])

    if "rules" in update_data:
        rules = update_data.pop("rules")
        errors = validate_rules(rules)
        if errors:
            raise HTTPException(status_code=422, detail=errors)
        update_data["rules_json"] = json.dumps(rules)
        # Changed rules invalidate any prior backtest/paper/live result and
        # the daily entry marker — always send an edited strategy back
        # through Backtest → Paper → Live from scratch rather than letting
        # stale performance numbers linger next to new rules.
        update_data["status"] = "DRAFT"
        update_data["backtest_return_pct"] = None
        update_data["paper_return_pct"] = None
        update_data["live_return_pct"] = None
        update_data["last_entry_date"] = None

    for field, value in update_data.items():
        setattr(db_strategy, field, value)

    db.commit()
    db.refresh(db_strategy)
    
    return db_strategy.to_dict()


@router.delete("/{strategy_id}")
def delete_strategy(strategy_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Delete a custom strategy."""
    db_strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))

    # Only allow deletion if in DRAFT or STOPPED status
    if db_strategy.status not in ("DRAFT", "STOPPED"):
        raise HTTPException(status_code=400, detail="Can only delete DRAFT or STOPPED strategies")
    
    db.delete(db_strategy)
    db.commit()
    
    return {"message": "Strategy deleted successfully"}


@router.patch("/{strategy_id}/status")
def update_strategy_status(strategy_id: int, status_update: StrategyStatusUpdate, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Update strategy status (workflow: DRAFT → BACKTESTING → PAPER_TRADING → LIVE)."""
    db_strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))

    valid_transitions = {
        "DRAFT": ["BACKTESTING", "STOPPED"],
        "BACKTESTING": ["PAPER_TRADING", "DRAFT", "STOPPED"],
        "PAPER_TRADING": ["LIVE", "DRAFT", "PAUSED", "STOPPED"],
        "LIVE": ["PAUSED", "STOPPED"],
        "PAUSED": ["PAPER_TRADING", "LIVE", "STOPPED"],
        "STOPPED": ["DRAFT"]
    }
    
    current_status = db_strategy.status
    new_status = status_update.status
    
    if new_status not in valid_transitions.get(current_status, []):
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid status transition from {current_status} to {new_status}"
        )
    
    db_strategy.status = new_status
    
    if new_status in ["PAPER_TRADING", "LIVE"]:
        db_strategy.deployed_at = datetime.now()
    
    db.commit()
    db.refresh(db_strategy)
    
    return db_strategy.to_dict()


@router.patch("/{strategy_id}/automation")
def update_automation_settings(strategy_id: int, settings: AutomationSettings, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Update automation settings for options handling."""
    db_strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))

    db_strategy.auto_roll = 1 if settings.auto_roll else 0
    db_strategy.roll_threshold_pct = settings.roll_threshold_pct
    db_strategy.auto_adjust = 1 if settings.auto_adjust else 0
    db_strategy.greek_threshold_delta = settings.greek_threshold_delta
    db_strategy.greek_threshold_theta = settings.greek_threshold_theta
    
    db.commit()
    db.refresh(db_strategy)
    
    return db_strategy.to_dict()


@router.patch("/{strategy_id}/performance")
def update_performance(strategy_id: int, performance: PerformanceUpdate, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Update performance metrics for a strategy."""
    db_strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))

    if performance.backtest_return_pct is not None:
        db_strategy.backtest_return_pct = performance.backtest_return_pct
    
    if performance.paper_return_pct is not None:
        db_strategy.paper_return_pct = performance.paper_return_pct
    
    if performance.live_return_pct is not None:
        db_strategy.live_return_pct = performance.live_return_pct
    
    db.commit()
    db.refresh(db_strategy)
    
    return db_strategy.to_dict()


class BacktestRequest(BaseModel):
    from_date: Optional[str] = None  # 'YYYY-MM-DD'
    to_date: Optional[str] = None


@router.post("/{strategy_id}/backtest")
def backtest_strategy(strategy_id: int, request: BacktestRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """
    Run this strategy's rules against real historical NSE F&O bhavcopy data
    (one simulated trade per historical monthly expiry cycle in range) and
    store the average return. See backtest/custom_engine.py for caveats
    (daily EOD data, no intraday slippage model).
    """
    from automate.backtest.custom_engine import CustomRuleBacktestEngine

    db_strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))
    if not db_strategy.rules_json:
        raise HTTPException(status_code=400, detail="This strategy has no rules configured.")
    if db_strategy.instrument_type == "COMMODITY":
        raise HTTPException(
            status_code=400,
            detail="Backtesting isn't available for COMMODITY strategies yet — the historical dataset "
                   "only covers NSE F&O (index/stock). Paper/live trading works for commodities; "
                   "backtest is scoped to INDEX/STOCK for now.",
        )

    symbols = json.loads(db_strategy.symbols)
    symbol = symbols[0]
    rules = json.loads(db_strategy.rules_json)
    option_instrument = "OPTIDX" if db_strategy.instrument_type == "INDEX" else "OPTSTK"
    future_instrument = "FUTIDX" if db_strategy.instrument_type == "INDEX" else "FUTSTK"

    try:
        engine = CustomRuleBacktestEngine(
            symbol=symbol, rules=rules, option_instrument=option_instrument, future_instrument=future_instrument,
        )
        cycles = engine.run(request.from_date, request.to_date)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not cycles:
        raise HTTPException(
            status_code=400,
            detail="No historical cycles produced a valid simulated trade — check the symbol has "
                   "downloaded history (scripts/download_real_history.py) and the date range covers "
                   "real expiry cycles.",
        )

    avg_return_pct = sum(c["pnl_pct_of_premium"] for c in cycles) / len(cycles)
    win_rate = sum(1 for c in cycles if c["won"]) / len(cycles) * 100.0
    run_at = datetime.now()

    result = {
        "strategy_id": strategy_id,
        "cycles_tested": len(cycles),
        "avg_return_pct_of_premium": round(avg_return_pct, 2),
        "win_rate_pct": round(win_rate, 2),
        "from_date": request.from_date,
        "to_date": request.to_date,
        "run_at": run_at.isoformat(),
        "cycles": cycles,
    }

    db_strategy.backtest_return_pct = round(avg_return_pct, 4)
    # Overwrites any previous run for this strategy — one stored result per
    # strategy (see db/models.py's CustomStrategy.backtest_result_json), not
    # a history of every run ever made.
    db_strategy.backtest_result_json = json.dumps(result)
    db_strategy.backtest_run_at = run_at
    if db_strategy.status == "DRAFT":
        db_strategy.status = "BACKTESTING"
    db.commit()
    db.refresh(db_strategy)

    return {**result, "strategy": db_strategy.to_dict()}


@router.get("/{strategy_id}/backtest")
def get_stored_backtest_result(strategy_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """
    The most recently run backtest result for this strategy, persisted —
    lets the UI show "View Backtest Results" anytime without re-running,
    and survive a page reload/navigating away and back. Returns 404 if
    this strategy has never been backtested.
    """
    db_strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))
    if not db_strategy.backtest_result_json:
        raise HTTPException(status_code=404, detail="This strategy hasn't been backtested yet.")
    return json.loads(db_strategy.backtest_result_json)


@router.get("/templates/strategy-types")
def get_strategy_types():
    """Get available strategy types with descriptions."""
    return {
        "strategy_types": [
            {
                "type": "STRADDLE",
                "description": "Buy ATM call and put for volatility plays",
                "risk_level": "high",
                "legs": [{"side": "BUY", "option_type": "CE"}, {"side": "BUY", "option_type": "PE"}]
            },
            {
                "type": "STRANGLE",
                "description": "Buy OTM call and put for cheaper volatility play",
                "risk_level": "medium",
                "legs": [{"side": "BUY", "option_type": "CE"}, {"side": "BUY", "option_type": "PE"}]
            },
            {
                "type": "IRON_CONDOR",
                "description": "Sell OTM put spread and OTM call spread for income",
                "risk_level": "medium",
                "legs": [
                    {"side": "SELL", "option_type": "PE"},
                    {"side": "BUY", "option_type": "PE"},
                    {"side": "SELL", "option_type": "CE"},
                    {"side": "BUY", "option_type": "CE"}
                ]
            },
            {
                "type": "BUTTERFLY",
                "description": "Limited risk strategy for directional moves",
                "risk_level": "low",
                "legs": [
                    {"side": "BUY", "option_type": "CE"},
                    {"side": "SELL", "option_type": "CE", "quantity": 2},
                    {"side": "BUY", "option_type": "CE"}
                ]
            },
            {
                "type": "CUSTOM",
                "description": "Custom multi-leg strategy with full control",
                "risk_level": "variable",
                "legs": []
            }
        ]
    }


@router.get("/templates/instrument-types")
def get_instrument_types():
    """Get available instrument types."""
    return {
        "instrument_types": [
            {"type": "INDEX", "description": "Index options (NIFTY, BANKNIFTY, etc.)"},
            {"type": "STOCK", "description": "Stock options"},
        ]
    }


@router.get("/templates/symbols")
def get_symbols():
    """Get all tradable index and stock symbols."""
    from automate.utils.instrument_cache import InstrumentCache
    try:
        return InstrumentCache().list_tradable_symbols()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not load instrument master: {exc}")


@router.get("/templates/expiries")
def get_expiries(symbol: str):
    """
    Real upcoming option expiries for one underlying, each labeled
    Weekly/Monthly — powers the wizard's expiry-type preview so a user
    picking "Weekly" vs "Monthly" can see the actual date it currently
    resolves to (see utils.option_utils.find_nearest_expiry_by_type,
    which uses the identical "last-in-month = monthly" rule at execution
    time). Preview only — the strategy itself always stores just the
    mode, re-resolved fresh at every entry, never a frozen date.
    """
    from automate.utils.instrument_cache import InstrumentCache

    try:
        df = InstrumentCache().get_or_refresh()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not load instrument master: {exc}")

    import re
    symbol = symbol.upper().strip()
    # The `name` column is the underlying's full company name for stocks
    # ("RELIANCE INDUSTRIES LTD", not "RELIANCE") — only usable as-is for
    # indices. Match by tradingsymbol prefix instead (same pattern as
    # custom_strategy_scheduler._is_leg_for_symbol / InstrumentCache.
    # resolve_nearest_future_key), which works identically for both.
    pattern = re.compile(rf"^{re.escape(symbol)}\d{{2}}")
    opt = df[(df["instrument_type"].isin(["OPTIDX", "OPTSTK"])) & (df["symbol"].astype(str).str.match(pattern))]
    expiries = sorted(set(opt["expiry"].astype(str)))
    if not expiries:
        raise HTTPException(status_code=404, detail=f"No listed option expiries found for '{symbol}'.")

    by_month: Dict[Any, str] = {}
    for exp in expiries:
        y, m, _ = exp.split("-")
        key = (y, m)
        if key not in by_month or exp > by_month[key]:
            by_month[key] = exp
    monthly_set = set(by_month.values())

    return {
        "symbol": symbol,
        "expiries": [{"date": exp, "label": "Monthly" if exp in monthly_set else "Weekly"} for exp in expiries],
    }


@router.get("/{strategy_id}/greeks")
def get_live_greeks(strategy_id: int, db: Session = Depends(get_db)):
    """
    One-off Black-76 Greeks snapshot (see utils/black76.py) for manual/curl
    inspection — the Strategies page itself uses the live-updating
    /ws/custom-strategy-greeks/{id} WebSocket instead (see
    ws_custom_strategy_greeks.py), not this endpoint. Both call the same
    shared computation (api/live_greeks.py) so there's one implementation.
    """
    from automate.api.live_greeks import compute_live_greeks

    payload = compute_live_greeks(strategy_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return payload


@router.get("/{strategy_id}/payoff")
def get_expected_payoff(strategy_id: int, db: Session = Depends(get_db)):
    """
    Expected Max Profit / Max Loss / breakeven(s) in real rupees, computed
    from CURRENT live option premiums against the strategy's own rules
    (see utils/payoff.py for the payoff-diagram math). Works in ANY
    status, including DRAFT — before ever backtesting or deploying, so a
    strategy still under construction shows real numbers, not "N/A".

    Always previews via the PAPER broker (read-only: get_ltp/option chain
    calls only, RuleBasedStrategy.preview() never places an order) —
    regardless of whether the strategy itself is DRAFT/PAPER_TRADING/LIVE,
    since this is a what-if calculation, not an actual position snapshot.
    """
    from datetime import date
    from automate.strategies.custom.rule_strategy import RuleBasedStrategy, resolve_leg_strike
    from automate.utils.option_utils import find_instrument_token, find_leg_iv
    from automate.utils.payoff import compute_payoff, probability_of_profit
    from automate.utils import black76
    from automate.utils.margin import estimate_margin_blocked
    from automate.api.custom_strategy_scheduler import _get_brokers, _audit, _kill_switch, _rate_limiter

    db_strategy = db.query(CustomStrategy).filter(CustomStrategy.id == strategy_id).first()
    if not db_strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if not db_strategy.rules_json:
        raise HTTPException(status_code=400, detail="This strategy has no rules configured.")

    brokers = _get_brokers()
    if brokers is None:
        raise HTTPException(status_code=503, detail="Broker connection not ready yet — try again shortly.")
    broker = brokers["paper"]
    rules = json.loads(db_strategy.rules_json)
    symbols = json.loads(db_strategy.symbols)
    is_index = db_strategy.instrument_type == "INDEX"

    results = {}
    for symbol in symbols:
        try:
            strategy = RuleBasedStrategy(
                broker=broker, audit=_audit, kill_switch=_kill_switch, rate_limiter=_rate_limiter,
                symbol=symbol, rules=rules,
            )
            preview = strategy.preview()
        except Exception as exc:
            results[symbol] = {"error": str(exc)}
            continue

        payoff_legs = [
            {"strike": float(leg["strike"]), "option_type": leg["option_type"], "action": leg["transaction_type"],
             "quantity": leg["quantity"], "premium": leg["current_price"]}
            for leg in preview["legs"] if leg.get("current_price") is not None
        ]
        if len(payoff_legs) != len(preview["legs"]):
            results[symbol] = {"error": "Could not fetch a current price for every leg — try again shortly."}
            continue

        payoff = compute_payoff(payoff_legs)
        spot = preview["spot_price"]  # cash equity/index LTP — fine for display (breakeven distance-from-spot), NOT for Black-76 math below.

        # Black-76 needs the FUTURES price, not cash spot (see utils/black76.py's
        # module docstring — this is the entire reason Black-76 exists instead of
        # plain Black-Scholes). Resolve it the same way api/live_greeks.py does,
        # so this endpoint and the live-Greeks panel agree on the same input —
        # using cash spot here was a real bug: IV solved against the wrong
        # underlying price is biased, which directly biases POP.
        from automate.utils.instrument_cache import InstrumentCache
        resolved_future = InstrumentCache().resolve_nearest_future_key(symbol)
        forward = broker.get_ltp(resolved_future[0]) if resolved_future else None
        forward = forward or spot  # commodities/resolution failure — cash spot is a better fallback than no POP at all.

        # ATM IV — used as the last-resort fallback volatility for POP below
        # (kept in case neither the broker's own per-leg IV nor a per-leg
        # self-solve is available for some reason).
        days_to_expiry = (date.fromisoformat(preview["expiry"]) - date.today()).days
        years_to_expiry = max(days_to_expiry, 1) / 365.0
        atm_iv = None
        atm_chain = None
        try:
            atm_chain = broker.get_option_chain(strategy.instrument_key, preview["expiry"])
            atm_strike = resolve_leg_strike({"option_type": "CE", "strike_selection": {"mode": "ATM", "value": None}}, forward, strategy.strike_step)
            atm_token = find_instrument_token(atm_chain, atm_strike, "CE")
            atm_price = broker.get_ltp(atm_token) if atm_token else None
            if atm_price:
                atm_iv = black76.implied_volatility(forward, atm_strike, years_to_expiry, black76.DEFAULT_RISK_FREE_RATE, atm_price, "CE")
        except Exception:
            atm_iv = None  # POP just comes back as None/"N/A" below — not fatal to the rest of the payoff response.

        # Per-leg IV: a strangle's two sides trade at genuinely different
        # IVs (skew) — blending them into one flat ATM number biases POP,
        # per the Sensibull comparison that flagged this. Prefer Upstox's
        # own broker-computed IV for each leg's exact strike (real market
        # consensus, not derived from our single possibly-stale LTP tick on
        # a thin, deep-OTM contract); fall back to self-solving IV from
        # that leg's own premium/strike if the broker didn't return one.
        call_ivs, put_ivs = [], []
        if atm_chain is not None:
            for leg in payoff_legs:
                leg_iv = find_leg_iv(atm_chain, leg["strike"], leg["option_type"])
                if leg_iv is None or leg_iv <= 0:
                    try:
                        leg_iv = black76.implied_volatility(
                            forward, leg["strike"], years_to_expiry, black76.DEFAULT_RISK_FREE_RATE,
                            leg["premium"], leg["option_type"],
                        )
                    except Exception:
                        leg_iv = None
                if leg_iv and leg_iv > 0:
                    (call_ivs if leg["option_type"] == "CE" else put_ivs).append(leg_iv)

        call_iv = sum(call_ivs) / len(call_ivs) if call_ivs else None
        put_iv = sum(put_ivs) / len(put_ivs) if put_ivs else None

        def _iv_for_bound(bound_price: float) -> Optional[float]:
            # A tail above the underlying's current forward is really about
            # whether the CALL side finishes ITM, and a tail below is about
            # the PUT side — use that leg's own IV for that side's mass.
            return call_iv if bound_price >= forward else put_iv

        fallback_iv = atm_iv or call_iv or put_iv
        pop = (
            probability_of_profit(payoff_legs, payoff["breakevens"], forward, fallback_iv, years_to_expiry, iv_lookup=_iv_for_bound)
            if fallback_iv else None
        )

        risk_reward_ratio = None
        if payoff["max_profit"] is not None and payoff["max_loss"] is not None and payoff["max_loss"] != 0:
            risk_reward_ratio = round(payoff["max_profit"] / abs(payoff["max_loss"]), 2)

        # Capital-at-risk basis for the ROI% shown next to Max Profit/Loss:
        # short legs need margin blocked (rough SPAN+exposure estimate,
        # same "not your real broker figure" caveat utils/margin.py and
        # historical_engine.py already carry); a pure debit strategy (no
        # short legs) instead uses the actual premium paid as its capital base.
        # Uses the LARGEST single short leg's quantity, not the sum across
        # legs — a strangle's two opposite-direction short legs can't both
        # go against you at once (the underlying can't crash and rally
        # simultaneously), so real exchange SPAN margining gives a benefit
        # for the second leg rather than stacking full margin on top of it;
        # summing double-counted the requirement and understated the ROI%.
        short_qty = max((l["quantity"] for l in payoff_legs if l["action"] == "SELL"), default=0)
        if short_qty > 0:
            capital_basis = estimate_margin_blocked(spot, short_qty, is_index)
        else:
            capital_basis = abs(payoff["net_premium"]) or None
        max_profit_pct = round(payoff["max_profit"] / capital_basis * 100, 2) if payoff["max_profit"] is not None and capital_basis else None

        results[symbol] = {
            **payoff,
            "max_profit_pct": max_profit_pct,
            "risk_reward_ratio": risk_reward_ratio,
            "probability_of_profit_pct": round(pop * 100, 2) if pop is not None else None,
            "breakevens_detail": [
                {"price": b, "pct_from_spot": round((b - spot) / spot * 100, 2)} for b in payoff["breakevens"]
            ],
            "spot_price": spot,
            "expiry": preview["expiry"],
            "legs": [
                {"strike": l["strike"], "option_type": l["option_type"], "action": l["transaction_type"],
                 "quantity": l["quantity"], "current_price": l["current_price"]}
                for l in preview["legs"]
            ],
        }

    return {"strategy_id": strategy_id, "symbols": results}
