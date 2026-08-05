"""
api/routes_custom_strategies.py — Custom strategy CRUD and deployment API.

Supports creating, updating, deploying custom strategies with full
workflow from draft → backtesting → paper trading → live deployment.
"""
import asyncio
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.auth import get_current_user
from db.engine import SessionLocal, get_db
from db.models import CustomBacktestRun, CustomStrategy, CustomStrategyPosition
from strategies.custom.engine_registry import get_engine
from utils.logger import get_logger

router = APIRouter(prefix="/api/custom-strategies", tags=["custom-strategies"])
log = get_logger(__name__)

# Kept referenced for the task's lifetime — an unreferenced asyncio task can be
# garbage-collected mid-run since the event loop only holds a weak reference.
_background_tasks: set = set()


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
    description: str | None = None
    instrument_type: str  # 'INDEX' | 'STOCK' | 'COMMODITY'
    symbols: list[str]
    rules: dict[str, Any]
    strategy_type: str = "CUSTOM"
    option_type: str = "BOTH"
    strike_offset: float | None = None
    expiry_days: int | None = None
    num_lots: int = 1
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    exit_days_before_expiry: int = 1


class CustomStrategyUpdate(BaseModel):
    """Request model for updating a custom strategy."""
    name: str | None = None
    description: str | None = None
    symbols: list[str] | None = None
    rules: dict[str, Any] | None = None
    strategy_type: str | None = None
    option_type: str | None = None
    strike_offset: float | None = None
    expiry_days: int | None = None
    num_lots: int | None = None
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    exit_days_before_expiry: int | None = None


class StrategyStatusUpdate(BaseModel):
    """Request model for updating strategy status."""
    status: str  # 'DRAFT' | 'BACKTESTING' | 'PAPER_TRADING' | 'LIVE' | 'PAUSED' | 'STOPPED'


class AutomationSettings(BaseModel):
    """Request model for updating automation settings."""
    auto_roll: bool = False
    roll_threshold_pct: float | None = None
    auto_adjust: bool = False
    greek_threshold_delta: float | None = None
    greek_threshold_theta: float | None = None


class PerformanceUpdate(BaseModel):
    """Request model for updating performance metrics."""
    backtest_return_pct: float | None = None
    paper_return_pct: float | None = None
    live_return_pct: float | None = None


@router.post("")
def create_strategy(strategy: CustomStrategyCreate, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Create a new custom strategy."""
    if not strategy.symbols:
        raise HTTPException(status_code=422, detail="At least one symbol is required.")
    if strategy.instrument_type not in ("INDEX", "STOCK", "COMMODITY"):
        raise HTTPException(status_code=422, detail="This strategy builder only supports INDEX, STOCK, and COMMODITY.")

    # Each strategy_type validates/describes its rules via its own engine
    # (a genuinely different rules shape — see engine_registry.py and each
    # schema module's own docstring for why) — CUSTOM/legacy types fall
    # through to the leg-based default there.
    engine = get_engine(strategy.strategy_type)
    errors = engine["validate"](strategy.rules)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    description = strategy.description or engine["describe"](strategy.rules, strategy.symbols[0])

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
    status: str | None = None,
    instrument_type: str | None = None,
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

    if "instrument_type" in update_data and update_data["instrument_type"] not in ("INDEX", "STOCK", "COMMODITY"):
        raise HTTPException(status_code=422, detail="instrument_type must be INDEX, STOCK, or COMMODITY.")

    if "symbols" in update_data:
        update_data["symbols"] = json.dumps(update_data["symbols"])

    if "rules" in update_data:
        rules = update_data.pop("rules")
        errors = get_engine(db_strategy.strategy_type)["validate"](rules)
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

    # The backtest-first gate only makes sense for strategies that CAN
    # actually be backtested — engines with backtest_supported=False (no
    # historical intraday/multi-symbol data source yet) and COMMODITY
    # (backtest data is NSE F&O only) are all rejected outright by
    # POST /{id}/backtest below, so requiring backtest_return_pct here
    # would strand them at DRAFT forever with no way to ever reach
    # PAPER_TRADING. Skip the gate for exactly those cases — every other
    # strategy still needs a real backtest first.
    backtest_unavailable = not get_engine(db_strategy.strategy_type)["backtest_supported"] or db_strategy.instrument_type == "COMMODITY"
    if current_status == "DRAFT" and new_status == "PAPER_TRADING" and db_strategy.backtest_return_pct is None and not backtest_unavailable:
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
            from api.custom_strategy_scheduler import (
                _get_brokers,
                square_off_all_open_legs,
            )
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
    from_date: str | None = None  # 'YYYY-MM-DD'
    to_date: str | None = None
    slippage_pct: float | None = 0.1


def _run_backtest_symbols(
    symbols: list[str],
    rules: dict,
    instrument_type: str,
    from_date: str | None,
    to_date: str | None,
    on_progress: Any | None = None,
    charge_rates: dict | None = None,
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
    from backtest.custom_engine import CustomRuleBacktestEngine

    option_instrument = "OPTIDX" if instrument_type == "INDEX" else "OPTSTK"
    future_instrument = "FUTIDX" if instrument_type == "INDEX" else "FUTSTK"

    all_cycles: list[dict] = []
    per_symbol: dict[str, dict] = {}
    skipped_symbols: dict[str, str] = {}

    for symbol in symbols:
        try:
            engine = CustomRuleBacktestEngine(
                symbol=symbol, rules=rules, option_instrument=option_instrument, future_instrument=future_instrument,
                charge_rates=charge_rates,
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
    from backtest.custom_engine import compute_nifty_benchmark_return
    from utils.backtest_stats import compute_backtest_stats

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

        from utils.wallet import get_charge_rates
        charge_rates = get_charge_rates(db_strategy.user_id) if db_strategy else None

        cycles, per_symbol, skipped_symbols = _run_backtest_symbols(
            symbols, rules, db_strategy.instrument_type if db_strategy else "STOCK",
            run.from_date, run.to_date, on_progress=on_progress, charge_rates=charge_rates,
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
    engine = get_engine(db_strategy.strategy_type)
    if not engine["backtest_supported"]:
        raise HTTPException(status_code=400, detail=engine["backtest_unavailable_reason"])
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

    task = asyncio.create_task(_execute_backtest_run(run.id))
    if task is not None:  # tests monkeypatch create_task to a no-op returning None
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

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


def _leg(action: str, option_type: str, mode: str = "ATM", value: float | None = None, lots: int = 1) -> dict:
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
                "type": "OVERHEDGED_MONTHLY_IRON_FLY",
                "description": (
                    "Buy 1 lot ATM straddle, sell a 2-lot strangle offset by half the straddle's own live "
                    "premium (rounded to the nearest strike), then over-hedge with 2 lots of deep-OTM ₹40-₹70 "
                    "insurance on each side — a low-touch monthly income structure with defined-ish risk. "
                    "Exits on a flat ₹4,500 profit or the underlying crossing the position's own computed "
                    "breakeven; review the wing lot counts (2 vs 3 a side) once you see the actual entry "
                    "T+0 curve — an asymmetric skew may need an extra lot on one side to flatten it, same as "
                    "any over-hedge."
                ),
                "risk_level": "medium",
                "legs": [
                    {"action": "BUY", "option_type": "CE", "strike_selection": {"mode": "ATM", "value": None}, "lots": 1},
                    {"action": "BUY", "option_type": "PE", "strike_selection": {"mode": "ATM", "value": None}, "lots": 1},
                    {"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "PREMIUM_OFFSET", "value": 2}, "lots": 2},
                    {"action": "SELL", "option_type": "PE", "strike_selection": {"mode": "PREMIUM_OFFSET", "value": 2}, "lots": 2},
                    {"action": "BUY", "option_type": "CE", "strike_selection": {"mode": "PREMIUM_BAND", "value": None, "min": 40, "max": 70}, "lots": 2},
                    {"action": "BUY", "option_type": "PE", "strike_selection": {"mode": "PREMIUM_BAND", "value": None, "min": 40, "max": 70}, "lots": 2},
                ],
                # Suggested entry/expiry/exit for this template — the
                # frontend template picker applies these too (not just
                # `legs`), since "enter on the monthly cycle at 10am,
                # exit on a flat ₹4,500 or a breakeven breach" is as much
                # a part of this strategy's shape as its legs are.
                "entry": {"mode": "AT_TIME", "time": "10:00"},
                "expiry": {"mode": "MONTHLY"},
                "exit": {
                    "take_profit_pct": None, "stop_loss_pct": None,
                    "take_profit_amount": 4500, "stop_loss_mode": "BREAKEVEN",
                    "exit_time": None, "exit_days_before_expiry": 0,
                },
            },
            {
                "type": "MONTHLY_BATMAN_1_3_2",
                "description": (
                    "1:3:2 call + put ratio spreads (buy 1 lot 400pt OTM, sell 3 lots 600pt OTM, buy 2 lots "
                    "1400pt OTM hedge, on both sides) — a symmetric, non-directional payoff shaped like the "
                    "Batman logo. Two things to check BEFORE deploying each cycle, once premiums are live: "
                    "(1) the net credit at the payoff's center valley should stay under ~6% of deployed "
                    "capital — if IV is elevated (budget/earnings), shift all three buy/sell distances "
                    "outward together (keep the 200pt and 800pt gaps fixed) until it drops back under 6%; "
                    "(2) balance the T+0 curve — raise the outer hedge lots (2 → 3 or 4) on whichever side "
                    "shows the bigger max loss, until both sides are roughly equal. Exits on ±2%/-2.5% of "
                    "the REAL broker margin blocked for this basket at entry, or monthly expiry."
                ),
                "risk_level": "medium",
                "legs": [
                    {"action": "BUY", "option_type": "CE", "strike_selection": {"mode": "OTM_POINTS", "value": 400}, "lots": 1},
                    {"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "OTM_POINTS", "value": 600}, "lots": 3},
                    {"action": "BUY", "option_type": "CE", "strike_selection": {"mode": "OTM_POINTS", "value": 1400}, "lots": 2},
                    {"action": "BUY", "option_type": "PE", "strike_selection": {"mode": "OTM_POINTS", "value": 400}, "lots": 1},
                    {"action": "SELL", "option_type": "PE", "strike_selection": {"mode": "OTM_POINTS", "value": 600}, "lots": 3},
                    {"action": "BUY", "option_type": "PE", "strike_selection": {"mode": "OTM_POINTS", "value": 1400}, "lots": 2},
                ],
                # "Last Friday before expiry week at 3:16pm" — BEFORE_EXPIRY
                # opens a 10-day window ahead of the resolved monthly expiry,
                # preferring Friday, with a built-in fallback so a holiday on
                # that Friday never skips the whole month (see
                # custom_strategy_scheduler.py::_before_expiry_eligible).
                "entry": {"mode": "BEFORE_EXPIRY", "time": None, "before_expiry": {"days_before_expiry": 10, "weekday": "FRI", "time": "15:16"}},
                "expiry": {"mode": "MONTHLY"},
                "exit": {
                    "take_profit_pct": None, "stop_loss_pct": None, "take_profit_amount": None,
                    "take_profit_capital_pct": 2.0, "stop_loss_capital_pct": 2.5, "stop_loss_mode": "PCT",
                    "exit_time": None, "exit_days_before_expiry": 0,
                },
            },
            {
                "type": "NO_DECAY_WEEKLY_RATIO_SPREAD",
                "description": (
                    "Sell 1 lot ATM straddle (CE+PE), buy 2 lots OTM CE + 2 lots OTM PE ~200-300pts away — "
                    "double-quantity long wings blunt theta in a flat market while a sharp break either way lets "
                    "the 2x OTM long legs outrun the 1x ATM short and turn net profitable. Check the live payoff "
                    "graph before each entry: if either side shows a loss in the non-moving zone, push that "
                    "side's OTM strike out another 50-100pts until both sides flatten. There is still a real risk "
                    "zone between the ATM and OTM strikes on a MODERATE (not flat, not sharp) move — this is "
                    "inherent to the structure, not a bug, which is why stop_loss_capital_pct is on by default here."
                ),
                "risk_level": "medium",
                "legs": [
                    {"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "ATM", "value": None}, "lots": 1},
                    {"action": "SELL", "option_type": "PE", "strike_selection": {"mode": "ATM", "value": None}, "lots": 1},
                    {"action": "BUY", "option_type": "CE", "strike_selection": {"mode": "OTM_POINTS", "value": 250}, "lots": 2},
                    {"action": "BUY", "option_type": "PE", "strike_selection": {"mode": "OTM_POINTS", "value": 250}, "lots": 2},
                ],
                # "Active trading days, ~9:30am after opening volatility
                # settles" — AT_TIME (not BEFORE_EXPIRY) since this isn't
                # tied to being near expiry, just once per weekly cycle.
                "entry": {"mode": "AT_TIME", "time": "09:30"},
                "expiry": {"mode": "WEEKLY"},
                "exit": {
                    "take_profit_pct": None, "stop_loss_pct": None, "take_profit_amount": None,
                    "take_profit_capital_pct": 40.0, "stop_loss_capital_pct": 8.0, "stop_loss_mode": "PCT",
                    "exit_time": None, "exit_days_before_expiry": 0,
                },
            },
            {
                "type": "WEEKLY_HEDGED_PUT_RATIO",
                "description": (
                    "Fully-hedged put ratio (broken-wing butterfly): BUY 1 lot near-OTM PE (closer to spot) + "
                    "SELL 2 lots further-OTM PE (the credit engine) + BUY 1 lot deep-OTM PE (tail hedge) — every "
                    "leg hedged, so margin stays well below naked put-selling and the wide downside breakeven "
                    "gives a high probability of profit. Base case (market flat/up): small theta-decay credit. "
                    "Best case: the underlying drifts down toward the sold strike by expiry, where the profit "
                    "peaks — this same 'peak' also cushions a late, sharp gamma spike that would hurt an "
                    "unhedged put seller. Manual tuning each cycle (not automated by this engine): start with "
                    "the sold strike far OTM for maximum safety, then as the week progresses with the market "
                    "flat, you can shift the sold strike 50-100pts closer to spot to collect more credit — "
                    "review the live payoff graph before doing so. Works the same way on a monthly expiry for "
                    "a wider (but slower) breakeven cushion — just change Expiry to Monthly in the builder."
                ),
                "risk_level": "medium",
                "legs": [
                    {"action": "BUY", "option_type": "PE", "strike_selection": {"mode": "OTM_POINTS", "value": 150}, "lots": 1},
                    {"action": "SELL", "option_type": "PE", "strike_selection": {"mode": "OTM_POINTS", "value": 750}, "lots": 2},
                    {"action": "BUY", "option_type": "PE", "strike_selection": {"mode": "OTM_POINTS", "value": 1400}, "lots": 1},
                ],
                "entry": {"mode": "AT_TIME", "time": "09:30"},
                "expiry": {"mode": "WEEKLY"},
                "exit": {
                    "take_profit_pct": None, "stop_loss_pct": None, "take_profit_amount": None,
                    "take_profit_capital_pct": 1.0, "stop_loss_capital_pct": 1.0, "stop_loss_mode": "PCT",
                    "exit_time": None, "exit_days_before_expiry": 0,
                },
            },
            {
                "type": "HAI_WEEKLY_1_3_2_CALL_RATIO",
                "description": (
                    "H.A.I. weekly call ratio spread: BUY 1 lot 200pt OTM CE + SELL 3 lots 400pt OTM CE + BUY 2 "
                    "lots 600pt OTM CE — all-CALL, so a downside gap/crash carries no black-swan risk (capped at "
                    "a small net credit/debit); the 1(bought)+2(bought)=3(sold) balance caps the upside too, "
                    "unlike a naked ratio spread. Zero adjustments — strictly rule-based. Trades next week's "
                    "Tuesday expiry (entered every Monday, avoiding an unintentionally short-DTE contract), "
                    "exits by Friday 3:15pm regardless — no weekend carry, ever. Before entering each cycle: "
                    "check the net credit collected stays under ~0.5-0.6% of deployed capital; if NIFTY VIX > 20 "
                    "is inflating premiums, widen all three strike distances together (e.g. 400/800/1200pt) to "
                    "bring it back down — the builder's Step 2 lets you adjust these like any other leg."
                ),
                "risk_level": "medium",
                "legs": [
                    {"action": "BUY", "option_type": "CE", "strike_selection": {"mode": "OTM_POINTS", "value": 200}, "lots": 1},
                    {"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "OTM_POINTS", "value": 400}, "lots": 3},
                    {"action": "BUY", "option_type": "CE", "strike_selection": {"mode": "OTM_POINTS", "value": 600}, "lots": 2},
                ],
                "entry": {"mode": "AT_TIME", "time": "09:45", "weekday": "MON"},
                "expiry": {"mode": "WEEKLY", "expiry_offset": 1},
                "exit": {
                    "take_profit_pct": None, "stop_loss_pct": None, "take_profit_amount": None,
                    "take_profit_capital_pct": 1.0, "stop_loss_capital_pct": 1.0, "stop_loss_mode": "PCT",
                    "exit_time": "15:15", "exit_weekday": "FRI", "exit_days_before_expiry": 0,
                },
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
    from utils.instrument_cache import InstrumentCache
    try:
        return InstrumentCache().list_tradable_symbols()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not load instrument master: {exc}") from exc


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
    from utils.instrument_cache import InstrumentCache

    try:
        df = InstrumentCache().get_or_refresh()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not load instrument master: {exc}") from exc

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

    by_month: dict[Any, str] = {}
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
    from api.live_greeks import compute_portfolio_greeks
    return compute_portfolio_greeks(_current_user_id(user))


@router.get("/portfolio/margin")
def get_portfolio_margin(user: dict = Depends(get_current_user)):
    """
    Real broker-netted margin (₹) currently blocked across every open leg
    of every active strategy this user owns, split by paper vs live — see
    api/live_greeks.py::compute_portfolio_margin. Registered above
    /{strategy_id}/margin for the same routing reason as /portfolio/greeks
    above (Starlette matches in registration order — "portfolio" would
    otherwise 422 against that route's int strategy_id path param).
    """
    from api.live_greeks import compute_portfolio_margin
    return compute_portfolio_margin(_current_user_id(user))


@router.get("/{strategy_id}/greeks")
def get_live_greeks(strategy_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """
    One-off Black-76 Greeks snapshot (see utils/black76.py) for manual/curl
    inspection — the Strategies page itself uses the live-updating
    /ws/custom-strategy-greeks/{id} WebSocket instead (see
    ws_custom_strategy_greeks.py), not this endpoint. Both call the same
    shared computation (api/live_greeks.py) so there's one implementation.
    """
    from api.live_greeks import compute_live_greeks

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

    from api.custom_strategy_scheduler import (
        _audit,
        _get_brokers,
        _kill_switch,
        _rate_limiter,
    )
    from strategies.custom.rule_strategy import RuleBasedStrategy, resolve_leg_strike
    from utils import black76
    from utils.margin import estimate_margin_blocked
    from utils.option_utils import find_instrument_token, find_leg_iv
    from utils.payoff import compute_payoff, compute_payoff_curve, probability_of_profit

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
        from utils.instrument_cache import InstrumentCache
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

        def _iv_for_bound(bound_price: float) -> float | None:
            # A tail above the underlying's current forward is really about
            # whether the CALL side finishes ITM, and a tail below is about
            # the PUT side — use that leg's own IV for that side's mass.
            # (call_iv/forward/put_iv are this iteration's values — safe since
            # probability_of_profit() below calls this synchronously, in the
            # same iteration, before the loop can reassign them.)
            return call_iv if bound_price >= forward else put_iv  # noqa: B023

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
        short_qty = max((leg["quantity"] for leg in payoff_legs if leg["action"] == "SELL"), default=0)
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
                {"strike": leg["strike"], "option_type": leg["option_type"], "action": leg["transaction_type"],
                 "quantity": leg["quantity"], "current_price": leg["current_price"]}
                for leg in preview["legs"]
            ],
        }

    return {"strategy_id": strategy_id, "symbols": results}


@router.get("/{strategy_id}/margin")
def get_required_margin(strategy_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """
    Real margin (₹) needed to deploy this strategy's legs right now, plus
    whether the user currently has enough — checked against BOTH capital
    pools independently, since they're genuinely separate: the per-user
    simulated paper wallet (utils/wallet.py::get_wallet_summary) and the
    one real Upstox account's actual funds (UpstoxBroker.get_available_
    funds — only ever the single account this app is configured against,
    not per-user). A strategy only ever trades in ONE of these at a time
    (its own status), but showing both up front lets the user see if
    they're ready for either before switching modes.

    Margin itself is computed via the real broker Margin Calculator basket
    call (netted across every leg — see get_basket_required_margin), same
    "real number when available" tier as get_expected_payoff's capital_basis,
    but the ACTUAL exchange figure here, not a flat-rate approximation —
    falling back to the flat-rate estimate (summed per leg) only if the
    basket call itself is unavailable (e.g. a transient API failure).
    """
    from api.custom_strategy_scheduler import (
        _audit,
        _get_brokers,
        _kill_switch,
        _rate_limiter,
    )
    from strategies.custom.rule_strategy import RuleBasedStrategy
    from utils.margin import is_commodity_instrument_key, resolve_required_margin
    from utils.wallet import get_wallet_summary

    db_strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))
    if not db_strategy.rules_json:
        raise HTTPException(status_code=400, detail="This strategy has no rules configured.")

    brokers = _get_brokers()
    if brokers is None:
        raise HTTPException(status_code=503, detail="Broker connection not ready yet — try again shortly.")
    broker = brokers["paper"]  # read-only preview — same rationale as get_expected_payoff above
    rules = json.loads(db_strategy.rules_json)
    symbols = json.loads(db_strategy.symbols)
    is_index = db_strategy.instrument_type == "INDEX"

    results = {}
    total_margin = 0.0
    any_real = False
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

        option_legs = [leg for leg in preview["legs"] if leg["instrument_type"] == "OPTION"]
        if not option_legs:
            results[symbol] = {"margin_required": 0.0, "is_real": True}
            continue

        instruments = [
            {"instrument_key": leg["instrument_token"], "quantity": leg["quantity"],
             "transaction_type": leg["transaction_type"], "product": "D"}
            for leg in option_legs
        ]
        margin = broker.get_basket_required_margin(instruments)
        is_real = margin is not None and margin > 0
        if not is_real:
            spot = preview["spot_price"]
            is_commodity = is_commodity_instrument_key(strategy.instrument_key)
            margin = sum(
                resolve_required_margin(
                    broker, leg["instrument_token"], leg["quantity"], leg["transaction_type"],
                    spot, is_index, is_commodity,
                )
                for leg in option_legs
            )

        results[symbol] = {"margin_required": round(margin, 2), "is_real": is_real}
        total_margin += margin
        any_real = any_real or is_real

    paper_available = get_wallet_summary(_current_user_id(user))["available_balance"]
    live_available = brokers["live"].get_available_funds() if brokers.get("live") else None

    return {
        "strategy_id": strategy_id,
        "symbols": results,
        "margin_required": round(total_margin, 2),
        "is_real": any_real,
        "paper": {"available_balance": paper_available, "sufficient": paper_available >= total_margin},
        "live": (
            {"available_balance": round(live_available, 2), "sufficient": live_available >= total_margin}
            if live_available is not None else {"available_balance": None, "sufficient": None}
        ),
    }


@router.post("/positions/{position_id}/close")
def close_custom_strategy_position(position_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Square off/close a single open leg of a custom strategy."""
    from api.custom_strategy_scheduler import _close_leg, _get_brokers

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
        "open": [leg.to_dict() for leg in legs if leg.status == "OPEN"],
        "closed": [leg.to_dict() for leg in legs if leg.status == "CLOSED"],
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
    from api.live_positions import compute_open_positions
    return {"rows": compute_open_positions(_current_user_id(user))}
