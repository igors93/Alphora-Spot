"""Source identity constants and the authority resolver of the snapshot provider."""

from __future__ import annotations

from market_data.snapshot_engine import AuthorityPolicyResolver, SourceAuthorityPolicy

# Canonical source names — single source of truth for every source identifier used in
# ResourceUpdate.source, CallableBatchSource.name, and SourceAuthorityPolicy.source_ranks.
# Any string that identifies an Alphora data source in the snapshot engine must come from here.

# Reconciled execution state and User Data have equal private-state authority.
# The engine then uses observed_at to let a newer User Data event replace the cycle baseline while
# still allowing a genuinely newer local reconciliation to win.
SOURCE_LOCAL_RECONCILED = "alphora-local-reconciled"

# High authority: real-time data feeds beat REST
SOURCE_USER_DATA_CACHE = "alphora-user-data-cache"  # batch reads from local shadow cache
SOURCE_USER_DATA = "alphora-user-data"  # user-data-stream push events
SOURCE_MARKET_STREAM = "alphora-market-stream"  # WebSocket market stream

# Derived resources (computed from authoritative inputs; authority tracks their dependencies)
SOURCE_POSITION_DERIVED = "alphora-position-view"  # derived from ALL_POSITIONS
SOURCE_RULES_DERIVED = "alphora-symbol-rules"  # derived from EXCHANGE_INFO

# REST sources (lowest authority: may lag real-time feeds by hundreds of milliseconds)
SOURCE_REST_MARKET = "alphora-rest-market"  # REST ticker / mark price
SOURCE_REST_PRIVATE = "alphora-rest-private"  # REST account, positions, orders
SOURCE_REST_EXCHANGE = "alphora-rest-exchange"  # REST exchangeInfo

AUTHORITY_LOCAL_RECONCILED = 300
AUTHORITY_USER_DATA_CACHE = 300
AUTHORITY_USER_DATA = 300
AUTHORITY_MARKET_STREAM = 200
AUTHORITY_DERIVED = 100
AUTHORITY_REST_MARKET = 40
AUTHORITY_REST_PRIVATE = 30
AUTHORITY_REST_EXCHANGE = 20

MIN_EVALUATION_AUTHORITY_RANK = AUTHORITY_REST_EXCHANGE
MIN_ENTRY_AUTHORITY_RANK = AUTHORITY_REST_PRIVATE
MIN_EXIT_AUTHORITY_RANK = AUTHORITY_REST_PRIVATE

_DEFAULT_AUTHORITY = SourceAuthorityPolicy(
    source_ranks={
        SOURCE_LOCAL_RECONCILED: AUTHORITY_LOCAL_RECONCILED,
        SOURCE_USER_DATA_CACHE: AUTHORITY_USER_DATA_CACHE,
        SOURCE_USER_DATA: AUTHORITY_USER_DATA,
        SOURCE_MARKET_STREAM: AUTHORITY_MARKET_STREAM,
        SOURCE_POSITION_DERIVED: AUTHORITY_DERIVED,
        SOURCE_RULES_DERIVED: AUTHORITY_DERIVED,
        SOURCE_REST_MARKET: AUTHORITY_REST_MARKET,
        SOURCE_REST_PRIVATE: AUTHORITY_REST_PRIVATE,
        SOURCE_REST_EXCHANGE: AUTHORITY_REST_EXCHANGE,
    }
)


def build_authority_resolver() -> AuthorityPolicyResolver:
    """Return the shared AuthorityPolicyResolver used by every snapshot builder.

    Uses a single default policy; per-resource overrides can be added via the
    AuthorityPolicyResolver.overrides or .dynamic parameters without touching callers.
    """
    return AuthorityPolicyResolver(default=_DEFAULT_AUTHORITY)


__all__ = [
    "AUTHORITY_DERIVED",
    "AUTHORITY_LOCAL_RECONCILED",
    "AUTHORITY_MARKET_STREAM",
    "AUTHORITY_REST_EXCHANGE",
    "AUTHORITY_REST_MARKET",
    "AUTHORITY_REST_PRIVATE",
    "AUTHORITY_USER_DATA",
    "AUTHORITY_USER_DATA_CACHE",
    "MIN_ENTRY_AUTHORITY_RANK",
    "MIN_EVALUATION_AUTHORITY_RANK",
    "MIN_EXIT_AUTHORITY_RANK",
    "SOURCE_LOCAL_RECONCILED",
    "SOURCE_MARKET_STREAM",
    "SOURCE_POSITION_DERIVED",
    "SOURCE_REST_EXCHANGE",
    "SOURCE_REST_MARKET",
    "SOURCE_REST_PRIVATE",
    "SOURCE_RULES_DERIVED",
    "SOURCE_USER_DATA",
    "SOURCE_USER_DATA_CACHE",
    "build_authority_resolver",
]
