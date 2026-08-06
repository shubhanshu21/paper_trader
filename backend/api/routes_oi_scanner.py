"""
api/routes_oi_scanner.py — EOD open-interest build-up/unwinding scanner,
reusing the SAME fno_bhavcopy table the backtest engine already walks
(open_int/chg_in_oi are real NSE bhavcopy columns, already present —
see db.models.FnoBhavcopy). NOT a live/intraday feed — this table is
synced from daily bhavcopy files, so a scan reflects the last date the
sync job actually ran, which can lag "today" by several days if the
sync hasn't been triggered recently. Every response carries the actual
`as_of` date used so the UI can be honest about staleness rather than
implying this is live.

Classification is the standard 4-quadrant OI+price read every options
platform uses (Sensibull/Opstra included):
  price up   + OI up   -> LONG_BUILDUP    (fresh longs being added)
  price down + OI up   -> SHORT_BUILDUP   (fresh shorts being added)
  price down + OI down -> LONG_UNWINDING  (longs closing out)
  price up   + OI down -> SHORT_COVERING  (shorts closing out)
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func

from api.auth import get_current_user
from db.engine import SessionLocal
from db.models import FnoBhavcopy

router = APIRouter(prefix="/api/oi-scanner", tags=["oi-scanner"])


def _classify(price_change: float, chg_in_oi: int) -> str:
    if chg_in_oi > 0:
        return "LONG_BUILDUP" if price_change > 0 else "SHORT_BUILDUP" if price_change < 0 else "NEUTRAL"
    if chg_in_oi < 0:
        return "SHORT_COVERING" if price_change > 0 else "LONG_UNWINDING" if price_change < 0 else "NEUTRAL"
    return "NEUTRAL"


@router.get("")
def oi_scanner(symbol: str, user: dict = Depends(get_current_user)):
    symbol = symbol.upper()
    db = SessionLocal()
    try:
        latest_date = db.query(func.max(FnoBhavcopy.trade_date)).filter(FnoBhavcopy.symbol == symbol).scalar()
        if not latest_date:
            raise HTTPException(status_code=404, detail=f"No bhavcopy data available for '{symbol}'.")
        prev_date = (
            db.query(FnoBhavcopy.trade_date)
            .filter(FnoBhavcopy.symbol == symbol, FnoBhavcopy.trade_date < latest_date)
            .distinct().order_by(FnoBhavcopy.trade_date.desc()).limit(1).scalar()
        )

        # Futures — one clean underlying-trend read (near-month contract, the most liquid).
        fut_latest = (
            db.query(FnoBhavcopy)
            .filter(FnoBhavcopy.symbol == symbol, FnoBhavcopy.instrument == "FUTIDX", FnoBhavcopy.trade_date == latest_date)
            .order_by(FnoBhavcopy.expiry_dt.asc()).first()
        ) or (
            db.query(FnoBhavcopy)
            .filter(FnoBhavcopy.symbol == symbol, FnoBhavcopy.instrument == "FUTSTK", FnoBhavcopy.trade_date == latest_date)
            .order_by(FnoBhavcopy.expiry_dt.asc()).first()
        )
        futures_signal = None
        if fut_latest is not None and prev_date is not None:
            fut_prev = db.query(FnoBhavcopy).filter(
                FnoBhavcopy.symbol == symbol, FnoBhavcopy.instrument == fut_latest.instrument,
                FnoBhavcopy.expiry_dt == fut_latest.expiry_dt, FnoBhavcopy.trade_date == prev_date,
            ).first()
            if fut_prev is not None and fut_prev.close:
                price_change = float(fut_latest.close) - float(fut_prev.close)
                chg_in_oi = int(fut_latest.chg_in_oi or 0)
                futures_signal = {
                    "expiry": fut_latest.expiry_dt, "close": float(fut_latest.close),
                    "price_change": round(price_change, 2), "price_change_pct": round(price_change / float(fut_prev.close) * 100, 2),
                    "open_interest": int(fut_latest.open_int or 0), "chg_in_oi": chg_in_oi,
                    "signal": _classify(price_change, chg_in_oi),
                }

        # Options — near-month strike-level OI movers.
        opt_instrument = "OPTIDX" if fut_latest is not None and fut_latest.instrument == "FUTIDX" else "OPTSTK"
        nearest_expiry = (
            db.query(func.min(FnoBhavcopy.expiry_dt))
            .filter(FnoBhavcopy.symbol == symbol, FnoBhavcopy.instrument == opt_instrument, FnoBhavcopy.trade_date == latest_date)
            .scalar()
        )
        strike_rows = []
        if nearest_expiry and prev_date is not None:
            latest_opts = db.query(FnoBhavcopy).filter(
                FnoBhavcopy.symbol == symbol, FnoBhavcopy.instrument == opt_instrument,
                FnoBhavcopy.expiry_dt == nearest_expiry, FnoBhavcopy.trade_date == latest_date,
            ).all()
            prev_opts = db.query(FnoBhavcopy).filter(
                FnoBhavcopy.symbol == symbol, FnoBhavcopy.instrument == opt_instrument,
                FnoBhavcopy.expiry_dt == nearest_expiry, FnoBhavcopy.trade_date == prev_date,
            ).all()
            prev_by_key = {(float(r.strike_pr), r.option_typ): r for r in prev_opts if r.strike_pr is not None}

            for r in latest_opts:
                if r.strike_pr is None or r.close is None:
                    continue
                prev_row = prev_by_key.get((float(r.strike_pr), r.option_typ))
                if prev_row is None or not prev_row.close:
                    continue
                price_change = float(r.close) - float(prev_row.close)
                chg_in_oi = int(r.chg_in_oi or 0)
                strike_rows.append({
                    "strike": float(r.strike_pr), "option_type": r.option_typ, "close": float(r.close),
                    "price_change": round(price_change, 2), "open_interest": int(r.open_int or 0), "chg_in_oi": chg_in_oi,
                    "signal": _classify(price_change, chg_in_oi),
                })
            strike_rows.sort(key=lambda x: abs(x["chg_in_oi"]), reverse=True)
            strike_rows = strike_rows[:15]

        return {
            "symbol": symbol, "as_of": latest_date, "compared_to": prev_date,
            "futures": futures_signal, "top_strike_moves": strike_rows,
        }
    finally:
        db.close()
