"""
api/routes_simulator.py — a scratch-pad options simulator: build an
arbitrary multi-leg position against a LIVE option chain (any symbol —
index, stock, or MCX commodity, unlike OI Scanner/Chain Replay which are
NSE-bhavcopy-only) and see live payoff/Greeks/margin/probability-of-
profit, the same way Sensibull/AlgoTest's "option simulator" tools do.

NOT tied to any CustomStrategy — legs live only in the request body, never
written to the DB. No order is ever placed; every call here is read-only
(get_ltp/get_option_chain/get_basket_required_margin lookups only).

Deliberately reuses the exact same pure math the rest of the app already
trusts rather than re-deriving it: utils/payoff.py (compute_payoff/
compute_payoff_curve/probability_of_profit — the same functions
routes_custom_strategies.py::get_expected_payoff uses) and
utils/black76.py (compute_greeks_from_market_price — the same self-solved-
from-market-price convention api/live_greeks.py's portfolio Greeks use,
not the broker's own reported greeks, for consistency with every other
Greeks number this app shows).

No intraday historical replay here — this is the LIVE half of the
simulator (see api/routes_chain_replay.py for the EOD-bhavcopy historical
half — this system has no minute-level historical options archive to
scrub through, see that module's docstring).
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.auth import get_current_user
from utils import black76
from utils.instrument_cache import InstrumentCache
from utils.logger import get_logger
from utils.margin import estimate_margin_blocked, is_commodity_instrument_key
from utils.payoff import PayoffLeg, compute_payoff, compute_payoff_curve, probability_of_profit

log = get_logger(__name__)

router = APIRouter(prefix="/api/simulator", tags=["simulator"])

_PRODUCT_CODE = "D"  # matches get_required_margin's own basket call convention


def _get_brokers_or_503():
    from api.custom_strategy_scheduler import _get_brokers
    brokers = _get_brokers()
    if brokers is None:
        raise HTTPException(status_code=503, detail="Broker connection not ready yet — try again shortly.")
    return brokers


@router.get("/chain")
def simulator_chain(symbol: str, expiry: str, user: dict = Depends(get_current_user)):
    """
    Live full chain snapshot for one symbol/expiry: per strike, CE and PE
    {instrument_key, ltp, iv, delta} — powers the simulator's chain-picker
    table. IV/delta are self-solved from the live LTP via Black-76 (see
    module docstring on why, not read off the broker's own greeks field).
    """
    symbol = symbol.upper()
    brokers = _get_brokers_or_503()
    broker = brokers["paper"]  # read-only preview — same rationale every other preview endpoint uses

    try:
        instrument_key = broker.resolve_instrument_key(symbol)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Unknown symbol '{symbol}': {exc}") from exc

    chain = broker.get_option_chain(instrument_key, expiry)
    if not chain:
        raise HTTPException(status_code=404, detail=f"No live option chain for {symbol} expiry {expiry}.")

    resolved_future = InstrumentCache().resolve_nearest_future_key(symbol)
    forward = broker.get_ltp(resolved_future[0]) if resolved_future else None
    spot = broker.get_ltp(instrument_key)
    forward = forward or spot
    if forward is None:
        raise HTTPException(status_code=503, detail=f"Could not fetch a live price for {symbol} right now.")

    days_to_expiry = (date.fromisoformat(expiry) - date.today()).days
    years_to_expiry = max(days_to_expiry, 1) / 365.0

    tokens = []
    for item in chain:
        if item.call_options:
            tokens.append(item.call_options.instrument_key)
        if item.put_options:
            tokens.append(item.put_options.instrument_key)
    ltp_by_token = broker.get_ltp_batch(tokens) if tokens else {}

    def _leg_snapshot(leg, option_type: str) -> dict | None:
        if leg is None:
            return None
        ltp = ltp_by_token.get(leg.instrument_key)
        greeks = None
        if ltp is not None and ltp > 0:
            greeks = black76.compute_greeks_from_market_price(
                F=forward, K=strike, T=years_to_expiry, r=black76.DEFAULT_RISK_FREE_RATE,
                market_price=ltp, option_type=option_type,
            )
        # black76.greeks() already returns "iv" in percentage points (e.g.
        # 22.5 for 22.5%, see its own return dict) — no further *100 here.
        return {
            "instrument_key": leg.instrument_key,
            "ltp": ltp,
            "iv": round(greeks["iv"], 2) if greeks and greeks.get("iv") else None,
            "delta": round(greeks["delta"], 4) if greeks else None,
        }

    rows = []
    for item in sorted(chain, key=lambda i: float(i.strike_price or 0)):
        strike = float(item.strike_price)
        rows.append({
            "strike": strike,
            "ce": _leg_snapshot(item.call_options, "CE"),
            "pe": _leg_snapshot(item.put_options, "PE"),
        })

    lot_size = broker.get_lot_size(symbol)
    return {"symbol": symbol, "expiry": expiry, "spot_price": spot, "forward_price": forward, "lot_size": lot_size, "chain": rows}


class SimulatorLeg(BaseModel):
    instrument_key: str
    strike: float
    option_type: str  # "CE" | "PE"
    action: str        # "BUY" | "SELL"
    quantity: int
    fallback_premium: float | None = None  # the LTP shown in the chain when this leg was added — see simulator_evaluate


class SimulatorEvaluateRequest(BaseModel):
    symbol: str
    expiry: str
    legs: list[SimulatorLeg]
    instrument_type: str = "INDEX"  # "INDEX" | "STOCK" | "COMMODITY" — margin-estimate fallback only


@router.post("/evaluate")
def simulator_evaluate(req: SimulatorEvaluateRequest, user: dict = Depends(get_current_user)):
    """Live payoff/Greeks/margin/probability-of-profit for an arbitrary leg set — never places an order, never touches the DB."""
    if not req.legs:
        raise HTTPException(status_code=422, detail="At least one leg is required.")

    symbol = req.symbol.upper()
    brokers = _get_brokers_or_503()
    broker = brokers["paper"]

    tokens = [leg.instrument_key for leg in req.legs]
    ltp_by_token = broker.get_ltp_batch(tokens)

    # A fresh quote can legitimately be unavailable for a specific leg —
    # most often a thin/deep-OTM contract with no recent trade, especially
    # once the market's closed for the day — without the WHOLE evaluation
    # being unusable over it. Fall back to fallback_premium (the LTP the
    # chain itself showed when the leg was added, a few seconds/minutes
    # earlier — a real observed price, just not the freshest possible one)
    # rather than hard-failing; only error out if a leg has neither.
    premium_by_token: dict[str, float] = {}
    stale_legs: list[str] = []
    hard_missing: list[str] = []
    for leg in req.legs:
        live = ltp_by_token.get(leg.instrument_key)
        if live:
            premium_by_token[leg.instrument_key] = live
        elif leg.fallback_premium:
            premium_by_token[leg.instrument_key] = leg.fallback_premium
            stale_legs.append(f"{leg.strike}{leg.option_type}")
        else:
            hard_missing.append(f"{leg.strike}{leg.option_type}")
    if hard_missing:
        raise HTTPException(status_code=503, detail=f"Could not fetch any price (live or last-seen) for {', '.join(hard_missing)} — try re-adding the leg from the chain.")

    payoff_legs: list[PayoffLeg] = [
        {"strike": leg.strike, "option_type": leg.option_type, "action": leg.action,
         "quantity": leg.quantity, "premium": premium_by_token[leg.instrument_key]}
        for leg in req.legs
    ]
    payoff = compute_payoff(payoff_legs)

    resolved_future = InstrumentCache().resolve_nearest_future_key(symbol)
    forward = broker.get_ltp(resolved_future[0]) if resolved_future else None
    spot = broker.get_ltp(broker.resolve_instrument_key(symbol))
    forward = forward or spot or 0.0

    days_to_expiry = (date.fromisoformat(req.expiry) - date.today()).days
    years_to_expiry = max(days_to_expiry, 1) / 365.0

    # Per-leg self-solved Greeks (Black-76, from live LTP) — same convention
    # api/live_greeks.py's portfolio Greeks use, not the broker's own
    # reported greeks field, so this number is consistent with every other
    # Greeks figure the app shows elsewhere.
    net_delta = net_gamma = net_theta = net_vega = 0.0
    leg_ivs: dict[str, float] = {}
    for leg in req.legs:
        premium = premium_by_token[leg.instrument_key]
        g = black76.compute_greeks_from_market_price(
            F=forward, K=leg.strike, T=years_to_expiry, r=black76.DEFAULT_RISK_FREE_RATE,
            market_price=premium, option_type=leg.option_type,
        )
        if g:
            sign = -1 if leg.action == "SELL" else 1
            net_delta += g["delta"] * leg.quantity * sign
            net_gamma += g["gamma"] * leg.quantity * sign
            net_theta += g["theta"] * leg.quantity * sign
            net_vega += g["vega"] * leg.quantity * sign
        # black76.greeks()'s "iv" is percentage-scaled (e.g. 22.5, for
        # display) — probability_of_profit()/prob_underlying_above() need
        # the raw DECIMAL sigma instead, so this is solved separately via
        # implied_volatility() rather than reusing g["iv"] (a real bug
        # caught in testing: feeding a percentage-scaled "22.5" in as sigma
        # produces nonsense probabilities, off by a factor of 100).
        iv = black76.implied_volatility(forward, leg.strike, years_to_expiry, black76.DEFAULT_RISK_FREE_RATE, premium, leg.option_type)
        if iv and iv > 0:
            leg_ivs[f"{leg.strike}{leg.option_type}"] = iv

    call_ivs = [leg_ivs[f"{leg.strike}{leg.option_type}"] for leg in req.legs if leg.option_type == "CE" and f"{leg.strike}{leg.option_type}" in leg_ivs]
    put_ivs = [leg_ivs[f"{leg.strike}{leg.option_type}"] for leg in req.legs if leg.option_type == "PE" and f"{leg.strike}{leg.option_type}" in leg_ivs]
    call_iv = sum(call_ivs) / len(call_ivs) if call_ivs else None
    put_iv = sum(put_ivs) / len(put_ivs) if put_ivs else None
    fallback_iv = call_iv or put_iv

    def _iv_for_bound(bound_price: float) -> float | None:
        return call_iv if bound_price >= forward else put_iv

    pop = (
        probability_of_profit(payoff_legs, payoff["breakevens"], forward, fallback_iv, years_to_expiry, iv_lookup=_iv_for_bound)
        if fallback_iv else None
    )

    # Margin: real Upstox basket call first (netted across every leg),
    # falling back to the flat-rate estimate only if that's unavailable —
    # same "real number when available" tier get_required_margin uses.
    basket = [
        {"instrument_key": leg.instrument_key, "quantity": leg.quantity, "transaction_type": leg.action, "product": _PRODUCT_CODE}
        for leg in req.legs
    ]
    margin = None
    try:
        margin = broker.get_basket_required_margin(basket)
    except Exception as exc:
        log.warning("simulator_evaluate: basket margin lookup failed for %s: %s", symbol, exc)
    is_real_margin = margin is not None and margin > 0
    if not is_real_margin:
        short_qty = max((leg.quantity for leg in req.legs if leg.action == "SELL"), default=0)
        if short_qty > 0:
            is_commodity = req.instrument_type == "COMMODITY" or is_commodity_instrument_key(req.legs[0].instrument_key)
            margin = estimate_margin_blocked(spot or forward, short_qty, req.instrument_type == "INDEX", is_commodity)
        else:
            margin = abs(payoff["net_premium"]) or None

    risk_reward_ratio = None
    if payoff["max_profit"] is not None and payoff["max_loss"] is not None and payoff["max_loss"] != 0:
        risk_reward_ratio = round(payoff["max_profit"] / abs(payoff["max_loss"]), 2)

    return {
        **payoff,
        "spot_price": spot,
        "forward_price": forward,
        "payoff_curve": compute_payoff_curve(payoff_legs, spot or forward),
        "greeks": {"delta": round(net_delta, 2), "gamma": round(net_gamma, 4), "theta": round(net_theta, 2), "vega": round(net_vega, 2)},
        "margin_required": round(margin, 2) if margin is not None else None,
        "margin_is_real": is_real_margin,
        "probability_of_profit_pct": round(pop * 100, 2) if pop is not None else None,
        "risk_reward_ratio": risk_reward_ratio,
        "breakevens_detail": [
            {"price": b, "pct_from_spot": round((b - spot) / spot * 100, 2) if spot else None} for b in payoff["breakevens"]
        ],
        "stale_legs": stale_legs,
    }
