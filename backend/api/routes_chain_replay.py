"""
api/routes_chain_replay.py — historical option chain "replay": scrub to
any past trading day/expiry and see the full CE/PE chain (close, OI,
volume) exactly as the EOD bhavcopy recorded it. Read-only, reuses the
same fno_bhavcopy table the backtest engine and oi_scanner already read
— no new data source, just a new way to browse data that already exists.
"""
from fastapi import APIRouter, Depends, HTTPException

from api.auth import get_current_user
from db.engine import SessionLocal
from db.models import FnoBhavcopy

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

        return {"symbol": symbol, "date": date, "expiry": expiry, "underlying_close": underlying_close, "chain": chain}
    finally:
        db.close()
