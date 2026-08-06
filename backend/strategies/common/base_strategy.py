"""
strategies/base_strategy.py — Abstract Base Class for all trading strategies.

Every strategy in this project must inherit from BaseStrategy and implement
the `execute()` method. This enforces a consistent interface and ensures
that SEBI compliance checks, audit trail setup, and logging are always
initialised before any strategy logic runs.
"""

from abc import ABC, abstractmethod

from broker.base_broker import BaseBroker
from compliance.sebi_rules import AuditTrail, ComplianceError, KillSwitch, OrderRateLimiter
from utils.logger import get_logger
from utils.notify import notify

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
        notify_on_failure: bool = True,
    ) -> None:
        self.broker = broker
        self.audit = audit
        self.kill_switch = kill_switch
        self.rate_limiter = rate_limiter
        self._name = self.__class__.__name__
        # False for backtest cycles (see backtest/custom_engine.py) — a
        # historical data gap (e.g. an empty option chain for some past
        # expiry) is an ordinary "skip this cycle" outcome there, not a
        # live-trading emergency. Without this, every such gap fired a
        # real user-facing "FATAL — kill switch activated" Notification/
        # Telegram alert during an ordinary backtest run, even though
        # nothing was actually halted (the backtest's own KillSwitch is a
        # fresh throwaway instance per cycle, never wired to anything).
        self._notify_on_failure = notify_on_failure

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
            # notify() writes the in-app Notification row AND sends Telegram
            # (was Telegram-only via alert_error() before — a real gap: the
            # Bell icon never saw these, only Telegram did). user_id: only
            # RuleBasedStrategy (custom strategies) carries one — legacy
            # hand-written strategies have no owning user, so this stays
            # None for them (system-wide/admin-only, same as before).
            if self._notify_on_failure:
                notify(self._name, f"Trade skipped — compliance check failed: {exc}", level="warning", user_id=getattr(self, "user_id", None))
            return {"status": "failed", "error": str(exc)}
        except Exception as exc:
            log.critical(
                "FATAL error in strategy '%s': %s",
                self._name, exc, exc_info=True,
            )
            # Activate kill switch on any unhandled exception
            self.kill_switch.activate(reason=f"Unhandled exception: {exc}")
            if self._notify_on_failure:
                notify(self._name, f"FATAL — kill switch activated: {exc}", level="error", user_id=getattr(self, "user_id", None))
            return {"status": "failed", "error": str(exc)}

        log.info("=" * 60)
        log.info("END    strategy: %s | result: %s", self._name, result.get("status"))
        log.info("=" * 60)
        return result
