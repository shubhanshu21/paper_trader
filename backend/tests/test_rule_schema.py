"""
tests/test_rule_schema.py — tests for strategies/custom/rule_schema.py's
validate_rules(), including the duplicate-leg check added this session.
No DB, no network — pure validation logic.
"""
from strategies.custom.rule_schema import validate_rules


def _leg(action="SELL", option_type="CE", mode="ATM", value=None, lots=1):
    return {"action": action, "option_type": option_type, "strike_selection": {"mode": mode, "value": value}, "lots": lots}


def _rules(legs, **overrides):
    base = {
        "legs": legs,
        "entry": {"mode": "IMMEDIATE", "time": None},
        "exit": {"take_profit_pct": None, "stop_loss_pct": None, "exit_time": None, "exit_days_before_expiry": 1},
    }
    base.update(overrides)
    return base


class TestValidLegs:
    def test_single_leg_valid(self):
        assert validate_rules(_rules([_leg()])) == []

    def test_straddle_valid(self):
        assert validate_rules(_rules([_leg("BUY", "CE"), _leg("BUY", "PE")])) == []


class TestDuplicateLegDetection:
    def test_identical_atm_legs_flagged(self):
        errors = validate_rules(_rules([_leg("SELL", "CE"), _leg("SELL", "CE")]))
        assert any("identical to Leg 1" in e for e in errors)

    def test_identical_otm_legs_with_same_distance_flagged(self):
        legs = [_leg("BUY", "CE", "OTM_PERCENT", 5.0), _leg("BUY", "CE", "OTM_PERCENT", 5.0)]
        errors = validate_rules(_rules(legs))
        assert any("identical" in e for e in errors)

    def test_int_vs_float_same_value_still_flagged(self):
        # 5 vs 5.0 must be treated as the same distance, not different types.
        legs = [_leg("BUY", "CE", "OTM_PERCENT", 5), _leg("BUY", "CE", "OTM_PERCENT", 5.0)]
        errors = validate_rules(_rules(legs))
        assert any("identical" in e for e in errors)

    def test_different_otm_distance_not_flagged(self):
        legs = [_leg("BUY", "CE", "OTM_PERCENT", 5.0), _leg("BUY", "CE", "OTM_PERCENT", 10.0)]
        errors = validate_rules(_rules(legs))
        assert not any("identical" in e for e in errors)

    def test_different_action_not_flagged(self):
        legs = [_leg("BUY", "CE"), _leg("SELL", "CE")]
        assert validate_rules(_rules(legs)) == []

    def test_different_option_type_not_flagged(self):
        legs = [_leg("SELL", "CE"), _leg("SELL", "PE")]
        assert validate_rules(_rules(legs)) == []

    def test_three_identical_legs_flags_each_extra_one(self):
        legs = [_leg("SELL", "CE"), _leg("SELL", "CE"), _leg("SELL", "CE")]
        errors = validate_rules(_rules(legs))
        dup_errors = [e for e in errors if "identical" in e]
        assert len(dup_errors) == 2  # legs 2 and 3 both flagged against leg 1

    def test_malformed_leg_does_not_crash_duplicate_check(self):
        legs = [{"action": "SELL"}, _leg("SELL", "CE")]  # first leg missing required fields
        errors = validate_rules(_rules(legs))
        assert isinstance(errors, list)
        assert len(errors) > 0


class TestOtherValidation:
    def test_too_many_legs_rejected(self):
        errors = validate_rules(_rules([_leg(option_type="CE" if i % 2 == 0 else "PE", mode="OTM_PERCENT", value=float(i)) for i in range(9)]))
        assert any("at most" in e for e in errors)

    def test_negative_otm_distance_rejected(self):
        errors = validate_rules(_rules([_leg("BUY", "CE", "OTM_PERCENT", -5.0)]))
        assert any("negative" in e for e in errors)

    def test_at_time_entry_without_time_rejected(self):
        errors = validate_rules(_rules([_leg()], entry={"mode": "AT_TIME", "time": None}))
        assert any("HH:MM" in e for e in errors)
