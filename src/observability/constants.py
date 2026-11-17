"""Observability subsystem constants."""

from __future__ import annotations

SCHEMA_VERSION = 1
EMERGENCY_FALLBACK_PATH = "runtime/emergency_diagnostics.log"
MAX_CHAIN_DEPTH = 10
MAX_CYCLE_DEPTH = 3
SENTINEL = object()  # queue shutdown sentinel
