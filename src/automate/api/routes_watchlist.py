"""
api/routes_watchlist.py — User watchlist management APIs.

Allows users to maintain custom watchlists that persist across sessions.
Integrates with the real broker for live price updates.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy import select

from automate.db.engine import get_session
from automate.db.models import UserWatchlist, Instrument
from automate.api.deps import get_brokers
from automate.api.auth import get_current_user_optional

log = logging.getLogger("api.watchlist")
router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class WatchlistAddRequest(BaseModel):
    instrument_key: str
    page: int = 1


class WatchlistRemoveRequest(BaseModel):
    instrument_key: str
    page: int = 1


class WatchlistReorderRequest(BaseModel):
    items: list[dict]  # [{"id": 1, "order_index": 0}, ...]


@router.get("")
def get_user_watchlist(page: int = 1, payload: Optional[dict] = Depends(get_current_user_optional)):
    """
    Get user's watchlist for a specific page with instrument details.
    If user is not authenticated, falls back to default user_id = 1.
    """
    user_id = int(payload["sub"]) if payload else 1
    with get_session() as session:
        query = select(UserWatchlist).where(
            (UserWatchlist.user_id == user_id) &
            (UserWatchlist.page == page)
        )
        
        watchlist_items = session.execute(
            query.order_by(UserWatchlist.order_index.asc(), UserWatchlist.added_at.asc())
        ).scalars().all()
        
        # Fetch instrument details for each watchlist item
        result = []
        for item in watchlist_items:
            inst = session.get(Instrument, item.instrument_key)
            if inst:
                inst_dict = inst.to_dict()
                inst_dict["watchlist_id"] = item.id
                inst_dict["added_at"] = item.added_at.isoformat() if item.added_at else None
                inst_dict["order_index"] = item.order_index
                inst_dict["page"] = item.page
                result.append(inst_dict)
        
        return result


@router.post("/add")
def add_to_watchlist(req: WatchlistAddRequest, payload: Optional[dict] = Depends(get_current_user_optional)):
    """Add an instrument to user's watchlist for a specific page."""
    user_id = int(payload["sub"]) if payload else 1
    
    # Verify instrument exists
    with get_session() as session:
        inst = session.get(Instrument, req.instrument_key)
        if not inst:
            raise HTTPException(status_code=404, detail="Instrument not found")
        
        # Check if already in watchlist on this page
        existing = session.execute(
            select(UserWatchlist).where(
                (UserWatchlist.user_id == user_id) &
                (UserWatchlist.instrument_key == req.instrument_key) &
                (UserWatchlist.page == req.page)
            )
        ).scalar_one_or_none()
        
        if existing:
            return {"status": "already_exists", "id": existing.id}
        
        # Get current max order_index on this page
        max_order = session.execute(
            select(UserWatchlist.order_index).where(
                (UserWatchlist.user_id == user_id) &
                (UserWatchlist.page == req.page)
            )
        ).scalar()
        next_order = (max_order + 1) if max_order is not None else 0
        
        # Add to watchlist
        new_item = UserWatchlist(
            user_id=user_id,
            instrument_key=req.instrument_key,
            page=req.page,
            order_index=next_order
        )
        session.add(new_item)
        session.commit()
        
        return {"status": "added", "id": new_item.id}


@router.post("/remove")
def remove_from_watchlist(req: WatchlistRemoveRequest, payload: Optional[dict] = Depends(get_current_user_optional)):
    """Remove an instrument from user's watchlist for a specific page."""
    user_id = int(payload["sub"]) if payload else 1
    
    with get_session() as session:
        item = session.execute(
            select(UserWatchlist).where(
                (UserWatchlist.user_id == user_id) &
                (UserWatchlist.instrument_key == req.instrument_key) &
                (UserWatchlist.page == req.page)
            )
        ).scalar_one_or_none()
        
        if not item:
            raise HTTPException(status_code=404, detail="Item not in watchlist")
        
        session.delete(item)
        session.commit()
        
        return {"status": "removed"}


@router.post("/reorder")
def reorder_watchlist(req: WatchlistReorderRequest, payload: Optional[dict] = Depends(get_current_user_optional)):
    """Reorder items in user's watchlist."""
    user_id = int(payload["sub"]) if payload else 1
    
    with get_session() as session:
        for item_data in req.items:
            item = session.get(UserWatchlist, item_data["id"])
            if item and item.user_id == user_id:
                item.order_index = item_data["order_index"]
        
        session.commit()
        
        return {"status": "reordered"}


@router.get("/ltp/{instrument_key}")
def get_watchlist_ltp(instrument_key: str, mode: str = "paper"):
    """
    Get live LTP for a watchlist instrument.
    This endpoint is called by the frontend to get real-time price updates.
    """
    brokers = get_brokers()
    if not brokers:
        raise HTTPException(status_code=503, detail="Broker client unavailable.")
    
    broker = brokers.get(mode)
    if not broker:
        raise HTTPException(status_code=400, detail=f"Invalid broker mode: {mode}")
    
    try:
        ltp = broker.get_ltp(instrument_key)
        if ltp is None:
            raise HTTPException(status_code=404, detail="LTP quote unavailable.")
        return {"instrument_key": instrument_key, "ltp": ltp}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Broker quote error: {exc}")


@router.post("/ltp/batch")
def get_batch_ltp(instrument_keys: list[str], mode: str = "paper"):
    """
    Get LTPs for multiple instruments in one request.
    More efficient than individual calls for watchlist updates.
    """
    brokers = get_brokers()
    if not brokers:
        raise HTTPException(status_code=503, detail="Broker client unavailable.")
    
    broker = brokers.get(mode)
    if not broker:
        raise HTTPException(status_code=400, detail=f"Invalid broker mode: {mode}")

    try:
        raw = broker.get_ltp_batch(instrument_keys)
    except Exception:
        raw = {}
    results = {key: (raw.get(key) or 0) for key in instrument_keys}
    
    return results
