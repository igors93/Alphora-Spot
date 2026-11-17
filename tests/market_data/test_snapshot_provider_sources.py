"""Tests for Etapa 1: source identity constants and authority resolver."""

from __future__ import annotations

from market_data.snapshot_engine import AuthorityPolicyResolver
from market_data.snapshot_provider.sources import (
    _DEFAULT_AUTHORITY,
    SOURCE_LOCAL_RECONCILED,
    SOURCE_MARKET_STREAM,
    SOURCE_POSITION_DERIVED,
    SOURCE_REST_EXCHANGE,
    SOURCE_REST_MARKET,
    SOURCE_REST_PRIVATE,
    SOURCE_RULES_DERIVED,
    SOURCE_USER_DATA,
    SOURCE_USER_DATA_CACHE,
    build_authority_resolver,
)


def test_all_source_constants_are_strings():
    constants = [
        SOURCE_LOCAL_RECONCILED,
        SOURCE_USER_DATA_CACHE,
        SOURCE_USER_DATA,
        SOURCE_MARKET_STREAM,
        SOURCE_POSITION_DERIVED,
        SOURCE_RULES_DERIVED,
        SOURCE_REST_MARKET,
        SOURCE_REST_PRIVATE,
        SOURCE_REST_EXCHANGE,
    ]
    for c in constants:
        assert isinstance(c, str), f"Expected str, got {type(c)} for {c!r}"
        assert c.startswith("alphora-"), f"Source constant must start with 'alphora-': {c!r}"


def test_source_constants_are_unique():
    constants = [
        SOURCE_LOCAL_RECONCILED,
        SOURCE_USER_DATA_CACHE,
        SOURCE_USER_DATA,
        SOURCE_MARKET_STREAM,
        SOURCE_POSITION_DERIVED,
        SOURCE_RULES_DERIVED,
        SOURCE_REST_MARKET,
        SOURCE_REST_PRIVATE,
        SOURCE_REST_EXCHANGE,
    ]
    assert len(set(constants)) == len(constants), "Source constants must be unique"


def test_authority_precedence_stream_beats_rest():
    ranks = _DEFAULT_AUTHORITY.source_ranks
    assert ranks[SOURCE_MARKET_STREAM] > ranks[SOURCE_REST_MARKET]
    assert ranks[SOURCE_USER_DATA] > ranks[SOURCE_REST_PRIVATE]
    assert ranks[SOURCE_USER_DATA_CACHE] > ranks[SOURCE_REST_PRIVATE]


def test_authority_precedence_local_reconciled_matches_user_data():
    ranks = _DEFAULT_AUTHORITY.source_ranks
    assert ranks[SOURCE_LOCAL_RECONCILED] == ranks[SOURCE_USER_DATA]
    assert ranks[SOURCE_LOCAL_RECONCILED] == ranks[SOURCE_USER_DATA_CACHE]


def test_authority_precedence_rest_exchange_is_lowest():
    ranks = _DEFAULT_AUTHORITY.source_ranks
    assert ranks[SOURCE_REST_EXCHANGE] == min(ranks.values())


def test_unknown_source_has_no_rank_in_policy():
    ranks = _DEFAULT_AUTHORITY.source_ranks
    assert "unknown-source" not in ranks
    assert "user-data-stream" not in ranks, "Old inconsistent name must not appear"
    assert "market-stream" not in ranks, "Old inconsistent name must not appear"
    assert "alphora-local-execution-state" not in ranks, "Old inconsistent name must not appear"


def test_build_authority_resolver_returns_correct_type():
    resolver = build_authority_resolver()
    assert isinstance(resolver, AuthorityPolicyResolver)


def test_build_authority_resolver_is_idempotent():
    r1 = build_authority_resolver()
    r2 = build_authority_resolver()
    assert type(r1) is type(r2)


def test_default_authority_contains_all_sources():
    ranks = _DEFAULT_AUTHORITY.source_ranks
    expected = {
        SOURCE_LOCAL_RECONCILED,
        SOURCE_USER_DATA_CACHE,
        SOURCE_USER_DATA,
        SOURCE_MARKET_STREAM,
        SOURCE_POSITION_DERIVED,
        SOURCE_RULES_DERIVED,
        SOURCE_REST_MARKET,
        SOURCE_REST_PRIVATE,
        SOURCE_REST_EXCHANGE,
    }
    assert expected == set(ranks.keys())


def test_user_data_and_cache_have_equal_authority():
    ranks = _DEFAULT_AUTHORITY.source_ranks
    assert ranks[SOURCE_USER_DATA] == ranks[SOURCE_USER_DATA_CACHE]


def test_rest_private_lower_than_user_data_and_cache():
    ranks = _DEFAULT_AUTHORITY.source_ranks
    assert ranks[SOURCE_REST_PRIVATE] < ranks[SOURCE_USER_DATA]
    assert ranks[SOURCE_REST_PRIVATE] < ranks[SOURCE_USER_DATA_CACHE]
