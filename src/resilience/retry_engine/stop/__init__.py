"""Stop strategies."""

from resilience.retry_engine.stop.attempts import StopAfterAttempt
from resilience.retry_engine.stop.base import StopMixin, StopStrategy
from resilience.retry_engine.stop.composite import AllStopStrategy, AnyStopStrategy
from resilience.retry_engine.stop.forever import StopForever
from resilience.retry_engine.stop.max_time import StopAfterDelay

__all__ = [
    "AllStopStrategy",
    "AnyStopStrategy",
    "StopAfterAttempt",
    "StopAfterDelay",
    "StopForever",
    "StopMixin",
    "StopStrategy",
]
