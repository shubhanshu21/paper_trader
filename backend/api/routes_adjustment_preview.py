"""
api/routes_adjustment_preview.py — read-only "what would the automatic
adjustment do right now" preview for SMART_CONDOR and DELTA_NEUTRAL_STRANGLE
(the two engines with a live-premium/delta-triggered mid-cycle adjustment
— see smart_condor_engine.py / delta_neutral_engine.py's own tick logic,
which this mirrors exactly for the trigger check and strike resolution,
but WITHOUT ever placing an order or writing to the DB).

Margin figures come from the real Upstox Margin Calculator basket call
(broker.get_basket_required_margin — the same primitive
routes_custom_strategies.py::get_required_margin already uses for its own
pre-flight margin preview), computed once for the CURRENT open basket and
once for the HYPOTHETICAL post-adjustment basket, so the UI can show the
actual margin delta an adjustment would produce before the engine's next
tick does it automatically.
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.auth import get_current_user
from api.routes_custom_strategies import _current_user_id, _get_owned_strategy
from db.engine import get_db
from db.models import CustomStrategyPosition
from strategies.custom.delta_neutral_schema import get_setting as get_dn_setting
from strategies.custom.delta_neutral_strategy import DeltaNeutralStrategy
from strategies.custom.smart_condor_schema import get_setting as get_sc_setting
from strategies.custom.smart_condor_strategy import SmartCondorStrategy
from utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/custom-strategies", tags=["custom-strategies"])

_PRODUCT_CODE = "D"  # matches get_required_margin's own basket call convention


def _leg_meta(position: CustomStrategyPosition) -> dict:
    try:
        return json.loads(position.leg_config_json) if position.leg_config_json else {}
    except json.JSONDecodeError:
        return {}


def _basket_row(position: CustomStrategyPosition) -> dict:
    return {
        "instrument_key": position.instrument_key, "quantity": position.quantity,
        "transaction_type": position.transaction_type, "product": _PRODUCT_CODE,
    }


def _hypothetical_row(instrument_key: str, quantity: int, transaction_type: str) -> dict:
    return {"instrument_key": instrument_key, "quantity": quantity, "transaction_type": transaction_type, "product": _PRODUCT_CODE}


def _get_brokers_or_503():
    from api.custom_strategy_scheduler import _get_brokers
    brokers = _get_brokers()
    if brokers is None:
        raise HTTPException(status_code=503, detail="Broker connection not ready yet — try again shortly.")
    return brokers


@router.get("/{strategy_id}/adjustment-preview")
def adjustment_preview(strategy_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    db_strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))
    if db_strategy.strategy_type not in ("SMART_CONDOR", "DELTA_NEUTRAL_STRANGLE"):
        raise HTTPException(status_code=400, detail="Adjustment preview is only available for Smart Condor and Delta-Neutral Strangle strategies.")
    if not db_strategy.rules_json:
        raise HTTPException(status_code=400, detail="This strategy has no rules configured.")

    open_positions = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy_id, CustomStrategyPosition.status == "OPEN",
    ).all()
    if not open_positions:
        return {"triggered": False, "trigger_detail": "No open legs — nothing to adjust.", "current_margin": None, "projected_margin": None}

    rules = json.loads(db_strategy.rules_json)
    symbols = json.loads(db_strategy.symbols)
    symbol = symbols[0]
    mode = open_positions[0].mode
    brokers = _get_brokers_or_503()
    broker = brokers.get(mode)
    if broker is None:
        raise HTTPException(status_code=503, detail=f"No broker available for '{mode}' mode right now.")

    short_ce = next((p for p in open_positions if p.option_type == "CE" and _leg_meta(p).get("role") == "SHORT"), None)
    short_pe = next((p for p in open_positions if p.option_type == "PE" and _leg_meta(p).get("role") == "SHORT"), None)
    if short_ce is None or short_pe is None:
        return {"triggered": False, "trigger_detail": "Mid-adjustment already (a side is currently flat) — resolves on the next tick.", "current_margin": None, "projected_margin": None}

    ce_premium, pe_premium = broker.get_ltp(short_ce.instrument_key), broker.get_ltp(short_pe.instrument_key)
    if not ce_premium or not pe_premium:
        raise HTTPException(status_code=503, detail="Could not fetch live premiums for the open short legs right now.")

    current_basket = [_basket_row(p) for p in open_positions]
    current_margin = broker.get_basket_required_margin(current_basket)
    expiry = open_positions[0].expiry

    if db_strategy.strategy_type == "SMART_CONDOR":
        return _smart_condor_preview(db_strategy, rules, symbol, broker, expiry, open_positions, short_ce, short_pe, ce_premium, pe_premium, current_margin)
    return _delta_neutral_preview(db_strategy, rules, symbol, broker, expiry, open_positions, short_ce, short_pe, ce_premium, pe_premium, current_margin)


def _smart_condor_preview(db_strategy, rules, symbol, broker, expiry, open_positions, short_ce, short_pe, ce_premium, pe_premium, current_margin):
    ratio = max(ce_premium, pe_premium) / min(ce_premium, pe_premium)
    trigger_ratio = get_sc_setting(rules, "premium_ratio_trigger")
    if ratio < trigger_ratio:
        return {
            "triggered": False,
            "trigger_detail": f"Short-leg premium ratio is {ratio:.2f} — needs to reach {trigger_ratio} to trigger an adjustment.",
            "current_margin": current_margin, "projected_margin": None,
        }

    safe_is_call = ce_premium < pe_premium
    safe_option_type = "CE" if safe_is_call else "PE"
    safe_side = "CALL" if safe_is_call else "PUT"
    threatened_premium = pe_premium if safe_is_call else ce_premium
    legs_to_replace = [p for p in open_positions if _leg_meta(p).get("side") == safe_side]

    try:
        engine = SmartCondorStrategy(broker=broker, audit=None, kill_switch=None, rate_limiter=None, symbol=symbol, rules=rules, user_id=db_strategy.user_id)
        spot = engine.get_spot()
        chain = engine.get_chain(expiry)
        from utils.option_utils import round_to_nearest_strike
        atm_strike = round_to_nearest_strike(spot, engine.strike_step)
        direction_sign = 1 if safe_option_type == "CE" else -1
        new_short_strike = engine.resolve_matching_strike(chain, safe_option_type, direction_sign, atm_strike, threatened_premium)
        hedge_points = get_sc_setting(rules, "hedge_points")
        new_hedge_strike = round_to_nearest_strike(new_short_strike + direction_sign * hedge_points, engine.strike_step)
        new_short_token = engine._resolve_token(chain, new_short_strike, safe_option_type)
        new_hedge_token = engine._resolve_token(chain, new_hedge_strike, safe_option_type)
        new_short_premium = broker.get_ltp(new_short_token)
        new_hedge_premium = broker.get_ltp(new_hedge_token)
    except Exception as exc:
        log.warning("adjustment_preview: smart condor preview failed for strategy %s: %s", db_strategy.id, exc)
        raise HTTPException(status_code=503, detail=f"Could not price the adjustment right now: {exc}") from exc

    quantity = legs_to_replace[0].quantity if legs_to_replace else rules.get("lots", 1) * (engine.real_lot_size or 1)
    hypothetical = [_basket_row(p) for p in open_positions if p not in legs_to_replace]
    hypothetical.append(_hypothetical_row(new_short_token, quantity, "SELL"))
    hypothetical.append(_hypothetical_row(new_hedge_token, quantity, "BUY"))
    projected_margin = broker.get_basket_required_margin(hypothetical)

    return {
        "triggered": True,
        "trigger_detail": f"Short-leg premium ratio is {ratio:.2f} ≥ {trigger_ratio} — the {safe_side} side would be re-centered.",
        "current_margin": current_margin,
        "projected_margin": projected_margin,
        "margin_delta": (projected_margin - current_margin) if (projected_margin is not None and current_margin is not None) else None,
        "legs_closed": [{"option_type": p.option_type, "strike": float(p.strike) if p.strike is not None else None, "role": _leg_meta(p).get("role")} for p in legs_to_replace],
        "new_legs": [
            {"option_type": safe_option_type, "strike": new_short_strike, "action": "SELL", "estimated_premium": new_short_premium},
            {"option_type": safe_option_type, "strike": new_hedge_strike, "action": "BUY", "estimated_premium": new_hedge_premium},
        ],
    }


def _delta_neutral_preview(db_strategy, rules, symbol, broker, expiry, open_positions, short_ce, short_pe, ce_premium, pe_premium, current_margin):
    try:
        engine = DeltaNeutralStrategy(broker=broker, audit=None, kill_switch=None, rate_limiter=None, symbol=symbol, rules=rules, user_id=db_strategy.user_id)
        futures_price = engine.get_futures_price()
        ce_delta = engine.live_delta(float(short_ce.strike), "CE", ce_premium, expiry, futures_price)
        pe_delta = engine.live_delta(float(short_pe.strike), "PE", pe_premium, expiry, futures_price)
    except Exception as exc:
        log.warning("adjustment_preview: delta neutral delta solve failed for strategy %s: %s", db_strategy.id, exc)
        raise HTTPException(status_code=503, detail=f"Could not solve live deltas right now: {exc}") from exc
    if ce_delta is None or pe_delta is None:
        raise HTTPException(status_code=503, detail="Could not solve live deltas for the open short legs right now.")

    losing_is_ce = ce_delta >= pe_delta
    losing_delta = ce_delta if losing_is_ce else pe_delta
    dmin, dmax = get_dn_setting(rules, "delta_trigger_min"), get_dn_setting(rules, "delta_trigger_max")

    # Stage 2 (full reset) takes precedence, same as the live engine's own tick order.
    if dmin <= losing_delta <= dmax:
        try:
            atm = engine.get_atm_strike()
            chain = engine.get_chain(expiry)
            losing_leg = short_ce if losing_is_ce else short_pe
            losing_premium = ce_premium if losing_is_ce else pe_premium
            target_premium = losing_premium * get_dn_setting(rules, "reset_premium_pct") / 100.0
            new_ce_strike = engine.find_strike_by_premium(chain, "CE", atm, target_premium)
            new_pe_strike = engine.find_strike_by_premium(chain, "PE", atm, target_premium)
            hedge_ce_strike = engine.find_hedge_strike(chain, atm, "CE")
            hedge_pe_strike = engine.find_hedge_strike(chain, atm, "PE")
            # Explicit (option_type, strike, side) triples rather than
            # inferring SELL/BUY by comparing strike values back against
            # new_ce_strike/new_pe_strike — a hedge strike could in theory
            # numerically coincide with a short strike (e.g. a very tight
            # hedge_points config), which would have silently mislabeled it.
            new_leg_specs = [
                ("CE", new_ce_strike, "SELL"), ("PE", new_pe_strike, "SELL"),
                ("CE", hedge_ce_strike, "BUY"), ("PE", hedge_pe_strike, "BUY"),
            ]
            tokens = {(opt, strike): engine._resolve_token(chain, strike, opt) for opt, strike, _side in new_leg_specs}
        except Exception as exc:
            log.warning("adjustment_preview: delta neutral stage-2 preview failed for strategy %s: %s", db_strategy.id, exc)
            raise HTTPException(status_code=503, detail=f"Could not price the Stage-2 reset right now: {exc}") from exc

        quantity = losing_leg.quantity
        hypothetical = [  # Stage 2 replaces EVERY leg
            _hypothetical_row(tokens[(opt, strike)], quantity, side) for opt, strike, side in new_leg_specs
        ]
        projected_margin = broker.get_basket_required_margin(hypothetical)

        return {
            "triggered": True,
            "trigger_detail": f"Losing leg's delta ({losing_delta:.3f}) is inside the Stage-2 reset range [{dmin}, {dmax}] — a full reset would fire.",
            "current_margin": current_margin,
            "projected_margin": projected_margin,
            "margin_delta": (projected_margin - current_margin) if (projected_margin is not None and current_margin is not None) else None,
            "legs_closed": [{"option_type": p.option_type, "strike": float(p.strike) if p.strike is not None else None, "role": _leg_meta(p).get("role")} for p in open_positions],
            "new_legs": [
                {"option_type": "CE", "strike": new_ce_strike, "action": "SELL", "estimated_premium": broker.get_ltp(tokens[("CE", new_ce_strike)])},
                {"option_type": "PE", "strike": new_pe_strike, "action": "SELL", "estimated_premium": broker.get_ltp(tokens[("PE", new_pe_strike)])},
                {"option_type": "CE", "strike": hedge_ce_strike, "action": "BUY", "estimated_premium": broker.get_ltp(tokens[("CE", hedge_ce_strike)])},
                {"option_type": "PE", "strike": hedge_pe_strike, "action": "BUY", "estimated_premium": broker.get_ltp(tokens[("PE", hedge_pe_strike)])},
            ],
        }

    # Stage 1 (rebalance the winning side to match the losing leg's delta).
    ratio = max(ce_premium, pe_premium) / min(ce_premium, pe_premium)
    trigger_ratio = get_dn_setting(rules, "premium_ratio_trigger")
    if ratio >= trigger_ratio:
        winning_leg = short_pe if losing_is_ce else short_ce
        winning_option_type = "PE" if losing_is_ce else "CE"
        try:
            atm = engine.get_atm_strike()
            chain = engine.get_chain(expiry)
            new_strike = engine.find_strike_by_delta(chain, winning_option_type, atm, expiry, futures_price, losing_delta)
            new_token = engine._resolve_token(chain, new_strike, winning_option_type)
        except Exception as exc:
            log.warning("adjustment_preview: delta neutral stage-1 preview failed for strategy %s: %s", db_strategy.id, exc)
            raise HTTPException(status_code=503, detail=f"Could not price the Stage-1 rebalance right now: {exc}") from exc

        hypothetical = [_basket_row(p) for p in open_positions if p.id != winning_leg.id]
        hypothetical.append(_hypothetical_row(new_token, winning_leg.quantity, "SELL"))
        projected_margin = broker.get_basket_required_margin(hypothetical)

        return {
            "triggered": True,
            "trigger_detail": f"Short-leg premium ratio is {ratio:.2f} ≥ {trigger_ratio} — Stage-1 rebalance would fire (booking the {winning_option_type} side, re-selling at the losing leg's delta).",
            "current_margin": current_margin,
            "projected_margin": projected_margin,
            "margin_delta": (projected_margin - current_margin) if (projected_margin is not None and current_margin is not None) else None,
            "legs_closed": [{"option_type": winning_leg.option_type, "strike": float(winning_leg.strike) if winning_leg.strike is not None else None, "role": _leg_meta(winning_leg).get("role")}],
            "new_legs": [{"option_type": winning_option_type, "strike": new_strike, "action": "SELL", "estimated_premium": broker.get_ltp(new_token)}],
        }

    return {
        "triggered": False,
        "trigger_detail": f"Not triggered — losing leg delta {losing_delta:.3f} (Stage-2 range [{dmin}, {dmax}]), premium ratio {ratio:.2f} (Stage-1 needs ≥ {trigger_ratio}).",
        "current_margin": current_margin,
        "projected_margin": None,
    }
