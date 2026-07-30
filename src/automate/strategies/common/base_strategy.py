"""
strategies/base_strategy.py — Abstract Base Class for all trading strategies.

Every strategy in this project must inherit from BaseStrategy and implement
the `execute()` method. This enforces a consistent interface and ensures
that SEBI compliance checks, audit trail setup, and logging are always
initialised before any strategy logic runs.
"""

from abc import ABC, abstractmethod

from automate.broker.base_broker import BaseBroker
from automate.compliance.sebi_rules import AuditTrail, KillSwitch, OrderRateLimiter, ComplianceError
from automate.utils.logger import get_logger
from automate.utils.telegram_alert import alert_error

log = get_logger(__name__)


class BaseStrategy(ABC):
    """
    Abstract base class for all automated trading strategies.

    Subclasses must implement `execute()`. The base class provides:
      - A shared UpstoxBroker instance
      - A shared KillSwitch instance
      - A shared OrderRateLimiter instance
      - A shared AuditTrail instance
      - Standardised strategy lifecycle logging

    Args:
        broker:    Initialised UpstoxBroker instance.
        config:    The StrategyConfig namespace.
        audit:     AuditTrail instance for SEBI record-keeping.
        kill_switch: Shared KillSwitch for emergency halt.
        rate_limiter: Shared OrderRateLimiter.
    """

    def __init__(
        self,
        broker: BaseBroker,
        audit: AuditTrail,
        kill_switch: KillSwitch,
        rate_limiter: OrderRateLimiter,
    ) -> None:
        self.broker = broker
        self.audit = audit
        self.kill_switch = kill_switch
        self.rate_limiter = rate_limiter
        self._name = self.__class__.__name__

        log.info("Strategy '%s' initialised.", self._name)

    @abstractmethod
    def execute(self) -> dict:
        """
        Execute the strategy.

        Subclasses implement all fetch → calculate → validate → order logic.

        Returns:
            A dict summarising the execution result, e.g.:
            {
                "status": "success" | "dry_run" | "failed",
                "call_order_id": str | None,
                "put_order_id":  str | None,
                "call_strike":   int,
                "put_strike":    int,
                "spot_price":    float,
            }
        """
        ...

    def run(self) -> dict:
        """
        Lifecycle wrapper that calls execute() with standard logging.

        This is the public entry point — callers call `strategy.run()`,
        never `strategy.execute()` directly.
        """
        log.info("=" * 60)
        log.info("START  strategy: %s", self._name)
        log.info("=" * 60)

        try:
            result = self.execute()
        except ComplianceError as exc:
            log.warning(
                "Strategy compliance check failed for '%s': %s",
                self._name, exc
            )
            alert_error(self._name, f"Trade skipped — compliance check failed: {exc}")
            return {"status": "failed", "error": str(exc)}
        except Exception as exc:
            log.critical(
                "FATAL error in strategy '%s': %s",
                self._name, exc, exc_info=True,
            )
            # Activate kill switch on any unhandled exception
            self.kill_switch.activate(reason=f"Unhandled exception: {exc}")
            alert_error(self._name, f"FATAL — kill switch activated: {exc}")
            return {"status": "failed", "error": str(exc)}

        log.info("=" * 60)
        log.info("END    strategy: %s | result: %s", self._name, result.get("status"))
        log.info("=" * 60)
        return result
