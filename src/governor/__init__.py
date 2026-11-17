"""Multi-symbol governor package.

The governor coordinates scanner score, symbol-level risk, portfolio risk and
execution. Score is observational; only the execution engine can mutate the
exchange after all checks approve an intent.
"""
