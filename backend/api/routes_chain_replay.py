"""
api/routes_chain_replay.py — historical option chain "replay": scrub to
any past trading day/expiry and see the full CE/PE chain (close, OI,
volume) exactly as the EOD bhavcopy recorded it. Read-only, reuses the
same fno_bhavcopy table the backtest engine and oi_scanner already read
— no new data source, just a new way to browse data that already exists.

/evaluate is the EOD half of the options simulator (see
api/routes_simulator.py for the LIVE half) — build a position from one
past date's chain, then mark it to a LATER date's chain to see how MTM
evolved DAY BY DAY. Coarser than a minute-level intraday scrubber (this
system has no historical options tick archive — see routes_simulator.py's
own docstring), but uses only data this system actually has.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.auth import get_current_user
from db.engine import SessionLocal
from db.models import FnoBhavcopy
from utils.payoff import PayoffLeg, compute_payoff, compute_payoff_curve

router = APIRouter(prefix="/api/chain-replay", tags=["chain-replay"])

_MAX_DATES = 180


@router.get("/dates")
def available_dates(symbol: str, user: dict = Depends(get_current_user)):
    """Most recent trade_dates with any bhavcopy data for this symbol, newest first."""
    symbol = symbol.upper()
    db = SessionLocal()
    try:
        rows = (
            db.query(FnoBhavcopy.trade_date)
            .filter(FnoBhavcopy.symbol == symbol)
            .distinct().order_by(FnoBhavcopy.trade_date.desc()).limit(_MAX_DATES).all()
        )
        return {"symbol": symbol, "dates": [r[0] for r in rows]}
    finally:
        db.close()


@router.get("/expiries")
def available_expiries(symbol: str, date: str, user: dict = Depends(get_current_user)):
    """Every options expiry with a listed chain on `date` for this symbol, nearest first."""
    symbol = symbol.upper()
    db = SessionLocal()
    try:
        rows = (
            db.query(FnoBhavcopy.expiry_dt)
            .filter(
                FnoBhavcopy.symbol == symbol, FnoBhavcopy.trade_date == date,
                FnoBhavcopy.instrument.in_(("OPTIDX", "OPTSTK")),
            )
            .distinct().order_by(FnoBhavcopy.expiry_dt.asc()).all()
        )
        if not rows:
            raise HTTPException(status_code=404, detail=f"No option chain data for {symbol} on {date}.")
        return {"symbol": symbol, "date": date, "expiries": [r[0] for r in rows]}
    finally:
        db.close()


@router.get("")
def chain_replay(symbol: str, date: str, expiry: str, user: dict = Depends(get_current_user)):
    """The full CE/PE chain for `symbol`/`expiry` as it stood at the close of `date`."""
    symbol = symbol.upper()
    db = SessionLocal()
    try:
        opt_rows = db.query(FnoBhavcopy).filter(
            FnoBhavcopy.symbol == symbol, FnoBhavcopy.trade_date == date, FnoBhavcopy.expiry_dt == expiry,
            FnoBhavcopy.instrument.in_(("OPTIDX", "OPTSTK")),
        ).all()
        if not opt_rows:
            raise HTTPException(status_code=404, detail=f"No option chain data for {symbol} {expiry} on {date}.")

        by_strike: dict[float, dict] = {}
        for r in opt_rows:
            if r.strike_pr is None:
                continue
            strike = float(r.strike_pr)
            row = by_strike.setdefault(strike, {"strike": strike, "ce": None, "pe": None})
            leg = {
                "close": float(r.close) if r.close is not None else None,
                "open_interest": int(r.open_int or 0),
                "chg_in_oi": int(r.chg_in_oi or 0),
                "volume": int(r.contracts or 0),
            }
            if r.option_typ == "CE":
                row["ce"] = leg
            elif r.option_typ == "PE":
                row["pe"] = leg

        chain = sorted(by_strike.values(), key=lambda x: x["strike"])

        underlying_close = None
        fut_instrument = "FUTIDX" if any(r.instrument == "OPTIDX" for r in opt_rows) else "FUTSTK"
        # Nearest future by expiry (same-day chain's underlying reference — the
        # bhavcopy has no separate cash-equity close, same convention the
        # backtest engine's BhavcopyDataFeed already documents/uses).
        fut = (
            db.query(FnoBhavcopy)
            .filter(FnoBhavcopy.symbol == symbol, FnoBhavcopy.trade_date == date, FnoBhavcopy.instrument == fut_instrument)
            .order_by(FnoBhavcopy.expiry_dt.asc()).first()
        )
        if fut is not None and fut.close is not None:
            underlying_close = float(fut.close)

        # CURRENT lot size (live broker call) as a stand-in for `date`'s
        # actual historical lot size — bhavcopy doesn't store a per-day lot
        # size, and NSE only revises these occasionally, so this is a minor
        # approximation for older dates, same tier as other "best real
        # number available" spots in this codebase.
        lot_size = None
        try:
            from api.custom_strategy_scheduler import _get_brokers
            brokers = _get_brokers()
            if brokers is not None:
                lot_size = brokers["paper"].get_lot_size(symbol)
        except Exception:
            lot_size = None

        return {"symbol": symbol, "date": date, "expiry": expiry, "underlying_close": underlying_close, "lot_size": lot_size, "chain": chain}
    finally:
        db.close()


class ReplayLeg(BaseModel):
    strike: float
    option_type: str  # "CE" | "PE"
    action: str        # "BUY" | "SELL"
    quantity: int


class ReplayEvaluateRequest(BaseModel):
    symbol: str
    expiry: str
    entry_date: str
    eval_date: str
    legs: list[ReplayLeg]


def _close_lookup(db, symbol: str, expiry: str, trade_date: str) -> dict[tuple[float, str], float]:
    rows = db.query(FnoBhavcopy).filter(
        FnoBhavcopy.symbol == symbol, FnoBhavcopy.expiry_dt == expiry, FnoBhavcopy.trade_date == trade_date,
        FnoBhavcopy.instrument.in_(("OPTIDX", "OPTSTK")),
    ).all()
    return {(float(r.strike_pr), r.option_typ): float(r.close) for r in rows if r.strike_pr is not None and r.close is not None}


@router.post("/evaluate")
def replay_evaluate(req: ReplayEvaluateRequest, user: dict = Depends(get_current_user)):
    """
    Mark a leg set (priced at `entry_date`'s close) to `eval_date`'s close
    — real day-by-day MTM evolution from real EOD bhavcopy data, plus the
    entry-date payoff-at-expiry diagram (a fixed reference curve, same as
    the live simulator's) for context.
    """
    if not req.legs:
        raise HTTPException(status_code=422, detail="At least one leg is required.")
    symbol = req.symbol.upper()
    db = SessionLocal()
    try:
        entry_closes = _close_lookup(db, symbol, req.expiry, req.entry_date)
        if not entry_closes:
            raise HTTPException(status_code=404, detail=f"No option chain data for {symbol} {req.expiry} on {req.entry_date}.")
        eval_closes = _close_lookup(db, symbol, req.expiry, req.eval_date)
        if not eval_closes:
            raise HTTPException(status_code=404, detail=f"No option chain data for {symbol} {req.expiry} on {req.eval_date}.")

        missing_entry = [leg for leg in req.legs if (leg.strike, leg.option_type) not in entry_closes]
        if missing_entry:
            raise HTTPException(status_code=422, detail=f"No {req.entry_date} close for strike(s) {[leg.strike for leg in missing_entry]} {missing_entry[0].option_type}.")
        missing_eval = [leg for leg in req.legs if (leg.strike, leg.option_type) not in eval_closes]
        if missing_eval:
            raise HTTPException(status_code=422, detail=f"No {req.eval_date} close for strike(s) {[leg.strike for leg in missing_eval]} {missing_eval[0].option_type} — that contract may not have been listed yet, or already expired.")

        mtm = 0.0
        leg_details = []
        for leg in req.legs:
            entry_price = entry_closes[(leg.strike, leg.option_type)]
            eval_price = eval_closes[(leg.strike, leg.option_type)]
            sign = 1 if leg.action == "SELL" else -1
            leg_pnl = (entry_price - eval_price) * leg.quantity * sign
            mtm += leg_pnl
            leg_details.append({
                "strike": leg.strike, "option_type": leg.option_type, "action": leg.action, "quantity": leg.quantity,
                "entry_price": entry_price, "eval_price": eval_price, "pnl": round(leg_pnl, 2),
            })

        payoff_legs: list[PayoffLeg] = [
            {"strike": leg.strike, "option_type": leg.option_type, "action": leg.action,
             "quantity": leg.quantity, "premium": entry_closes[(leg.strike, leg.option_type)]}
            for leg in req.legs
        ]
        payoff = compute_payoff(payoff_legs)

        entry_underlying = db.query(FnoBhavcopy).filter(
            FnoBhavcopy.symbol == symbol, FnoBhavcopy.trade_date == req.entry_date,
            FnoBhavcopy.instrument.in_(("FUTIDX", "FUTSTK")),
        ).order_by(FnoBhavcopy.expiry_dt.asc()).first()
        eval_underlying = db.query(FnoBhavcopy).filter(
            FnoBhavcopy.symbol == symbol, FnoBhavcopy.trade_date == req.eval_date,
            FnoBhavcopy.instrument.in_(("FUTIDX", "FUTSTK")),
        ).order_by(FnoBhavcopy.expiry_dt.asc()).first()
        entry_spot = float(entry_underlying.close) if entry_underlying and entry_underlying.close else None

        return {
            "symbol": symbol, "expiry": req.expiry, "entry_date": req.entry_date, "eval_date": req.eval_date,
            "legs": leg_details,
            "mtm": round(mtm, 2),
            **payoff,
            "payoff_curve": compute_payoff_curve(payoff_legs, entry_spot) if entry_spot else [],
            "entry_underlying_close": entry_spot,
            "eval_underlying_close": float(eval_underlying.close) if eval_underlying and eval_underlying.close else None,
        }
    finally:
        db.close()
