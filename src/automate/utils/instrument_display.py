"""
utils/instrument_display.py — Resolve instrument_key -> human-readable
trading symbol.

EquityPosition.symbol stores the full instrument_key (e.g.
'NSE_EQ|INE931S01010'), not a plain ticker — it needs to be in that exact
format for broker.get_ltp() lookups. But that means anything displaying it
directly to a user (Holdings, Orders) was showing the raw key, and the
common `symbol.split("|")[0]` trick used elsewhere in the frontend actually
extracts the EXCHANGE SEGMENT ("NSE_EQ"), not the ticker — instrument_key's
second half is an ISIN, not the symbol. Real display symbols only live in
the Instrument master table, keyed by the same instrument_key.
"""
from typing import Iterable

from sqlalchemy import select

from automate.db.engine import get_session
from automate.db.models import Instrument


def resolve_display_symbols(instrument_keys: Iterable[str]) -> dict[str, str]:
    """
    Map instrument_key -> real trading symbol (e.g. 'NSE_EQ|INE002A01018' ->
    'RELIANCE') via one batched query against the Instrument master. Falls
    back to the instrument_key's own ISIN/token suffix (still better than
    the exchange-segment prefix) for any key not found in the master —
    e.g. a delisted/stale instrument no longer present after a resync.
    """
    keys = {k for k in instrument_keys if k}
    if not keys:
        return {}

    with get_session() as session:
        rows = session.execute(
            select(Instrument.instrument_key, Instrument.symbol).where(Instrument.instrument_key.in_(keys))
        ).all()

    mapping = {key: symbol for key, symbol in rows}
    for key in keys:
        if key not in mapping:
            mapping[key] = key.split("|")[-1]
    return mapping
