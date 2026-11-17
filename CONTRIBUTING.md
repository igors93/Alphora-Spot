# Contributing to Alphora

Thank you for your interest in contributing! This document explains how to get your development environment set up and how to submit changes.

---

## Development Setup

Follow the [Getting Started](README.md#getting-started) section in the README to create your virtual environment and install all dev dependencies.

---

## Branching

| Branch type | Naming convention | Example |
|---|---|---|
| Feature | `feat/<topic>` | `feat/adaptive-sizing` |
| Bug fix | `fix/<topic>` | `fix/order-retry-budget` |
| Refactor | `refactor/<topic>` | `refactor/governor-cycle` |
| Docs | `docs/<topic>` | `docs/backtesting-guide` |

Always branch off `main` and keep branches short-lived.

---

## Making Changes

1. **Write focused commits.** One logical change per commit. Keep messages short and imperative: `fix(governor): handle missing balance on cold start`.
2. **No magic constants.** Add named enums or constants to the appropriate `enums.py` or config module.
3. **No bare `except`.** Always catch specific exceptions.
4. **Preserve execution-mode safety.** Live modes (`TESTNET_LIVE`, `REAL_LIVE`) have guards in `src/engine.py` (run by `main.py`) — do not remove or bypass them.
5. **Type-annotate new public functions.** `mypy` runs in CI.

---

## Running the Quality Pipeline

Before opening a pull request, run the full pipeline locally:

```bash
python -m ruff format .
python -m ruff check . --fix
python -m ruff format --check .
python -m ruff check .
python -m mypy src
python -m pytest -v
```

All checks must pass. The build step (`python -m build`) is optional locally but runs in CI.

---

## Pull Request Guidelines

- Fill in the PR template completely.
- Link any related issue in the description with `Closes #<issue>`.
- Keep PRs small and focused. Large refactors should be discussed in an issue first.
- Add or update tests for any changed behavior.
- Do not modify `pyproject.toml` dependency versions without discussion.

---

## Adding a Strategy

Strategies live in `src/strategies/futures/`. Subclass `FuturesStrategyBase` from `src/strategies/futures/base.py` and implement the required interface. Register the strategy in your config JSON via `strategy_module` and `strategy_class`.

---

## Working with Backtests

Use the backtesting CLI to validate strategy behavior before any live run:

```bash
python -m backtesting.adapters.cli
```

Candle data (CSV files) lives in `data/market/`. Download additional data with:

```bash
python -m research.cli
```

---

## Questions?

Open an issue with the `question` label. Please search existing issues before opening a new one.
