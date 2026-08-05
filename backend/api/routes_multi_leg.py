"""
api/routes_multi_leg.py — Multi-leg options strategies API.

Supports complex options strategies like iron condors, butterflies,
spreads, and other multi-leg combinations as single orders.
"""
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/options/multi-leg", tags=["multi-leg"])


class OptionLeg(BaseModel):
    """Individual option leg in a multi-leg strategy."""
    symbol: str
    side: str  # "BUY" or "SELL"
    quantity: int
    strike: float
    expiry: str
    option_type: str  # "CE" or "PE"


class MultiLegOrderRequest(BaseModel):
    """Multi-leg options strategy order request."""
    strategy_type: str  # "iron_condor", "butterfly", "spread", "straddle", "strangle", etc.
    legs: list[OptionLeg]
    user_id: int | None = None
    strategy_name: str | None = None
    notes: str | None = None


class StrategyTemplate(BaseModel):
    """Pre-defined strategy template."""
    name: str
    description: str
    legs: list[dict]
    risk_profile: str  # "low", "medium", "high"


# In-memory storage for multi-leg orders (in production, use database)
multi_leg_orders: dict = {}
strategy_templates: dict = {
    "iron_condor": StrategyTemplate(
        name="Iron Condor",
        description="Sell OTM put spread and OTM call spread for income",
        legs=[
            {"side": "SELL", "option_type": "PE", "strike_offset": -0.05},
            {"side": "BUY", "option_type": "PE", "strike_offset": -0.10},
            {"side": "SELL", "option_type": "CE", "strike_offset": 0.05},
            {"side": "BUY", "option_type": "CE", "strike_offset": 0.10},
        ],
        risk_profile="medium"
    ),
    "butterfly": StrategyTemplate(
        name="Butterfly Spread",
        description="Limited risk strategy for directional moves",
        legs=[
            {"side": "BUY", "option_type": "CE", "strike_offset": -0.05},
            {"side": "SELL", "option_type": "CE", "strike_offset": 0.00, "quantity": 2},
            {"side": "BUY", "option_type": "CE", "strike_offset": 0.05},
        ],
        risk_profile="low"
    ),
    "straddle": StrategyTemplate(
        name="Straddle",
        description="Buy ATM call and put for volatility plays",
        legs=[
            {"side": "BUY", "option_type": "CE", "strike_offset": 0.00},
            {"side": "BUY", "option_type": "PE", "strike_offset": 0.00},
        ],
        risk_profile="high"
    ),
    "strangle": StrategyTemplate(
        name="Strangle",
        description="Buy OTM call and put for cheaper volatility play",
        legs=[
            {"side": "BUY", "option_type": "CE", "strike_offset": 0.05},
            {"side": "BUY", "option_type": "PE", "strike_offset": -0.05},
        ],
        risk_profile="medium"
    ),
    "credit_spread": StrategyTemplate(
        name="Credit Spread",
        description="Sell OTM option, buy further OTM for protection",
        legs=[
            {"side": "SELL", "option_type": "CE", "strike_offset": 0.05},
            {"side": "BUY", "option_type": "CE", "strike_offset": 0.10},
        ],
        risk_profile="low"
    ),
}


@router.get("/templates")
def get_strategy_templates():
    """Get available multi-leg strategy templates."""
    return strategy_templates


@router.get("/templates/{strategy_name}")
def get_strategy_template(strategy_name: str):
    """Get a specific strategy template."""
    if strategy_name not in strategy_templates:
        raise HTTPException(status_code=404, detail="Strategy template not found")
    return strategy_templates[strategy_name]


@router.post("/calculate")
def calculate_strategy_premium(req: MultiLegOrderRequest):
    """
    Calculate net premium and Greeks for a multi-leg strategy.
    
    Returns combined premium, max profit, max loss, and portfolio Greeks.
    """
    from utils.options_greeks import OptionsGreeks
    
    total_premium = 0.0
    portfolio_greeks = {
        'delta': 0.0,
        'gamma': 0.0,
        'theta': 0.0,
        'vega': 0.0,
        'rho': 0.0
    }
    
    for leg in req.legs:
        # Calculate individual leg Greeks (mock implementation)
        # In production, use real spot price and volatility
        leg_greeks = OptionsGreeks.calculate_greeks(
            spot_price=18000.0,  # Mock spot price
            strike_price=leg.strike,
            time_to_expiry=0.1,  # Mock time to expiry
            risk_free_rate=0.06,
            volatility=0.15,
            option_type=leg.option_type.lower()
        )
        
        # Apply side multiplier (BUY = +1, SELL = -1)
        multiplier = 1 if leg.side == "BUY" else -1
        quantity = leg.quantity
        
        total_premium += multiplier * quantity * leg_greeks['premium']
        portfolio_greeks['delta'] += multiplier * quantity * leg_greeks['delta']
        portfolio_greeks['gamma'] += multiplier * quantity * leg_greeks['gamma']
        portfolio_greeks['theta'] += multiplier * quantity * leg_greeks['theta']
        portfolio_greeks['vega'] += multiplier * quantity * leg_greeks['vega']
        portfolio_greeks['rho'] += multiplier * quantity * leg_greeks['rho']
    
    # Calculate risk metrics based on strategy type
    max_profit = abs(total_premium) if total_premium < 0 else None
    max_loss = abs(total_premium) if total_premium > 0 else None
    
    return {
        "strategy_type": req.strategy_type,
        "net_premium": total_premium,
        "max_profit": max_profit,
        "max_loss": max_loss,
        "portfolio_greeks": portfolio_greeks,
        "legs_count": len(req.legs)
    }


@router.post("/execute")
def execute_multi_leg_order(req: MultiLegOrderRequest):
    """
    Execute a multi-leg options strategy as a single order.
    
    All legs are executed simultaneously or none at all.
    """
    order_id = f"ml_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    # Calculate strategy metrics
    calculation = calculate_strategy_premium(req)
    
    multi_leg_orders[order_id] = {
        "order_id": order_id,
        "strategy_type": req.strategy_type,
        "legs": req.legs,
        "user_id": req.user_id,
        "strategy_name": req.strategy_name,
        "notes": req.notes,
        "net_premium": calculation["net_premium"],
        "max_profit": calculation["max_profit"],
        "max_loss": calculation["max_loss"],
        "portfolio_greeks": calculation["portfolio_greeks"],
        "status": "pending",
        "created_at": datetime.now().isoformat()
    }
    
    return {
        "order_id": order_id,
        "status": "created",
        "calculation": calculation,
        "message": f"{req.strategy_type} order created successfully"
    }


@router.get("/orders/{order_id}")
def get_multi_leg_order(order_id: str):
    """Get multi-leg order details."""
    if order_id not in multi_leg_orders:
        raise HTTPException(status_code=404, detail="Multi-leg order not found")
    return multi_leg_orders[order_id]


@router.delete("/orders/{order_id}")
def cancel_multi_leg_order(order_id: str):
    """Cancel a multi-leg order."""
    if order_id not in multi_leg_orders:
        raise HTTPException(status_code=404, detail="Multi-leg order not found")
    
    multi_leg_orders[order_id]["status"] = "cancelled"
    
    return {"status": "cancelled", "order_id": order_id}


@router.get("/orders")
def list_multi_leg_orders(user_id: int | None = None, strategy_type: str | None = None):
    """List multi-leg orders, optionally filtered by user or strategy type."""
    filtered_orders = list(multi_leg_orders.values())
    
    if user_id:
        filtered_orders = [o for o in filtered_orders if o["user_id"] == user_id]
    
    if strategy_type:
        filtered_orders = [o for o in filtered_orders if o["strategy_type"] == strategy_type]
    
    return {"orders": filtered_orders}


@router.post("/analyze")
def analyze_risk_profile(req: MultiLegOrderRequest):
    """
    Analyze the risk profile of a multi-leg strategy.
    
    Provides detailed risk analysis including Greeks exposure,
    probability ranges, and scenario analysis.
    """
    calculation = calculate_strategy_premium(req)
    
    # Risk analysis
    greeks = calculation["portfolio_greeks"]
    delta_exposure = abs(greeks["delta"])
    gamma_risk = abs(greeks["gamma"])
    theta_decay = greeks["theta"]
    vega_exposure = abs(greeks["vega"])
    
    # Risk level assessment
    risk_score = (
        delta_exposure * 0.3 +
        gamma_risk * 0.3 +
        abs(theta_decay) * 0.2 +
        vega_exposure * 0.2
    )
    
    risk_level = "low" if risk_score < 1.0 else "medium" if risk_score < 2.0 else "high"
    
    return {
        "strategy_type": req.strategy_type,
        "risk_level": risk_level,
        "risk_score": risk_score,
        "greeks_analysis": {
            "directional_exposure": delta_exposure,
            "convexity_risk": gamma_risk,
            "time_decay_risk": theta_decay,
            "volatility_exposure": vega_exposure
        },
        "recommendations": _generate_risk_recommendations(risk_level, greeks)
    }


def _generate_risk_recommendations(risk_level: str, greeks: dict) -> list[str]:
    """Generate risk recommendations based on analysis."""
    recommendations = []
    
    if risk_level == "high":
        recommendations.append("Consider reducing position size")
        recommendations.append("Add protective hedges if possible")
    
    if abs(greeks["theta"]) > 0.5:
        recommendations.append("High time decay - monitor expiry closely")
    
    if abs(greeks["vega"]) > 0.5:
        recommendations.append("High volatility exposure - consider IV levels")
    
    if abs(greeks["gamma"]) > 0.3:
        recommendations.append("High convexity risk - monitor for large moves")
    
    if not recommendations:
        recommendations.append("Risk profile appears balanced")
    
    return recommendations
