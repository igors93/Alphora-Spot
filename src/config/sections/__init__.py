"""Section loaders of ``load_config`` (migration M7, OPT-029).

Each module reads the environment variables of one group of the Plano Diretor §22 and returns
the matching ``AppConfig`` fields as a ``TypedDict``; ``config.loaders.load_config`` composes them
into the single immutable ``AppConfig``:

- ``execution``: environment, mode, authorization, credentials, order policies.
- ``intelligence``: architecture, activation level, operational module modes, probabilistic AI.
- ``market_data``: market and user data streams, market shock, candles, snapshots, REST weight.
- ``trading``: symbols, strategy and trade parameters, exit model, trend context.
- ``risk``: daily guards, defensive mode, economic gate, costs, smart sizing.
- ``runtime``: files and paths, loop cadence, back-off, Governor scheduling.
- ``observability``: Governor metrics and run audit retention.
"""
