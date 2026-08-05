"""
utils/strategy_overrides.py — Runtime-editable strategy fields, layered on
top of config.py's STRATEGY_CONFIGS defaults.

config.py's per-strategy classes (e.g. TenPercentOTMStrangleConfig) are
plain Python class attributes — editing them means editing source and
restarting every process. The control-panel API needs to flip MODE
(paper<->live) or tweak SL/TP/exit-days from a browser, safely, without
touching config.py or requiring a restart.

This stores ONLY the fields a human/API might change at runtime, in a
small JSON file (data/runtime/strategy_overrides.json). Every reader of
STRATEGY_CONFIGS in the CLI (run_strategy.py, run_daemon.py,
run_position_monitor.py's mode dict) goes through get_effective_config()
instead of reading the config class directly — when no override file (or
no entry for that strategy) exists, behavior is byte-for-byte identical to
before this module existed, so the existing CLI/cron/daemon usage is
completely unaffected unless the API is actually used to change something.
"""
import json
from pathlib import Path

from config import STRATEGY_CONFIGS

OVERRIDES_PATH = "data/runtime/strategy_overrides.json"

# Only these fields are ever runtime-editable — everything else (PRODUCT,
# ...) stays source-controlled in config.py.
_EDITABLE_FIELDS = ("MODE", "SYMBOLS", "NUM_LOTS", "TAKE_PROFIT_PCT", "STOP_LOSS_PCT", "EXIT_DAYS_BEFORE_EXPIRY")


class EffectiveConfig:
    """A read-only view of one strategy's config, with overrides applied."""

    def __init__(self, cfg, overrides: dict):
        self.SYMBOLS = overrides.get("SYMBOLS", cfg.SYMBOLS)
        self.NUM_LOTS = overrides.get("NUM_LOTS", cfg.NUM_LOTS)
        self.PRODUCT = cfg.PRODUCT
        self.MODE = overrides.get("MODE", cfg.MODE)
        self.TAKE_PROFIT_PCT = overrides.get("TAKE_PROFIT_PCT", cfg.TAKE_PROFIT_PCT)
        self.STOP_LOSS_PCT = overrides.get("STOP_LOSS_PCT", cfg.STOP_LOSS_PCT)
        self.EXIT_DAYS_BEFORE_EXPIRY = overrides.get("EXIT_DAYS_BEFORE_EXPIRY", cfg.EXIT_DAYS_BEFORE_EXPIRY)
        self.EXTRA_KWARGS = {
            "take_profit_pct": self.TAKE_PROFIT_PCT,
            "stop_loss_pct": self.STOP_LOSS_PCT,
            "exit_days_before_expiry": self.EXIT_DAYS_BEFORE_EXPIRY,
        }


def load_overrides(path: str = OVERRIDES_PATH) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open() as f:
        return json.load(f)


def get_effective_config(strategy_name: str, path: str = OVERRIDES_PATH) -> EffectiveConfig:
    """Same fields as STRATEGY_CONFIGS[strategy_name], with any saved runtime overrides applied on top."""
    cfg = STRATEGY_CONFIGS[strategy_name]
    all_overrides = load_overrides(path)
    return EffectiveConfig(cfg, all_overrides.get(strategy_name, {}))


def set_override(strategy_name: str, path: str = OVERRIDES_PATH, **fields) -> EffectiveConfig:
    """
    Read-modify-write one or more editable fields for a strategy. Unknown
    field names are rejected loudly (an API bug, not a user input to
    silently ignore) — this is only ever called from the control-panel API,
    not user-facing form data directly.
    """
    unknown = [k for k in fields if k not in _EDITABLE_FIELDS]
    if unknown:
        raise ValueError(f"Not a runtime-editable field: {unknown}. Editable: {_EDITABLE_FIELDS}")
    if strategy_name not in STRATEGY_CONFIGS:
        raise ValueError(f"Unknown strategy: {strategy_name}")
    if "MODE" in fields and fields["MODE"] not in ("paper", "live"):
        raise ValueError("MODE must be 'paper' or 'live'")
    if "SYMBOLS" in fields and fields["SYMBOLS"] is not None:
        symbols = fields["SYMBOLS"]
        if not isinstance(symbols, list) or not symbols or not all(isinstance(s, str) and s.strip() for s in symbols):
            raise ValueError("SYMBOLS must be a non-empty list of non-empty strings")
    if "NUM_LOTS" in fields and fields["NUM_LOTS"] is not None and (
        not isinstance(fields["NUM_LOTS"], int) or fields["NUM_LOTS"] < 1
    ):
        raise ValueError("NUM_LOTS must be a positive integer")

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    all_overrides = load_overrides(path)
    strategy_overrides = all_overrides.setdefault(strategy_name, {})
    for key, value in fields.items():
        if value is None:
            strategy_overrides.pop(key, None)
        else:
            strategy_overrides[key] = value
    p.write_text(json.dumps(all_overrides, indent=2))
    return get_effective_config(strategy_name, path)
