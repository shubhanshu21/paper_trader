"""
utils/position_tracker.py — Position tracking powered by SQLAlchemy & MySQL.

Broker-agnostic and strategy-level — records what a strategy actually sold,
independent of which broker executed it.
"""
from typing import Optional, List

from automate.db.engine import get_session
from automate.db.models import Position


def record_open_position(
    strategy_name: str, mode: str, symbol: str, entry_date: str, expiry: str,
    call_token: str, call_strike: int, call_entry_price: float, call_order_id: Optional[str],
    put_token: str, put_strike: int, put_entry_price: float, put_order_id: Optional[str],
    quantity: int, product: str,
    take_profit_pct: Optional[float], stop_loss_pct: Optional[float],
    exit_days_before_expiry: int = 1,
    user_id: Optional[int] = None,
) -> int:
    """
    Record a newly-opened strangle position in the MySQL database.
    Returns the new row's ID.

    user_id: the owning account, when this came from a real HTTP request
    (e.g. a manual terminal trade — see routes_terminal.py). Left None for
    the legacy CLI daemon, which has no per-request user context at all —
    those rows are system-wide/admin-only, same convention as Notification.
    """
    with get_session() as session:
        pos = Position(
            user_id=user_id,
            strategy_name=strategy_name,
            mode=mode,
            symbol=symbol,
            entry_date=entry_date,
            expiry=expiry,
            call_token=call_token,
            call_strike=call_strike,
            call_entry_price=call_entry_price,
            call_order_id=call_order_id,
            put_token=put_token,
            put_strike=put_strike,
            put_entry_price=put_entry_price,
            put_order_id=put_order_id,
            quantity=quantity,
            product=product,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            exit_days_before_expiry=exit_days_before_expiry,
            status="OPEN",
        )
        session.add(pos)
        session.flush()
        return pos.id


def get_open_positions(strategy_name: Optional[str] = None, mode: Optional[str] = None, user_id: Optional[int] = None) -> List[dict]:
    """
    Return open positions as a list of dicts, optionally filtered by
    strategy and/or mode. user_id: pass a real id to scope to one
    account's own positions; leave None for the unscoped/admin view
    (includes system-wide rows with no owner, e.g. the legacy daemon's).
    """
    with get_session() as session:
        query = session.query(Position).filter_by(status="OPEN")
        if strategy_name:
            query = query.filter_by(strategy_name=strategy_name)
        if mode:
            query = query.filter_by(mode=mode)
        if user_id is not None:
            query = query.filter_by(user_id=user_id)
        return [p.to_dict() for p in query.all()]


def get_closed_positions(limit: Optional[int] = 50, mode: Optional[str] = None, user_id: Optional[int] = None) -> List[dict]:
    """Most-recently-closed positions first. limit=None returns the full history (e.g. for wallet/ledger math)."""
    with get_session() as session:
        query = session.query(Position).filter_by(status="CLOSED")
        if mode:
            query = query.filter_by(mode=mode)
        if user_id is not None:
            query = query.filter_by(user_id=user_id)
        query = query.order_by(Position.exit_date.desc(), Position.id.desc())
        if limit is not None:
            query = query.limit(limit)
        return [r.to_dict() for r in query.all()]


def get_position(position_id: int) -> Optional[dict]:
    """Fetch a single position by ID (any status)."""
    with get_session() as session:
        pos = session.query(Position).filter_by(id=position_id).first()
        return pos.to_dict() if pos else None


def has_open_position(strategy_name: str, symbol: str) -> bool:
    """True if this strategy already has an open position for this symbol."""
    with get_session() as session:
        exists = (
            session.query(Position)
            .filter_by(status="OPEN", strategy_name=strategy_name, symbol=symbol)
            .first()
        )
        return exists is not None


def delete_closed_positions(mode: str, user_id: Optional[int] = None) -> int:
    """
    Permanently delete every CLOSED position for one mode (used by the
    "reset paper trading history" control-panel action). Open positions are
    never touched — an in-progress trade shouldn't be silently abandoned by
    a history reset. Returns the number of rows deleted.

    user_id: scope the reset to one account's own history — resetting
    YOUR wallet must never wipe another account's closed trades.
    """
    with get_session() as session:
        query = session.query(Position).filter_by(status="CLOSED", mode=mode)
        if user_id is not None:
            query = query.filter_by(user_id=user_id)
        count = query.count()
        query.delete(synchronize_session=False)
        return count


def close_position(
    position_id: int, exit_date: str, exit_reason: str,
    call_exit_price: float, put_exit_price: float,
    call_exit_order_id: Optional[str], put_exit_order_id: Optional[str],
) -> None:
    """Mark a position CLOSED with its exit details."""
    with get_session() as session:
        session.query(Position).filter_by(id=position_id).update({
            "status": "CLOSED",
            "exit_date": exit_date,
            "exit_reason": exit_reason,
            "call_exit_price": call_exit_price,
            "put_exit_price": put_exit_price,
            "call_exit_order_id": call_exit_order_id,
            "put_exit_order_id": put_exit_order_id,
        })
