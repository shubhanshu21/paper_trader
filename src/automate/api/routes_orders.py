"""
api/routes_orders.py — Order execution tracking APIs.

Real-time order status tracking from broker integration.
Provides order lifecycle monitoring and status updates.
"""
import logging
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from automate.db.engine import get_session
from automate.db.models import OrderExecution

log = logging.getLogger("api.orders")
router = APIRouter(prefix="/api/orders/tracking", tags=["orders"])


class OrderStatusUpdate(BaseModel):
    order_id: str
    status: str
    status_message: Optional[str] = None
    filled_quantity: Optional[int] = None
    filled_price: Optional[float] = None


@router.get("")
def list_orders(
    user_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 100
):
    """
    List order executions with optional filtering.
    Can filter by user_id and/or status.
    """
    with get_session() as session:
        query = select(OrderExecution)
        
        if user_id:
            query = query.where(OrderExecution.user_id == user_id)
        
        if status:
            query = query.where(OrderExecution.status == status)
        
        query = query.order_by(OrderExecution.created_at.desc()).limit(limit)
        
        orders = session.execute(query).scalars().all()
        return [order.to_dict() for order in orders]


@router.get("/{order_id}")
def get_order(order_id: str):
    """Get specific order by broker order ID."""
    with get_session() as session:
        order = session.execute(
            select(OrderExecution).where(OrderExecution.order_id == order_id)
        ).scalar_one_or_none()
        
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        
        return order.to_dict()


@router.post("/track")
def track_order(req: OrderStatusUpdate, user_id: Optional[int] = None):
    """
    Track or update order execution status.
    Called by broker integration when order status changes.
    """
    with get_session() as session:
        # Check if order exists
        order = session.execute(
            select(OrderExecution).where(OrderExecution.order_id == req.order_id)
        ).scalar_one_or_none()
        
        if order:
            # Update existing order
            if req.status:
                order.status = req.status
            if req.status_message:
                order.status_message = req.status_message
            if req.filled_quantity is not None:
                order.filled_quantity = req.filled_quantity
            if req.filled_price is not None:
                order.filled_price = req.filled_price
            order.updated_at = datetime.utcnow()
        else:
            # Create new order tracking record
            order = OrderExecution(
                user_id=user_id,
                order_id=req.order_id,
                status=req.status,
                status_message=req.status_message,
                filled_quantity=req.filled_quantity,
                filled_price=req.filled_price,
                created_at=datetime.utcnow()
            )
            session.add(order)
        
        session.commit()
        return {"status": "tracked", "order_id": req.order_id}


@router.get("/status/{status}")
def get_orders_by_status(status: str, user_id: Optional[int] = None, limit: int = 100):
    """Get all orders with specific status."""
    with get_session() as session:
        query = select(OrderExecution).where(OrderExecution.status == status)
        
        if user_id:
            query = query.where(OrderExecution.user_id == user_id)
        
        query = query.order_by(OrderExecution.created_at.desc()).limit(limit)
        
        orders = session.execute(query).scalars().all()
        return [order.to_dict() for order in orders]


@router.get("/pending")
def get_pending_orders(user_id: Optional[int] = None):
    """Get all pending orders that haven't completed yet."""
    with get_session() as session:
        query = select(OrderExecution).where(
            OrderExecution.status.in_(["PENDING", "OPEN"])
        )
        
        if user_id:
            query = query.where(OrderExecution.user_id == user_id)
        
        query = query.order_by(OrderExecution.created_at.asc())
        
        orders = session.execute(query).scalars().all()
        return [order.to_dict() for order in orders]


@router.post("/{order_id}/cancel")
def cancel_order(order_id: str, user_id: Optional[int] = None):
    """
    Cancel a pending order.
    This would integrate with broker's cancel order functionality.
    """
    with get_session() as session:
        order = session.execute(
            select(OrderExecution).where(OrderExecution.order_id == order_id)
        ).scalar_one_or_none()
        
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        
        if order.status not in ["PENDING", "OPEN"]:
            raise HTTPException(status_code=400, detail="Cannot cancel order in current status")
        
        # Here you would integrate with broker's cancel order API
        # For now, just mark as cancelled
        order.status = "CANCELLED"
        order.status_message = "Cancelled by user"
        order.updated_at = datetime.utcnow()
        
        session.commit()
        
        return {"status": "cancelled", "order_id": order_id}
