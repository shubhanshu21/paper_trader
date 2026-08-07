"""api/routes_strategies.py — view/edit per-strategy runtime config (MODE, SYMBOLS, NUM_LOTS, SL/TP, exit-days-before-expiry)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.auth import require_admin
from config import STRATEGY_CONFIGS
from utils.strategy_overrides import get_effective_config, set_override

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


class ModeUpdate(BaseModel):
    mode: str


class ConfigUpdate(BaseModel):
    # This is a full-form save (the frontend always submits every field
    # together, pre-filled with the current effective values) rather than a
    # partial patch — so take_profit_pct/stop_loss_pct: None is a real,
    # meaningful state ("turn this threshold off"), not "leave unchanged".
    # A partial-patch design couldn't distinguish "field left alone" from
    # "field explicitly cleared" without a separate sentinel.
    symbols: list[str]
    num_lots: int
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    exit_days_before_expiry: int


def _serialize(name: str) -> dict:
    cfg = get_effective_config(name)
    return {
        "name": name,
        "symbols": cfg.SYMBOLS,
        "num_lots": cfg.NUM_LOTS,
        "product": cfg.PRODUCT,
        "mode": cfg.MODE,
        "take_profit_pct": cfg.TAKE_PROFIT_PCT,
        "stop_loss_pct": cfg.STOP_LOSS_PCT,
        "exit_days_before_expiry": cfg.EXIT_DAYS_BEFORE_EXPIRY,
    }


@router.get("")
def list_strategies():
    return [_serialize(name) for name in STRATEGY_CONFIGS]


@router.get("/{name}/known-symbols")
def known_symbols(name: str):
    """
    Every underlying that genuinely has F&O contracts listed today,
    sourced from the real downloaded instrument master (cache/) — not a
    hardcoded convenience list — so the UI can offer any tradable stock or
    index, not just the handful config.py happens to mention.
    """
    if name not in STRATEGY_CONFIGS:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {name}")
    from utils.instrument_cache import InstrumentCache

    try:
        return InstrumentCache().list_tradable_symbols()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"Could not load instrument master: {exc}") from exc


@router.post("/{name}/mode")
def update_mode(name: str, body: ModeUpdate, user: dict = Depends(require_admin)):
    if name not in STRATEGY_CONFIGS:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {name}")
    try:
        set_override(name, MODE=body.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize(name)


@router.post("/{name}/config")
def update_config(name: str, body: ConfigUpdate, user: dict = Depends(require_admin)):
    if name not in STRATEGY_CONFIGS:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {name}")
    symbols = [s.strip().upper() for s in body.symbols if s.strip()]
    try:
        set_override(
            name,
            SYMBOLS=symbols,
            NUM_LOTS=body.num_lots,
            TAKE_PROFIT_PCT=body.take_profit_pct,
            STOP_LOSS_PCT=body.stop_loss_pct,
            EXIT_DAYS_BEFORE_EXPIRY=body.exit_days_before_expiry,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize(name)
