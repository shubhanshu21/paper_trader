"""
api/routes_custom_strategies.py — Custom strategy CRUD and deployment API.

Supports creating, updating, deploying custom strategies with full
workflow from draft → backtesting → paper trading → live deployment.
"""
import asyncio
import json
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from automate.api.auth import get_current_user
from automate.db.engine import get_db, SessionLocal
from automate.db.models import CustomBacktestRun, CustomStrategy, CustomStrategyPosition
from automate.strategies.custom.rule_schema import validate_rules, describe_rules
from automate.utils.logger import get_logger

router = APIRouter(prefix="/api/custom-strategies", tags=["custom-strategies"])
log = get_logger(__name__)


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
    if strategy.instrument_type not in ("INDEX", "STOCK", "COMMODITY"):
        raise HTTPException(status_code=422, detail="This strategy builder only supports INDEX, STOCK, and COMMODITY.")
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

    # Editable in any state that isn't actively taking NEW entries —
    # PAPER_TRADING/LIVE must be paused or stopped first. Note PAUSED can
    # still have real OPEN legs (pausing only blocks new entries; already-
    # open legs stay exit-managed by the scheduler until they close or the
    # strategy is stopped — see custom_strategy_scheduler.py's module
    # docstring) — editing rules_json here only affects future entries;
    # each open leg's own exit config was already snapshotted into its
    # leg_config_json at entry time, so it's unaffected by this edit.
    if db_strategy.status not in ("DRAFT", "PAUSED", "BACKTESTING", "STOPPED"):
        raise HTTPException(status_code=400, detail="Can only update DRAFT, BACKTESTING, PAUSED, or STOPPED strategies")

    update_data = strategy.dict(exclude_unset=True)

    if "instrument_type" in update_data:
        if update_data["instrument_type"] not in ("INDEX", "STOCK", "COMMODITY"):
            raise HTTPException(status_code=422, detail="instrument_type must be INDEX, STOCK, or COMMODITY.")

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
    """Delete a custom strategy, along with all its positions and backtest run history."""
    db_strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))

    # Only allow deletion if in DRAFT or STOPPED status
    if db_strategy.status not in ("DRAFT", "STOPPED"):
        raise HTTPException(status_code=400, detail="Can only delete DRAFT or STOPPED strategies")

    # Explicitly delete children in the correct dependency order so this
    # works even when the DB FK constraints don't have ON DELETE CASCADE
    # configured (safer than relying solely on the DB schema).
    db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy_id
    ).delete(synchronize_session=False)
    db.query(CustomBacktestRun).filter(
        CustomBacktestRun.strategy_id == strategy_id
    ).delete(synchronize_session=False)

    db.delete(db_strategy)
    db.commit()

    return {"message": "Strategy deleted successfully"}


@router.patch("/{strategy_id}/status")
def update_strategy_status(strategy_id: int, status_update: StrategyStatusUpdate, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Update strategy status (workflow: DRAFT → BACKTESTING → PAPER_TRADING → LIVE)."""
    db_strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))

    valid_transitions = {
        # DRAFT -> PAPER_TRADING is only actually allowed below (see the
        # backtest_return_pct check) when a still-valid backtest already
        # exists — e.g. Stop -> Reactivate (STOPPED -> DRAFT) on an
        # unedited strategy shouldn't force a redundant re-backtest just
        # because status literally says DRAFT. update_strategy() (the
        # rules-edit endpoint) already clears backtest_return_pct back to
        # None whenever rules actually change, so its presence here is a
        # reliable "already validated against these exact rules" signal,
        # not a stale leftover.
        "DRAFT": ["BACKTESTING", "PAPER_TRADING", "STOPPED"],
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

    if current_status == "DRAFT" and new_status == "PAPER_TRADING" and db_strategy.backtest_return_pct is None:
        raise HTTPException(
            status_code=400,
            detail="This strategy hasn't been backtested yet — run a backtest before paper trading."
        )

    # Stopping a strategy must actually square off any open legs — the
    # frontend's confirm dialog already promises this ("square off any
    # open position?"), and once STOPPED, custom_strategy_scheduler.py's
    # main loop query excludes this strategy entirely, so any leg left
    # OPEN here would never be managed (or exited) again.
    if new_status == "STOPPED":
        has_open_legs = db.query(CustomStrategyPosition.id).filter(
            CustomStrategyPosition.strategy_id == db_strategy.id,
            CustomStrategyPosition.status == "OPEN",
        ).first() is not None
        if has_open_legs:
            from automate.api.custom_strategy_scheduler import _get_brokers, square_off_all_open_legs
            brokers = _get_brokers()
            if brokers is None:
                raise HTTPException(
                    status_code=503,
                    detail="Broker connection not ready — cannot square off open positions right now. Please try again shortly.",
                )
            square_off_all_open_legs(db, db_strategy, brokers)
            # Do NOT flip to STOPPED if any leg failed to close (broker
            # rejection, missing broker, etc.) — once STOPPED, this
            # strategy leaves the scheduler's main-loop query for good, so
            # a leftover OPEN leg here would never be managed again. Better
            # to fail the stop request loudly (strategy stays in its
            # current, still-scheduler-managed status) than silently
            # abandon a real position.
            still_open = db.query(CustomStrategyPosition.id).filter(
                CustomStrategyPosition.strategy_id == db_strategy.id,
                CustomStrategyPosition.status == "OPEN",
            ).count()
            if still_open:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"Failed to square off {still_open} open position(s) — strategy was NOT stopped and "
                        f"remains under active management. Check the alert notification for details, or retry."
                    ),
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
    slippage_pct: Optional[float] = 0.1


def _run_backtest_symbols(
    symbols: List[str],
    rules: dict,
    instrument_type: str,
    from_date: Optional[str],
    to_date: Optional[str],
    on_progress: Optional[Any] = None,
) -> tuple:
    """
    Run CustomRuleBacktestEngine once per symbol (a strategy can be
    configured against several — the old code silently only ever
    backtested symbols[0]), tag each cycle with its symbol, and merge them
    chronologically into one combined cycle list. A symbol whose
    instrument key can't be resolved (RuntimeError) is skipped rather than
    aborting the whole run, as long as at least one other symbol produces
    cycles.

    Returns (merged_cycles_sorted_by_entry_date, per_symbol_breakdown,
    skipped_symbols).
    """
    from automate.backtest.custom_engine import CustomRuleBacktestEngine

    option_instrument = "OPTIDX" if instrument_type == "INDEX" else "OPTSTK"
    future_instrument = "FUTIDX" if instrument_type == "INDEX" else "FUTSTK"

    all_cycles: List[dict] = []
    per_symbol: Dict[str, dict] = {}
    skipped_symbols: Dict[str, str] = {}

    for symbol in symbols:
        try:
            engine = CustomRuleBacktestEngine(
                symbol=symbol, rules=rules, option_instrument=option_instrument, future_instrument=future_instrument,
            )
            cycles = engine.run(from_date, to_date, on_progress=(
                (lambda done, total, sym=symbol: on_progress(sym, done, total)) if on_progress else None
            ))
        except RuntimeError as exc:
            skipped_symbols[symbol] = str(exc)
            continue

        if not cycles:
            skipped_symbols[symbol] = "No historical cycles produced a valid simulated trade in this date range."
            continue

        for c in cycles:
            c["symbol"] = symbol
        all_cycles.extend(cycles)
        wins = sum(1 for c in cycles if c["won"])
        per_symbol[symbol] = {
            "cycles_tested": len(cycles),
            "avg_return_pct": round(sum(c["pnl_pct_of_premium"] for c in cycles) / len(cycles), 2),
            "win_rate_pct": round(wins / len(cycles) * 100.0, 2),
        }

    all_cycles.sort(key=lambda c: c["entry_date"])
    return all_cycles, per_symbol, skipped_symbols


def _run_backtest_sync(run_id: int) -> None:
    """
    The actual (blocking) backtest computation — always run via
    `await asyncio.to_thread(_run_backtest_sync, run_id)` from
    `_execute_backtest_run`, never awaited directly, since
    CustomRuleBacktestEngine does synchronous SQLAlchemy/SQL work. Opens
    its own DB session (the request's `db: Session = Depends(get_db)` is
    closed by the time this runs in the background) and NEVER lets an
    exception escape uncaught — always leaves the CustomBacktestRun row in a
    terminal COMPLETED/FAILED state, matching this codebase's other
    background-task error-handling convention (see
    custom_strategy_scheduler.py).
    """
    from automate.backtest.custom_engine import compute_nifty_benchmark_return
    from automate.utils.backtest_stats import compute_backtest_stats

    db = SessionLocal()
    try:
        run = db.query(CustomBacktestRun).filter(CustomBacktestRun.id == run_id).first()
        if run is None:
            log.error("backtest run %s: row vanished before execution started.", run_id)
            return

        run.status = "RUNNING"
        db.commit()

        rules = json.loads(run.rules_snapshot_json)
        db_strategy = db.query(CustomStrategy).filter(CustomStrategy.id == run.strategy_id).first()
        symbols = json.loads(db_strategy.symbols) if db_strategy else []

        # Coarse per-symbol progress: total = len(symbols) * 100, each
        # symbol's own cycle-count contributes proportionally — a strategy
        # backtest doesn't know its total cycle count across ALL symbols
        # up front (discover_cycles() runs per-symbol, inside the engine),
        # so this is an approximation, refined as each symbol finishes.
        run.progress_total = max(len(symbols), 1) * 100
        completed_symbols = {"n": 0}

        def on_progress(symbol: str, done: int, total: int) -> None:
            frac = (done / total) if total else 1.0
            run.progress_current = int((completed_symbols["n"] + frac) * 100)
            # Commit every 5th cycle (or the symbol's last one) rather than
            # every single cycle — a 20+ year monthly history is ~250
            # cycles; no need to round-trip the DB that often just for a
            # progress bar.
            if done % 5 == 0 or done >= total:
                db.commit()
            if total and done >= total:
                completed_symbols["n"] += 1

        cycles, per_symbol, skipped_symbols = _run_backtest_symbols(
            symbols, rules, db_strategy.instrument_type if db_strategy else "STOCK",
            run.from_date, run.to_date, on_progress=on_progress,
        )

        if not cycles:
            reasons = "; ".join(f"{s}: {r}" for s, r in skipped_symbols.items()) or "no symbols configured"
            raise RuntimeError(f"No historical cycles produced a valid simulated trade. {reasons}")

        first_entry = cycles[0]["entry_date"]
        last_exit = max(c["exit_date"] for c in cycles)
        benchmark_return_pct = compute_nifty_benchmark_return(first_entry, last_exit)

        avg_return_pct = sum(c["pnl_pct_of_premium"] for c in cycles) / len(cycles)
        win_rate = sum(1 for c in cycles if c["won"]) / len(cycles) * 100.0
        run_at = datetime.now()

        result = {
            "strategy_id": run.strategy_id,
            "run_id": run.id,
            "cycles_tested": len(cycles),
            "avg_return_pct_of_premium": round(avg_return_pct, 2),
            "win_rate_pct": round(win_rate, 2),
            "from_date": run.from_date,
            "to_date": run.to_date,
            "run_at": run_at.isoformat(),
            "cycles": cycles,
            "per_symbol": per_symbol,
            "skipped_symbols": skipped_symbols,
            **compute_backtest_stats(cycles, rules=rules, benchmark_return_pct=benchmark_return_pct),
        }

        run.status = "COMPLETED"
        run.progress_current = run.progress_total or 100
        run.result_json = json.dumps(result)
        run.completed_at = run_at
        db.commit()

        if db_strategy is not None:
            db_strategy.backtest_return_pct = round(avg_return_pct, 4)
            # Overwrites any previous run for this strategy — the single
            # "latest" result GET /{id}/backtest reads; full history lives
            # in backtest_runs, see GET /{id}/backtest/runs.
            db_strategy.backtest_result_json = json.dumps(result)
            db_strategy.backtest_run_at = run_at
            if db_strategy.status == "DRAFT":
                db_strategy.status = "BACKTESTING"
            db.commit()

    except Exception as exc:
        db.rollback()
        run = db.query(CustomBacktestRun).filter(CustomBacktestRun.id == run_id).first()
        if run is not None:
            run.status = "FAILED"
            run.error_message = str(exc)
            run.completed_at = datetime.now()
            db.commit()
        log.error("backtest run %s failed: %s", run_id, exc, exc_info=True)
    finally:
        db.close()


async def _execute_backtest_run(run_id: int) -> None:
    await asyncio.to_thread(_run_backtest_sync, run_id)


@router.post("/{strategy_id}/backtest", status_code=202)
async def backtest_strategy(strategy_id: int, request: BacktestRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """
    Queue a backtest of this strategy's rules against real historical NSE
    F&O bhavcopy data (one simulated trade per historical expiry cycle in
    range, across every configured symbol) and return immediately with a
    run_id — the actual computation runs in the background (see
    _execute_backtest_run) so a long history doesn't tie up the request
    or risk a timeout. Poll GET /{id}/backtest/runs/{run_id} for progress
    and the final result. See backtest/custom_engine.py for methodology
    caveats (daily EOD data, simulated slippage, not tick-accurate).
    """
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
    rules_for_check = json.loads(db_strategy.rules_json)
    if rules_for_check.get("legs") and all((leg.get("instrument_type") or "OPTION") == "EQUITY" for leg in rules_for_check["legs"]):
        raise HTTPException(
            status_code=400,
            detail="Backtesting isn't available for an all-EQUITY strategy — the backtest engine's cycle "
                   "model is expiry-driven (options only); a plain equity leg has no expiry to anchor a "
                   "cycle to. Paper/live trading works for equity legs; add at least one OPTION leg to "
                   "backtest, or paper-trade this strategy directly.",
        )

    rules_for_check["slippage_pct"] = request.slippage_pct
    rules_snapshot = json.dumps(rules_for_check)
    run = CustomBacktestRun(
        strategy_id=strategy_id, user_id=_current_user_id(user), status="QUEUED",
        from_date=request.from_date, to_date=request.to_date, rules_snapshot_json=rules_snapshot,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    asyncio.create_task(_execute_backtest_run(run.id))

    return {"run_id": run.id, "status": run.status}


@router.get("/{strategy_id}/backtest/runs")
def list_backtest_runs(strategy_id: int, limit: int = 20, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Run history for this strategy, newest first — lets the UI list past runs and pick two to compare."""
    _get_owned_strategy(db, strategy_id, _current_user_id(user))  # ownership check (404s if not owned)
    runs = (
        db.query(CustomBacktestRun)
        .filter(CustomBacktestRun.strategy_id == strategy_id, CustomBacktestRun.user_id == _current_user_id(user))
        .order_by(CustomBacktestRun.created_at.desc())
        .limit(min(limit, 100))
        .all()
    )
    return {"runs": [r.to_dict(include_result=False) for r in runs]}


@router.get("/{strategy_id}/backtest/runs/{run_id}")
def get_backtest_run(strategy_id: int, run_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Poll a single run's status/progress/result — used both while RUNNING and to view/compare a completed run."""
    _get_owned_strategy(db, strategy_id, _current_user_id(user))  # ownership check (404s if not owned)
    run = db.query(CustomBacktestRun).filter(
        CustomBacktestRun.id == run_id, CustomBacktestRun.strategy_id == strategy_id, CustomBacktestRun.user_id == _current_user_id(user),
    ).first()
    if not run:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return run.to_dict(include_result=True)


@router.get("/{strategy_id}/backtest")
def get_stored_backtest_result(strategy_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """
    The most recently COMPLETED backtest result for this strategy,
    persisted — lets the UI show "View Backtest Results" anytime without
    re-running, and survive a page reload/navigating away and back.
    Returns 404 if this strategy has never completed a backtest.
    """
    db_strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))
    if not db_strategy.backtest_result_json:
        raise HTTPException(status_code=404, detail="This strategy hasn't been backtested yet.")
    return json.loads(db_strategy.backtest_result_json)


def _leg(action: str, option_type: str, mode: str = "ATM", value: Optional[float] = None, lots: int = 1) -> dict:
    return {"action": action, "option_type": option_type, "strike_selection": {"mode": mode, "value": value}, "lots": lots}


@router.get("/templates/strategy-types")
def get_strategy_types():
    """
    Available strategy templates with descriptions AND real, ready-to-use
    leg specs — in the actual rules-schema shape (action/option_type/
    strike_selection/lots, see rule_schema.py), not a display-only
    approximation. Each template's `legs` passes validate_rules() as-is
    (see tests/test_custom_strategy_templates.py) — the frontend's
    template picker feeds this straight into the strategy builder's leg
    state, no translation layer. Strike distances are sensible starting
    points, not locked-in — the builder's Step 2 lets the user adjust
    them like any other leg.
    """
    return {
        "strategy_types": [
            {
                "type": "STRADDLE",
                "description": "Buy ATM call and put — profits from a big move in either direction, loses if the underlying stays flat.",
                "risk_level": "high",
                "legs": [_leg("BUY", "CE"), _leg("BUY", "PE")],
            },
            {
                "type": "STRANGLE",
                "description": "Buy OTM call and put — cheaper than a straddle, needs an even bigger move to profit.",
                "risk_level": "medium",
                "legs": [_leg("BUY", "CE", "OTM_PERCENT", 5), _leg("BUY", "PE", "OTM_PERCENT", 5)],
            },
            {
                "type": "IRON_CONDOR",
                "description": "Sell a near OTM put + call, buy further OTM put + call as protection — defined-risk income from a range-bound market.",
                "risk_level": "medium",
                "legs": [
                    _leg("SELL", "PE", "OTM_PERCENT", 5), _leg("BUY", "PE", "OTM_PERCENT", 10),
                    _leg("SELL", "CE", "OTM_PERCENT", 5), _leg("BUY", "CE", "OTM_PERCENT", 10),
                ],
            },
            {
                "type": "BUTTERFLY",
                "description": "Buy ATM call, sell 2x a bit further OTM, buy 1 more further OTM as protection — defined, low-cost risk for a pinned/range-bound view. Adjust the wing distances to taste.",
                "risk_level": "low",
                "legs": [
                    _leg("BUY", "CE"),
                    _leg("SELL", "CE", "OTM_PERCENT", 5, lots=2),
                    _leg("BUY", "CE", "OTM_PERCENT", 10),
                ],
            },
            {
                "type": "CUSTOM",
                "description": "Start from a single leg and build any combination yourself.",
                "risk_level": "variable",
                "legs": [_leg("SELL", "CE")],
            },
        ]
    }


@router.get("/templates/instrument-types")
def get_instrument_types():
    """Get available instrument types."""
    return {
        "instrument_types": [
            {"type": "INDEX", "description": "Index options (NIFTY, BANKNIFTY, etc.)"},
            {"type": "STOCK", "description": "Stock options"},
            {"type": "COMMODITY", "description": "MCX commodity options (GOLD, CRUDEOIL, etc.) — live/paper trading only, no backtesting yet"},
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
    opt = df[(df["instrument_type"].isin(["OPTIDX", "OPTSTK", "OPTFUT"])) & (df["symbol"].astype(str).str.match(pattern))]
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


@router.get("/portfolio/greeks")
def get_portfolio_greeks(user: dict = Depends(get_current_user)):
    """
    Net Black-76 Greeks across EVERY open leg of EVERY active strategy
    this user owns — see api/live_greeks.py::compute_portfolio_greeks for
    why this is a materially different (and more useful) number than any
    single strategy's own combined Greeks. Registered ABOVE
    /{strategy_id}/greeks below — Starlette matches routes in
    registration order, so "portfolio" would otherwise be swallowed by
    that route's int strategy_id path param and 422.
    """
    from automate.api.live_greeks import compute_portfolio_greeks
    return compute_portfolio_greeks(_current_user_id(user))


@router.get("/{strategy_id}/greeks")
def get_live_greeks(strategy_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """
    One-off Black-76 Greeks snapshot (see utils/black76.py) for manual/curl
    inspection — the Strategies page itself uses the live-updating
    /ws/custom-strategy-greeks/{id} WebSocket instead (see
    ws_custom_strategy_greeks.py), not this endpoint. Both call the same
    shared computation (api/live_greeks.py) so there's one implementation.
    """
    from automate.api.live_greeks import compute_live_greeks

    payload = compute_live_greeks(strategy_id, owner_user_id=_current_user_id(user))
    if payload is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return payload


@router.get("/{strategy_id}/payoff")
def get_expected_payoff(strategy_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
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
    from automate.utils.payoff import compute_payoff, compute_payoff_curve, probability_of_profit
    from automate.utils import black76
    from automate.utils.margin import estimate_margin_blocked
    from automate.api.custom_strategy_scheduler import _get_brokers, _audit, _kill_switch, _rate_limiter

    db_strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))
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
            "capital_basis": round(capital_basis, 2) if capital_basis else None,
            "risk_reward_ratio": risk_reward_ratio,
            "probability_of_profit_pct": round(pop * 100, 2) if pop is not None else None,
            "breakevens_detail": [
                {"price": b, "pct_from_spot": round((b - spot) / spot * 100, 2)} for b in payoff["breakevens"]
            ],
            "payoff_curve": compute_payoff_curve(payoff_legs, spot),
            "spot_price": spot,
            "expiry": preview["expiry"],
            "legs": [
                {"strike": l["strike"], "option_type": l["option_type"], "action": l["transaction_type"],
                 "quantity": l["quantity"], "current_price": l["current_price"]}
                for l in preview["legs"]
            ],
        }

    return {"strategy_id": strategy_id, "symbols": results}


@router.post("/positions/{position_id}/close")
def close_custom_strategy_position(position_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Square off/close a single open leg of a custom strategy."""
    from automate.api.custom_strategy_scheduler import _get_brokers, _close_leg

    # 1. Fetch the leg
    leg = db.query(CustomStrategyPosition).filter(CustomStrategyPosition.id == position_id).first()
    if not leg:
        raise HTTPException(status_code=404, detail="Position not found.")

    # 2. Fetch the strategy and verify ownership
    strategy = db.query(CustomStrategy).filter(
        CustomStrategy.id == leg.strategy_id,
        CustomStrategy.user_id == _current_user_id(user)
    ).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Position not found.")

    # 3. Check status
    if leg.status != "OPEN":
        raise HTTPException(status_code=400, detail="Position is already closed.")

    # 4. Get brokers
    brokers = _get_brokers()
    if not brokers:
        raise HTTPException(status_code=503, detail="Broker connection not ready yet — try again shortly.")

    mode = leg.mode or "paper"
    broker = brokers.get(mode)
    if not broker:
        raise HTTPException(status_code=500, detail=f"Broker for mode '{mode}' is not configured.")

    # 5. Get LTP for exit price
    try:
        ltp = broker.get_ltp(leg.instrument_key)
    except Exception:
        ltp = None
    now_prices = {}
    if ltp is not None:
        now_prices[leg.instrument_key] = ltp

    # 6. Close the leg
    success = _close_leg(db, strategy, broker, leg, "MANUAL_CLOSE", now_prices)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to place close order with the broker.")

    return {"message": "Position squared off successfully", "exit_price": leg.exit_price, "order_id": leg.exit_order_id}


@router.get("/{strategy_id}/positions")
def get_strategy_positions(strategy_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """
    Real position/order history for this strategy — every leg the
    scheduler (api/custom_strategy_scheduler.py) has actually entered,
    OPEN or CLOSED, with real entry/exit fill prices and order ids. This
    is the "what actually happened" surface — distinct from /greeks (live
    mark-to-market on currently open legs only) and /payoff (a what-if
    calculation from current option prices, works even pre-deployment).
    """
    strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))

    legs = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy.id
    ).order_by(CustomStrategyPosition.opened_at.desc(), CustomStrategyPosition.leg_index.asc()).all()

    return {
        "strategy_id": strategy_id,
        "open": [l.to_dict() for l in legs if l.status == "OPEN"],
        "closed": [l.to_dict() for l in legs if l.status == "CLOSED"],
    }


@router.get("/positions/open")
def get_all_open_positions(user: dict = Depends(get_current_user)):
    """
    One-off snapshot of open custom-strategy positions for manual/curl
    inspection — the Positions page itself uses the live-updating
    /ws/custom-strategy-positions WebSocket instead (see
    ws_custom_strategy_positions.py). Both call the same shared
    computation (api/live_positions.py) so there's one implementation.

    NOTE: This route MUST be registered before /{strategy_id}/positions
    in this file so FastAPI does not attempt to coerce the literal path
    segment "positions" into an integer strategy_id (which would 422).
    It appears here at the end of the file but the router scans by
    registration order — this GET has no path parameter so Starlette's
    router resolves it before any /{strategy_id}/* wildcard patterns.
    """
    from automate.api.live_positions import compute_open_positions
    return {"rows": compute_open_positions(_current_user_id(user))}
