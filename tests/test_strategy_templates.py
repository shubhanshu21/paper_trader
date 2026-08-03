"""
tests/test_strategy_templates.py — GET /templates/strategy-types used to
return legs in a shape ("side", no strike_selection) that didn't match
rule_schema.py at all and could never round-trip into a real strategy.
Fixed this session so each template's legs are real, valid rule-schema
legs the frontend's template picker can feed straight into the builder.
This test is the actual regression that matters: every template's legs
must pass validate_rules() when combined with a complete rules dict.
"""
from automate.api.routes_custom_strategies import get_strategy_types
from automate.strategies.custom.rule_schema import validate_rules


def _full_rules(legs):
    return {
        "legs": legs,
        "entry": {"mode": "IMMEDIATE", "time": None},
        "exit": {"take_profit_pct": None, "stop_loss_pct": None, "exit_time": None, "exit_days_before_expiry": 1},
    }


class TestStrategyTemplates:
    def test_returns_expected_template_types(self):
        types = {t["type"] for t in get_strategy_types()["strategy_types"]}
        assert types == {"STRADDLE", "STRANGLE", "IRON_CONDOR", "BUTTERFLY", "CUSTOM"}

    def test_every_template_legs_pass_validate_rules(self):
        for template in get_strategy_types()["strategy_types"]:
            errors = validate_rules(_full_rules(template["legs"]))
            assert errors == [], f"{template['type']} template legs failed validation: {errors}"

    def test_every_leg_has_real_rule_schema_fields(self):
        for template in get_strategy_types()["strategy_types"]:
            for leg in template["legs"]:
                assert leg["action"] in ("BUY", "SELL")
                assert leg["option_type"] in ("CE", "PE")
                assert "strike_selection" in leg and "mode" in leg["strike_selection"]
                assert leg["lots"] >= 1

    def test_iron_condor_has_four_legs_two_sell_two_buy(self):
        iron_condor = next(t for t in get_strategy_types()["strategy_types"] if t["type"] == "IRON_CONDOR")
        legs = iron_condor["legs"]
        assert len(legs) == 4
        assert sum(1 for leg in legs if leg["action"] == "SELL") == 2
        assert sum(1 for leg in legs if leg["action"] == "BUY") == 2

    def test_no_template_has_duplicate_legs(self):
        # Confirms the templates themselves don't trip the new
        # duplicate-leg check added to validate_rules() this session.
        for template in get_strategy_types()["strategy_types"]:
            errors = validate_rules(_full_rules(template["legs"]))
            assert not any("identical" in e for e in errors)
