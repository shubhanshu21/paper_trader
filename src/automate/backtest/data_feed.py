"""
backtest/data_feed.py — Historical Data Manager for Backtesting

Feeds REAL historical market data (downloaded via
scripts/download_real_history.py) into the MockBroker. No synthetic/fake
prices — every LTP served during a backtest comes from an actual downloaded
Upstox candle.
"""
import csv
from bisect import bisect_right
from datetime import datetime
from types import SimpleNamespace
from typing import Optional, List, Dict, Tuple, Any

from automate.utils.logger import get_logger

log = get_logger(__name__)


def _parse_candle_csv(path: str) -> List[Tuple[datetime, float]]:
    """
    Load a candle CSV written by scripts/download_real_history.py
    (columns: timestamp, open, high, low, close, volume, open_interest)
    into a list of (timestamp, close) pairs sorted ascending by time.
    """
    series: List[Tuple[datetime, float]] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = datetime.fromisoformat(row["timestamp"])
            series.append((ts, float(row["close"])))
    series.sort(key=lambda pair: pair[0])
    return series


def _price_at(series: List[Tuple[datetime, float]], at_time: Optional[datetime]) -> Optional[float]:
    """Return the most recent candle close at or before `at_time`."""
    if not series or at_time is None:
        return None
    idx = bisect_right(series, at_time, key=lambda pair: pair[0]) - 1
    if idx < 0:
        return None
    return series[idx][1]


class DataFeed:
    """
    Feeds real historical market data into the MockBroker.

    Real usage: call `load_from_csv()` with the spot CSV and the two option
    leg CSVs (all downloaded via scripts/download_real_history.py). The
    simulation clock is driven entirely by the spot CSV's own timestamps
    (`self.timestamps`) — BacktestEngine steps through them in order.
    """

    def __init__(self):
        self.current_time: Optional[datetime] = None

        # Real historical time series, keyed by instrument_key/token.
        self._series: Dict[str, List[Tuple[datetime, float]]] = {}

        # Sorted spot timestamps — drives the simulation clock.
        self.timestamps: List[datetime] = []

        # First timestamp at which spot AND both option legs all already
        # have at least one real print — set by load_from_csv(). A sensible
        # default entry time: entering any earlier is guaranteed to fail
        # for whichever leg hasn't traded yet that day (common for
        # deep-OTM/illiquid strikes right at market open).
        self.earliest_viable_time: Optional[datetime] = None

        # Static overrides set directly via set_ltp/set_option_chain/etc.
        # (kept for unit-testing DataFeed itself without real CSVs).
        self._ltps: Dict[str, float] = {}
        self._expiries: Dict[str, List[str]] = {}
        self._option_chains: Dict[tuple, List[Any]] = {}

    def set_time(self, new_time: datetime) -> None:
        """Advance the simulated clock."""
        self.current_time = new_time
        log.debug("DataFeed time advanced to %s", new_time)

    def set_ltp(self, instrument_key: str, ltp: float) -> None:
        """Set a static LTP override for an instrument (test-only helper)."""
        self._ltps[instrument_key] = ltp

    def set_option_contracts(self, instrument_key: str, expiries: List[str]) -> None:
        """Set the available expiries for an underlying (test-only helper)."""
        self._expiries[instrument_key] = expiries

    def set_option_chain(self, instrument_key: str, expiry: str, chain: List[Any]) -> None:
        """Set the option chain for a given underlying and expiry (test-only helper)."""
        self._option_chains[(instrument_key, expiry)] = chain

    # ------------------------------------------------------------------
    # Real historical data loading
    # ------------------------------------------------------------------

    def load_from_csv(
        self,
        equity_key: str,
        spot_csv_path: str,
        expiry: str,
        call_strike: int,
        call_token: str,
        call_csv_path: str,
        put_strike: int,
        put_token: str,
        put_csv_path: str,
    ) -> None:
        """
        Load real 1-minute historical candles for the underlying equity and
        both option legs of a short strangle, all downloaded via
        `scripts/download_real_history.py --equity-key ... --call-token ...
        --put-token ...`.

        IMPORTANT — data-source limitation: Upstox only exposes option chain
        *listings* (which strikes/tokens exist) for currently-live expiries,
        not for expiries that have already lapsed. So `call_token`/`put_token`
        must be real instrument_keys you resolved for a still-current expiry
        (e.g. from `cache/upstox_instruments_*.csv` or the option chain API)
        — this backtest replays real historical price candles for those two
        specific contracts, it does not retroactively discover what strikes
        existed on a past date.

        Args:
            equity_key:      Upstox instrument_key for the underlying equity.
            spot_csv_path:   CSV of the equity's historical candles.
            expiry:          Expiry date 'YYYY-MM-DD' for both legs.
            call_strike:     CE strike price (for compliance/logging only).
            call_token:      Upstox instrument_key for the CE contract.
            call_csv_path:   CSV of the CE contract's historical candles.
            put_strike:      PE strike price (for compliance/logging only).
            put_token:       Upstox instrument_key for the PE contract.
            put_csv_path:    CSV of the PE contract's historical candles.
        """
        spot_series = _parse_candle_csv(spot_csv_path)
        if not spot_series:
            raise RuntimeError(f"No candles found in spot CSV '{spot_csv_path}'.")
        call_series = _parse_candle_csv(call_csv_path)
        put_series = _parse_candle_csv(put_csv_path)
        if not call_series:
            raise RuntimeError(f"No candles found in CE CSV '{call_csv_path}'.")
        if not put_series:
            raise RuntimeError(f"No candles found in PE CSV '{put_csv_path}'.")

        self._series[equity_key] = spot_series
        self._series[call_token] = call_series
        self._series[put_token] = put_series
        self.timestamps = [t for t, _ in spot_series]
        self.earliest_viable_time = max(spot_series[0][0], call_series[0][0], put_series[0][0])

        self._expiries[equity_key] = [expiry]
        self._option_chains[(equity_key, expiry)] = [
            SimpleNamespace(
                strike_price=call_strike,
                call_options=SimpleNamespace(instrument_key=call_token),
                put_options=None,
            ),
            SimpleNamespace(
                strike_price=put_strike,
                call_options=None,
                put_options=SimpleNamespace(instrument_key=put_token),
            ),
        ]

        log.info(
            "Loaded real historical data | spot=%d bars (%s → %s) | CE=%d bars | PE=%d bars | "
            "earliest viable entry (all 3 legs have traded): %s",
            len(spot_series), self.timestamps[0], self.timestamps[-1],
            len(call_series), len(put_series), self.earliest_viable_time,
        )

    # ------------------------------------------------------------------
    # Methods called by MockBroker
    # ------------------------------------------------------------------

    def get_ltp(self, instrument_key: str) -> Optional[float]:
        """
        Return the real historical price at `self.current_time` for a loaded
        series, falling back to a static override if one was set directly.
        """
        if instrument_key in self._series:
            return _price_at(self._series[instrument_key], self.current_time)
        return self._ltps.get(instrument_key)

    def get_option_contracts(self, instrument_key: str) -> List[str]:
        return self._expiries.get(instrument_key, [])

    def get_option_chain(self, instrument_key: str, expiry: str) -> List[Any]:
        return self._option_chains.get((instrument_key, expiry), [])
