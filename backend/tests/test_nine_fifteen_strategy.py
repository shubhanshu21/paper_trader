"""
tests/test_nine_fifteen_strategy.py — coverage for the pure signal logic
in strategies/custom/nine_fifteen_strategy.py (the 9:15 opening-range-
breakout stock-options-buying engine) and the rules contract in
nine_fifteen_schema.py. Direct-function-call style with a fake broker,
no real broker/DB — same pattern as tests/test_zero_to_hero_strategy.py.
Deliberately does NOT test enter()/resolve_leg() (thin order-placement
wrappers around BaseBroker, not decision logic).
"""
from unittest.mock import MagicMock, patch

from strategies.custom.nine_fifteen_schema import (
    describe_nine_fifteen_rules,
    validate_nine_fifteen_rules,
)
from strategies.custom.nine_fifteen_strategy import NineFifteenStrategy


class FakeBroker:
    def __init__(self, ohlc=None, resolve_map=None, dry_run=True):
        self.ohlc = ohlc or {}
        self.resolve_map = resolve_map or {}
        self.dry_run = dry_run

    def resolve_instrument_key(self, symbol):
        if symbol not in self.resolve_map:
            raise RuntimeError(f"unresolvable: {symbol}")
        return self.resolve_map[symbol]

    def get_ohlc_batch(self, instrument_keys):
        return {k: self.ohlc.get(k) for k in instrument_keys}

    def get_ltp(self, instrument_key):
        row = self.ohlc.get(instrument_key)
        return row["ltp"] if row else None


def make_strategy(broker, rules=None) -> NineFifteenStrategy:
    default_rules = {
        "lots": 1, "scan_time": "09:15", "observation_seconds": 45,
        "entry_cutoff_time": "09:20", "exit_time": "09:30", "min_pct_move": 0,
    }
    if rules:
        default_rules.update(rules)
    return NineFifteenStrategy(
        broker=broker, audit=MagicMock(), kill_switch=MagicMock(), rate_limiter=MagicMock(),
        rules=default_rules, user_id=1,
    )


# ---------------------------------------------------------------------------
# scan_top_movers
# ---------------------------------------------------------------------------
class TestScanTopMovers:
    def _universe_patch(self, symbols):
        return patch(
            "strategies.custom.nine_fifteen_strategy.InstrumentCache",
            return_value=MagicMock(list_tradable_symbols=lambda: {"stocks": symbols}),
        )

    def test_ranks_by_pct_change_and_picks_extremes(self):
        resolve_map = {"A": "KEY|A", "B": "KEY|B", "C": "KEY|C"}
        ohlc = {
            "KEY|A": {"ltp": 110, "prev_close": 100, "today_open": 101, "today_high": 112, "today_low": 100},  # +10%
            "KEY|B": {"ltp": 95, "prev_close": 100, "today_open": 99, "today_high": 100, "today_low": 94},     # -5%
            "KEY|C": {"ltp": 100, "prev_close": 100, "today_open": 100, "today_high": 101, "today_low": 99},   # 0%
        }
        broker = FakeBroker(ohlc=ohlc, resolve_map=resolve_map)
        strat = make_strategy(broker)
        with self._universe_patch(["A", "B", "C"]):
            result = strat.scan_top_movers()
        assert result["top_gainer"]["symbol"] == "A"
        assert result["top_loser"]["symbol"] == "B"
        assert round(result["top_gainer"]["pct_change"], 2) == 10.0
        assert round(result["top_loser"]["pct_change"], 2) == -5.0

    def test_skips_symbols_with_missing_or_zero_prev_close(self):
        resolve_map = {"A": "KEY|A", "B": "KEY|B"}
        ohlc = {
            "KEY|A": {"ltp": 110, "prev_close": 0, "today_open": 100, "today_high": 111, "today_low": 99},
            "KEY|B": {"ltp": 105, "prev_close": 100, "today_open": 101, "today_high": 106, "today_low": 100},
        }
        broker = FakeBroker(ohlc=ohlc, resolve_map=resolve_map)
        strat = make_strategy(broker)
        with self._universe_patch(["A", "B"]):
            result = strat.scan_top_movers()
        # Only B is usable — it becomes both the top gainer AND top loser (a 1-candidate field).
        assert result["top_gainer"]["symbol"] == "B"
        assert result["top_loser"]["symbol"] == "B"

    def test_none_when_nothing_resolvable(self):
        broker = FakeBroker(ohlc={}, resolve_map={})
        strat = make_strategy(broker)
        with self._universe_patch(["A", "B"]):
            assert strat.scan_top_movers() is None

    def test_none_when_universe_empty(self):
        broker = FakeBroker()
        strat = make_strategy(broker)
        with self._universe_patch([]):
            assert strat.scan_top_movers() is None


# ---------------------------------------------------------------------------
# schema validation / description
# ---------------------------------------------------------------------------
class TestValidateRules:
    def test_valid_rules_pass(self):
        rules = {
            "lots": 1, "scan_time": "09:15", "observation_seconds": 45,
            "entry_cutoff_time": "09:20", "exit_time": "09:30", "min_pct_move": 0,
        }
        assert validate_nine_fifteen_rules(rules) == []

    def test_bad_time_format_rejected(self):
        errors = validate_nine_fifteen_rules({"lots": 1, "scan_time": "9:15am"})
        assert any("scan_time" in e for e in errors)

    def test_out_of_order_times_rejected(self):
        errors = validate_nine_fifteen_rules({
            "lots": 1, "scan_time": "09:20", "entry_cutoff_time": "09:15", "exit_time": "09:30",
        })
        assert any("entry_cutoff_time" in e and "after" in e for e in errors)

    def test_observation_seconds_floor(self):
        errors = validate_nine_fifteen_rules({"lots": 1, "observation_seconds": 5})
        assert any("observation_seconds" in e for e in errors)

    def test_negative_min_pct_move_rejected(self):
        errors = validate_nine_fifteen_rules({"lots": 1, "min_pct_move": -1})
        assert any("min_pct_move" in e for e in errors)

    def test_non_dict_rejected(self):
        assert validate_nine_fifteen_rules([]) == ["Strategy rules must be an object."]


class TestDescribeRules:
    def test_empty_rules_message(self):
        assert describe_nine_fifteen_rules(None) == "No rules configured yet."

    def test_includes_key_settings(self):
        text = describe_nine_fifteen_rules({"lots": 2, "scan_time": "09:15", "exit_time": "09:30"})
        assert "2 lot" in text
        assert "09:15" in text
        assert "09:30" in text
