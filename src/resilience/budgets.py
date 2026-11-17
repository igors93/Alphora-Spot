from __future__ import annotations

from resilience.retry_engine import RetryBudget

BINANCE_READ_RETRY_BUDGET = RetryBudget(max_retries=20, per=60)
POSITION_CONFIRMATION_BUDGET = RetryBudget(max_retries=30, per=60)
EMERGENCY_FLATTEN_BUDGET = RetryBudget(max_retries=15, per=60)
