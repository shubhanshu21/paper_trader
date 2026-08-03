"""
tests/test_wallet_real_margin.py — utils/wallet.py::_custom_strategy_wallet_stats
now prefers the REAL Upstox-calculated NETTED margin for an open basket
(via BaseBroker.get_basket_required_margin, all SELL legs in one call so
the exchange's own hedge-benefit netting applies) over the old flat-rate
estimate — same tiered "real when available, flat-rate fallback" policy
order-time sizing/validation already use.

Real automate_test MySQL schema (automate.db.engine.SessionLocal
monkeypatched to a factory bound to this test's own transaction — see
tests/conftest.py's db_session_factory), a fake paper broker
(monkeypatching wallet._paper_broker) — no network, and never the real
production DB.
"""
import json
import sys
from datetime import datetime

import pytest

import automate.utils.wallet as wallet
from automate.db.models import CustomStrategy, CustomStrategyPosition

# automate/db/__init__.py does `from .engine import engine`, which shadows
# the `automate.db.engine` package ATTRIBUTE with the Engine instance
# itself (a classic Python gotcha: a submodule import inside __init__.py
# under the same name as the submodule overwrites the submodule
# reference on the package). `import automate.db.engine as db_engine`
# would therefore bind to the Engine, not the module — go through
# sys.modules instead, which always holds the real submodule regardless.
db_engine = sys.modules["automate.db.engine"]


@pytest.fixture()
def session_factory(db_session_factory, monkeypatch):
    monkeypatch.setattr(db_engine, "SessionLocal", db_session_factory)
    return db_session_factory


def _seed_open_strangle(session_factory, symbol="NIFTY"):
    session = session_factory()
    strategy = CustomStrategy(
        user_id=1, name="Strangle", instrument_type="INDEX", strategy_type="CUSTOM", option_type="BOTH",
        symbols=json.dumps([symbol]), status="PAPER_TRADING",
    )
    session.add(strategy)
    session.commit()
    session.refresh(strategy)

    now = datetime.now()
    session.add_all([
        CustomStrategyPosition(
            strategy_id=strategy.id, leg_index=0, mode="paper", instrument_key="NSE_FO|CE1", instrument_type="OPTION",
            option_type="CE", strike=24000, expiry="2026-08-27", transaction_type="SELL", quantity=50,
            entry_price=100.0, status="OPEN", opened_at=now,
        ),
        CustomStrategyPosition(
            strategy_id=strategy.id, leg_index=1, mode="paper", instrument_key="NSE_FO|PE1", instrument_type="OPTION",
            option_type="PE", strike=23000, expiry="2026-08-27", transaction_type="SELL", quantity=50,
            entry_price=90.0, status="OPEN", opened_at=now,
        ),
    ])
    session.commit()
    session.close()


class FakeBroker:
    def __init__(self, basket_margin):
        self.basket_margin = basket_margin
        self.calls = []

    def get_basket_required_margin(self, instruments):
        self.calls.append(instruments)
        return self.basket_margin


class TestRealMarginPreferred:
    def test_uses_real_basket_margin_when_broker_available(self, session_factory, monkeypatch):
        _seed_open_strangle(session_factory)
        broker = FakeBroker(basket_margin=42000.0)
        monkeypatch.setattr(wallet, "_paper_broker", lambda: broker)
        monkeypatch.setattr(
            "automate.api.custom_strategy_scheduler._is_leg_for_symbol",
            lambda key, sym: True,
        )

        stats = wallet._custom_strategy_wallet_stats(user_id=1, mode="paper")

        assert stats["margin_blocked"] == 42000.0
        assert len(broker.calls) == 1
        instruments = broker.calls[0]
        assert len(instruments) == 2  # both SELL legs passed together for netting
        assert all(i["transaction_type"] == "SELL" for i in instruments)

    def test_falls_back_to_flat_estimate_when_no_broker(self, session_factory, monkeypatch):
        _seed_open_strangle(session_factory)
        monkeypatch.setattr(wallet, "_paper_broker", lambda: None)
        monkeypatch.setattr(
            "automate.api.custom_strategy_scheduler._is_leg_for_symbol",
            lambda key, sym: True,
        )

        stats = wallet._custom_strategy_wallet_stats(user_id=1, mode="paper")

        # Flat estimate: spot_proxy = avg(24000, 23000) = 23500, short_qty = 50, NIFTY is an index -> 11% rate
        assert stats["margin_blocked"] == pytest.approx(23500 * 50 * 0.11, rel=1e-6)

    def test_falls_back_to_flat_estimate_when_real_margin_call_fails(self, session_factory, monkeypatch):
        _seed_open_strangle(session_factory)

        class ExplodingBroker:
            def get_basket_required_margin(self, instruments):
                raise RuntimeError("network hiccup")

        monkeypatch.setattr(wallet, "_paper_broker", lambda: ExplodingBroker())
        monkeypatch.setattr(
            "automate.api.custom_strategy_scheduler._is_leg_for_symbol",
            lambda key, sym: True,
        )

        stats = wallet._custom_strategy_wallet_stats(user_id=1, mode="paper")
        assert stats["margin_blocked"] == pytest.approx(23500 * 50 * 0.11, rel=1e-6)
