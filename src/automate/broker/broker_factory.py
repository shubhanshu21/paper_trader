"""
broker/broker_factory.py — Builds the paper + live broker pair.

Upstox is the only real broker this bot talks to. There is no broker
"selection" — `create_mode_brokers()` always builds a real UpstoxBroker
(live orders, no dry-run gate — MODE='live' means live) and a PaperBroker
wrapping that same connection for real market data (simulated fills,
never real money). Each strategy/position picks one of these two by its
own MODE, not by any global/CLI setting — see config.py.
"""

from automate.broker.base_broker import BaseBroker
from automate.utils.logger import get_logger

log = get_logger(__name__)


class BrokerFactory:
    """Builds the {'paper': ..., 'live': ...} broker pair."""

    @staticmethod
    def create_mode_brokers() -> dict:
        """
        Only ONE real connection is made: PaperBroker never calls the real
        broker's order-placement methods (see broker/paper_broker.py), so
        it's safe for the same UpstoxBroker instance to also serve
        MODE='live' strategies directly — no second connection/instrument
        -cache load needed.
        """
        from automate.broker.paper_broker import PaperBroker
        from automate.broker.upstox_broker import UpstoxBroker
        from automate.config import UpstoxConfig

        real_broker: BaseBroker = UpstoxBroker(access_token=UpstoxConfig.ACCESS_TOKEN, dry_run=False)
        log.info("BrokerFactory: Upstox connection ready — building paper + live broker pair.")
        return {"paper": PaperBroker(real_broker=real_broker), "live": real_broker}
