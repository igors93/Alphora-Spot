"""Names the snapshot engine writes into payloads that leave the process.

legacy-wire-names (ADR-002): the engine was developed under the name "Coalestra", and these values
are part of the persisted diagnostics and metrics contract (journals, audit logs, health payloads).
They keep their values until the event schema of migration M7 versions them. New code must not use
the legacy name anywhere else; ``tools/architecture_metrics.py`` counts it outside these modules.
"""

from __future__ import annotations

CAPABILITIES_SCHEMA = "coalestra.capabilities"
ERROR_DIAGNOSTICS_SCHEMA = "coalestra.error-diagnostics"
BUILDER_HEALTH_SCHEMA = "coalestra.builder-health"
BUILDER_HEALTH_ASSESSMENT_SCHEMA = "coalestra.builder-health-assessment"

# Outcome label of a source call stopped by an engine-owned timer (metrics and diagnostics).
SOURCE_TIMEOUT_OUTCOME = "coalestra_timeout"

__all__ = [
    "BUILDER_HEALTH_ASSESSMENT_SCHEMA",
    "BUILDER_HEALTH_SCHEMA",
    "CAPABILITIES_SCHEMA",
    "ERROR_DIAGNOSTICS_SCHEMA",
    "SOURCE_TIMEOUT_OUTCOME",
]
