"""
backtest/bhavcopy_data_feed.py — DataFeed backed by MySQL fno_bhavcopy table.
"""
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.orm import Session

from utils.logger import get_logger

log = get_logger(__name__)

_TOKEN_PREFIX = "BHAV"


def _make_token(symbol: str, expiry: str, strike: int, option_type: str) -> str:
    return f"{_TOKEN_PREFIX}|{symbol}|{expiry}|{strike}|{option_type}"


def _parse_option_token(token: str) -> tuple[str, str, int, str] | None:
    """Return (symbol, expiry, strike, option_type), or None if not one of our option tokens."""
    parts = token.split("|")
    if len(parts) != 5 or parts[0] != _TOKEN_PREFIX:
        return None
    _, symbol, expiry, strike, option_type = parts
    return symbol, expiry, int(strike), option_type


class BhavcopyDataFeed:
    """
    Feeds real historical DAILY prices from MySQL fno_bhavcopy table into MockBroker.
    """

    def __init__(
        self,
        session: Session,
        symbol: str,
        equity_key: str,
        option_instrument: str,
        future_instrument: str,
    ) -> None:
        self.session = session
        self.symbol = symbol.upper()
        self.equity_key = equity_key
        self.option_instrument = option_instrument
        self.future_instrument = future_instrument
        self.current_time: datetime | None = None

    def set_time(self, new_time: datetime) -> None:
        self.current_time = new_time
        log.debug("BhavcopyDataFeed time advanced to %s", new_time)

    def _current_date_str(self) -> str:
        if self.current_time is None:
            raise RuntimeError("BhavcopyDataFeed.set_time() must be called before use.")
        return self.current_time.date().isoformat()

    def get_ltp(self, instrument_key: str) -> float | None:
        trade_date = self._current_date_str()

        if instrument_key == self.equity_key:
            row = self.session.execute(
                text(
                    "SELECT close FROM fno_bhavcopy WHERE symbol=:symbol AND instrument=:instrument AND trade_date=:trade_date "
                    "AND expiry_dt >= trade_date ORDER BY expiry_dt ASC LIMIT 1"
                ),
                {"symbol": self.symbol, "instrument": self.future_instrument, "trade_date": trade_date},
            ).fetchone()
            return float(row[0]) if row and row[0] is not None else None

        parsed = _parse_option_token(instrument_key)
        if not parsed:
            return None
        symbol, expiry, strike, option_type = parsed
        row = self.session.execute(
            text(
                "SELECT close FROM fno_bhavcopy WHERE symbol=:symbol AND instrument=:instrument AND expiry_dt=:expiry "
                "AND strike_pr=:strike AND option_typ=:option_type AND trade_date=:trade_date"
            ),
            {
                "symbol": symbol,
                "instrument": self.option_instrument,
                "expiry": expiry,
                "strike": float(strike),
                "option_type": option_type,
                "trade_date": trade_date,
            },
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def get_option_contracts(self, instrument_key: str) -> list[str]:
        """Every expiry currently listed (>= the simulated date)."""
        if instrument_key != self.equity_key:
            return []
        trade_date = self._current_date_str()
        rows = self.session.execute(
            text(
                "SELECT DISTINCT expiry_dt FROM fno_bhavcopy WHERE symbol=:symbol AND instrument=:instrument "
                "AND expiry_dt >= :trade_date ORDER BY expiry_dt"
            ),
            {"symbol": self.symbol, "instrument": self.option_instrument, "trade_date": trade_date},
        ).fetchall()
        return [r[0] for r in rows]

    def get_option_chain(self, instrument_key: str, expiry_date: str) -> list[SimpleNamespace]:
        if instrument_key != self.equity_key:
            return []
        trade_date = self._current_date_str()
        rows = self.session.execute(
            text(
                "SELECT strike_pr, option_typ FROM fno_bhavcopy WHERE symbol=:symbol AND instrument=:instrument "
                "AND expiry_dt=:expiry_date AND trade_date=:trade_date"
            ),
            {
                "symbol": self.symbol,
                "instrument": self.option_instrument,
                "expiry_date": expiry_date,
                "trade_date": trade_date,
            },
        ).fetchall()

        by_strike: dict[int, dict[str, str | None]] = {}
        for strike_pr, option_typ in rows:
            strike = int(strike_pr)
            entry = by_strike.setdefault(strike, {"CE": None, "PE": None})
            entry[option_typ] = _make_token(self.symbol, expiry_date, strike, option_typ)

        chain = []
        for strike, legs in sorted(by_strike.items()):
            chain.append(
                SimpleNamespace(
                    strike_price=strike,
                    call_options=SimpleNamespace(instrument_key=legs["CE"]) if legs["CE"] else None,
                    put_options=SimpleNamespace(instrument_key=legs["PE"]) if legs["PE"] else None,
                )
            )
        return chain

    def get_volume(self, instrument_key: str) -> int:
        """Real traded CONTRACTS for an option leg at the current simulated date."""
        parsed = _parse_option_token(instrument_key)
        if not parsed:
            return 0
        symbol, expiry, strike, option_type = parsed
        trade_date = self._current_date_str()
        row = self.session.execute(
            text(
                "SELECT contracts FROM fno_bhavcopy WHERE symbol=:symbol AND instrument=:instrument AND expiry_dt=:expiry "
                "AND strike_pr=:strike AND option_typ=:option_type AND trade_date=:trade_date"
            ),
            {
                "symbol": symbol,
                "instrument": self.option_instrument,
                "expiry": expiry,
                "strike": float(strike),
                "option_type": option_type,
                "trade_date": trade_date,
            },
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
