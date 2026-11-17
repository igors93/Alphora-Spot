# Alphora — Trading Engine

> An automated trading system undergoing a Binance Spot migration, with a native desktop interface, multi-mode execution, and capital rotation across USDT pairs.

---

## Table of Contents

- [Overview](#overview)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started)
- [Running the App](#running-the-app)
- [Developer Commands](#developer-commands)
- [Execution Modes](#execution-modes)
- [Configuration](#configuration)
- [Architecture & Engineering Plan](#architecture--engineering-plan)
- [Real-Market Readiness Roadmap](#real-market-readiness-roadmap)

---

## Overview

Alphora uses a central **Governor** to manage symbols, evaluate opportunities, execute trades, and enforce risk boundaries. The Spot rotation strategy reuses this engine; several module names still contain `futures` for compatibility.

### Spot capital rotation

Copy `.env.example` to `.env` and provide Spot API credentials. The example monitors BTC, ETH, SOL, BNB, and XRP, compares trend, momentum, volume, and relative strength, and keeps USDT when no entry qualifies. It can close a profitable holding when momentum weakens while its price is still rising. The example polls every 1 second; order placement still depends on Binance responses and the configured risk gates.

`ALPHORA_POSITION_NOTIONAL_USDT` controls the amount allocated to each new position. The default is a fixed 20 USDT, so rotation does not automatically invest the entire available USDT balance. The default portfolio limit is one open position. Spot stop and take orders are currently virtual: the running engine checks their prices and sends a market sell when triggered. They do not protect a holding while the bot is offline.

Start with `DRY_RUN` or `PAPER_LIVE`; `TESTNET_LIVE` and `REAL_LIVE` submit actual Spot orders to their respective environments. Keep the market type and endpoint consistent: `ALPHORA_MARKET_TYPE=SPOT` with `https://testnet.binance.vision` or `https://api.binance.com`.

Key capabilities:

| Capability | Details |
|---|---|
| Exchange | Binance Spot for capital rotation; legacy Futures modules remain |
| Interface | Native desktop app + CLI |
| Execution modes | `DRY_RUN`, `PAPER_LIVE`, `TESTNET_LIVE`, `REAL_LIVE` |
| Backtesting | Candle-level (OHLCV) CSV backtest engine with multi-timeframe data, realistic fills and metrics reports |
| AI scoring | Probabilistic model for signal filtering |
| Observability | Structured JSONL audit logs + rotating text logs |
| Resilience | Embedded snapshot engine with retry budgets, circuit breakers and snapshot policies |

---

## Project Structure

```
alphora/
├── src/           # Python modules and packages
│   ├── engine.py          # Engine entry point (alphora-engine)
│   ├── governor/          # Central orchestration engine
│   ├── futures/           # Position management, planning, risk
│   ├── trading/           # Trade intent, plans, intelligent execution
│   ├── backtesting/       # Backtest engine, broker simulation, metrics
│   ├── scanner/           # Signal scoring and priority scheduling
│   ├── strategies/        # Pluggable strategy base classes
│   ├── market_data/       # Candle hub, stores, timeframes
│   ├── indicators/        # ATR, RSI, moving averages, volatility
│   ├── observability/     # Structured logging, events, diagnostics
│   ├── resilience/        # Retry budgets, polling policies
│   ├── config/            # Config loading, validation, enums
│   ├── desktop/           # Python services and bridge for the native interface
│   ├── binance/           # Binance REST client
│   ├── ops/               # Operational CLI tools (pause, stop, status)
│   └── research/          # AI model training, klines/funding downloaders, paper broker
├── interface/             # Native Rust desktop interface
├── docs/                  # Guides, architecture map and engineering plans
├── tools/                 # Developer tools (architecture metrics)
├── models/                # Local AI model storage
├── data/                  # Candle CSVs, trade audit logs (created at runtime)
├── runtime/               # Live state files per symbol (created at runtime)
├── tests/                 # Test suite
└── main.py                # Source-checkout shim that runs src/engine.py
```

`config/` is not part of the repository: it is created locally for the
gitignored `*.local.json` files described in [Configuration](#configuration).

Code is imported directly from packages under `src/` (for example `governor`).
Strategy module paths saved by earlier versions with the `alphora.` prefix
are still accepted and translated to the new paths.

---

## Getting Started

### 1 — Create and activate a virtual environment

```bash
python -m venv .venv
```

**Linux / macOS**
```bash
source .venv/bin/activate
```

**Windows**
```bash
.venv\Scripts\activate
```

### 2 — Upgrade pip

```bash
python -m pip install --upgrade pip
```

### 3 — Install dependencies

```bash
pip install -e ".[dev]"
```

> The `[dev]` extras include `pytest`, `ruff`, `mypy`, and `build`.
> Installing also creates the `alphora-engine`, `alphora-research`
> and `alphora-backtest` commands. After pulling a version that adds entry points,
> rerun the install so an existing editable install picks them up.

Os mecanismos de snapshots e de tentativas fazem parte do Alphora e são instalados
com ele. Consulte `docs/snapshot-engine.md` e `docs/retry-engine.md` para sua
organização e manutenção.

### 4 — Set up environment variables

Create a `.env` file at the project root (or copy `.env.example`):

```env
ALPHORA_MARKET_TYPE=SPOT
BINANCE_SPOT_API_KEY=your_binance_spot_api_key
BINANCE_SPOT_API_SECRET=your_binance_spot_api_secret
ALPHORA_EXECUTION_MODE=DRY_RUN
ALPHORA_ENVIRONMENT=TESTNET
```

Every mode needs these credentials, including `DRY_RUN`: it never sends orders,
but it reads the account, balances and open orders through signed Binance
endpoints. Without them, configuration validation stops before any network call.
With `ALPHORA_ENVIRONMENT=TESTNET` and `ALPHORA_MARKET_TYPE=SPOT`, the default
endpoint is Spot Testnet (`https://testnet.binance.vision`), so use keys created there.

---

## Running the App

### Desktop GUI (recommended for live modes)

```bash
cargo run --manifest-path interface/Cargo.toml
```

> Live execution modes (`TESTNET_LIVE`, `REAL_LIVE`) require the GUI or `ALPHORA_ALLOW_CLI_RUN=true` as a safety guard.

The GUI's workspace (`config/`, `data/`, `runtime/`, …) is `ALPHORA_PROJECT_ROOT` when
set. Otherwise, when the running code sits inside a source checkout (a directory with
both `main.py` and `pyproject.toml`), it is that checkout and the engine starts with its
`main.py`. Elsewhere — for example a wheel installed in a virtual environment outside
the repository — it is a per-user directory (`%LOCALAPPDATA%\Alphora`,
`~/Library/Application Support/Alphora` or `$XDG_DATA_HOME/Alphora`) and the engine
starts with `python -m engine`.

### CLI (headless)

```bash
alphora-engine           # or: python -m engine, or python main.py in a checkout
```

### Research & backtesting CLI

```bash
alphora-research --help  # or: python -m research.cli --help
alphora-research download-funding --symbol ETHUSDT --start 2025-06-01 --end 2026-06-01
```

```bash
alphora-backtest --help  # or: python -m backtesting.adapters.cli --help
```

See [docs/backtesting-engine.md](docs/backtesting-engine.md) for cost semantics,
funding coverage, out-of-sample gates, and the promotion criteria from research to
paper/testnet.

Latest profitability audit: [docs/auditoria-lucratividade-2026-07-04.md](docs/auditoria-lucratividade-2026-07-04.md).

### Operational tools

```bash
# Check governor status
python -m ops.governor_status

# Pause a symbol
python -m ops.pause_symbol --symbol BTCUSDT

# Resume a symbol
python -m ops.resume_symbol --symbol BTCUSDT

# Stop a symbol
python -m ops.stop_symbol --symbol BTCUSDT
```

`ops.operational_stop` audits the exchange-side stop and take orders before approving
a stop with an open position. It blocks `SAFE_STOP` and `STOP_KEEP_PROTECTIONS` while
robot orders that could remain after shutdown are open. Cancel them explicitly or use
the confirmed emergency flatten flow before retrying the stop.

#### Emergency flatten

```bash
# Preview only: lists every open leg of ALPHORA_SYMBOL and the token it would require
python -m ops.emergency_flatten

# Flatten one symbol: TESTNET_LIVE token, then REAL_LIVE token
python -m ops.emergency_flatten --symbol BTCUSDT --confirm EMERGENCY_FLATTEN
python -m ops.emergency_flatten --symbol BTCUSDT --confirm EMERGENCY_FLATTEN_REAL

# Flatten every traded symbol, open position, and robot-order symbol (same tokens)
python -m ops.emergency_flatten --all --confirm EMERGENCY_FLATTEN
```

- Without `--symbol` or `--all` the command acts only on the configured `ALPHORA_SYMBOL`.
- An order is sent only in `TESTNET_LIVE` (`--confirm EMERGENCY_FLATTEN`) or `REAL_LIVE`
  (`--confirm EMERGENCY_FLATTEN_REAL`). A missing or wrong token, or `DRY_RUN`/`PAPER_LIVE`,
  returns a preview with `"dry_run": true` and sends nothing.
- When confirmed, it closes the One-way position with a single reduce-only MARKET order,
  waits until every leg reads flat, and only then cancels the robot's open orders. Already
  flat symbols also have their robot orders cancelled. If the position or cleanup is not
  confirmed, it writes a `manual_reconcile_required` file and reports `"blocked": true`.
- Hedge Mode, more than one open leg, or an unexpected `positionSide` is refused: no order
  is sent, a `manual_reconcile_required` file is written, and the JSON lists every leg
  under `open_legs` together with the manual procedure.
- Empty or malformed `positionRisk` data blocks the command; the account position must be
  checked manually before another emergency action.
- `--all` covers the symbols the Governor trades (the profiles in
  `ALPHORA_SYMBOL_PROFILES_PATH` when that file exists, whatever
  `ALPHORA_MULTI_SYMBOL_ENABLED` says; otherwise `ALPHORA_SYMBOLS`) plus every symbol with
  an open position or robot order anywhere on the account. One failing symbol does not stop the others.
  If the account scan fails, the results start with an error entry (`"symbol": "*"`) and
  the configured symbols are still processed. `python -m ops.cancel_robot_orders --all`
  and `python -m ops.operational_stop --mode SAFE_STOP --all --preview` use the same targets.

---

## Developer Commands

### Code quality — format and lint

```bash
python -m ruff format .
python -m ruff check . --fix
python -m ruff format --check .
python -m ruff check .
python -m mypy src
python -m pytest -v
python -m build
```

> Copy the entire block above to run the full quality pipeline in one go.

---

## Execution Modes

| Mode | Description |
|---|---|
| `DRY_RUN` | Read-only: real market data and signed account/position/order reads (credentials required); the strategy runs, but no order or account change is ever sent. |
| `PAPER_LIVE` | Live market data, real strategy, simulated order execution. |
| `TESTNET_LIVE` | Full live execution against Binance Testnet. Real logic, fake money. |
| `REAL_LIVE` | Full live execution on the real Binance Futures exchange. |

Set the mode via `ALPHORA_EXECUTION_MODE` in your `.env` or in the GUI before starting.

---

## Configuration

The engine's configuration (`load_config()` in `src/config/loaders.py`) comes only from
environment variables, resolved in this order:

1. **Process environment.** A variable that is already set always wins. When the desktop
   starts the engine, it converts its saved settings into these variables, so GUI
   settings take precedence over `.env`.
2. **One `.env` file**, loaded without overriding existing variables. The first that
   exists is used: the file named by `ALPHORA_ENV_FILE` (an error if it does not exist),
   then `.env` in `ALPHORA_PROJECT_ROOT` (the desktop workspace), in the current working
   directory, and in the source checkout that contains the code. The startup summary logs
   the file that was read as `env_file`. The research CLI uses the same rules.
3. **Built-in defaults** in `src/config/loaders.py`.

Key variables:

| Variable | Description |
|---|---|
| `ALPHORA_MARKET_TYPE` | `SPOT` (default) or `FUTURES`; must match the REST endpoint |
| `BINANCE_SPOT_API_KEY` / `BINANCE_SPOT_API_SECRET` | Spot credentials; Futures keys are never used as fallback |
| `BINANCE_FUTURES_API_KEY` / `BINANCE_FUTURES_API_SECRET` | Futures credentials when `ALPHORA_MARKET_TYPE=FUTURES` |
| `ALPHORA_ALLOW_CUSTOM_BINANCE_HOST` | Explicit opt-in for an unknown HTTPS host |
| `ALPHORA_EXECUTION_MODE` | `DRY_RUN`, `PAPER_LIVE`, `TESTNET_LIVE` or `REAL_LIVE` |
| `ALPHORA_ENVIRONMENT` | `TESTNET` or `REAL` for `DRY_RUN`/`PAPER_LIVE`; forced by `TESTNET_LIVE`/`REAL_LIVE` |
| `ALPHORA_SYMBOL` / `ALPHORA_SYMBOLS` | Single symbol, or a comma-separated list (e.g. `BTCUSDT,ETHUSDT`) |
| `ALPHORA_MAX_ACTIVE_SYMBOLS` | How many of `ALPHORA_SYMBOLS` are used (minimum 1) |
| `ALPHORA_STRATEGY_MODULE` / `ALPHORA_STRATEGY_CLASS` | Python module path and class of the active strategy |
| `ALPHORA_EXIT_MODEL` | `FIXED_PCT` or `ATR` |
| `ALPHORA_STOP_LOSS_PCT` / `ALPHORA_TAKE_PROFIT_PCT` | Fixed-percentage exits as fractions (defaults `0.01` / `0.02`) |
| `ALPHORA_MIN_SECONDS_BETWEEN_ENTRIES` | Per-symbol spacing between new entries (default 300 s, floor 60 s) |
| `ALPHORA_MAX_DAILY_TRADES` | Entries per symbol per UTC day (`0` = ceiling of 48, never unlimited) |
| `ALPHORA_STATE_PATH` / `ALPHORA_TRADE_JOURNAL_PATH` | Optional explicit financial-state paths; live defaults are `runtime/state_<symbol>_<environment>.json` and `data/trades_<symbol>_<environment>.jsonl`; dry-run and paper add their mode to the suffix |

Order mutations are not a separate setting: they are enabled exactly when
`ALPHORA_EXECUTION_MODE` is `TESTNET_LIVE` or `REAL_LIVE` (`AppConfig.mutations_enabled`).

Two optional JSON files complement the environment. Both are gitignored local files, and
a missing file is not an error:

| File (variable, default path) | Read by | Effect when missing |
|---|---|---|
| `ALPHORA_SYMBOL_PROFILES_PATH`, `config/alphora_symbols.local.json` | Governor and `ops` tools | The Governor trades `ALPHORA_SYMBOLS` with the environment's settings |
| `ALPHORA_PORTFOLIO_RISK_PATH`, `config/alphora_portfolio.local.json` | Governor | Default portfolio risk settings |

The default paths are relative to the process's working directory (the GUI runs the engine
from its workspace). The desktop also stores its own settings in
`config/alphora_gui.local.json`; the engine never reads that file directly.

Existing `runtime/state.json` or `data/trades.jsonl` files have no symbol or environment
identity. Startup refuses them until you identify their origin and move each file to the
matching symbol/environment/mode path. Set `ALPHORA_STATE_PATH` and
`ALPHORA_TRADE_JOURNAL_PATH` only to dedicated paths; the old shared filenames are refused
even when explicitly configured. Preserve backups before moving live state or journals.

Alphora targets **day trading and swing trading**. High-frequency trading is
blocked by hard limits; see
[Trading Cadence](docs/execution-modes-and-safety-guards.md#trading-cadence-day-trade-and-swing-trade-only).

See `src/config/models.py` for the full `AppConfig` schema.

---

## Architecture & Engineering Plan

The architecture documents are written in Portuguese.

| Document | What it answers |
|---|---|
| [Architecture map](docs/architecture/README.md) | How the system is built today: processes, packages, the Governor's 22 evaluation stages, decision and execution chain, data paths, backtest, observability and the desktop interface, in interactive Mermaid diagrams with file and line references |
| [Engineering master plan](docs/ALPHORA_SOFTWARE_ENGINEERING_OPTIMIZATION_PLAN.md) | What to improve and why: principles, invariants, target architecture, `OPT-*` backlog, quality gates and governance |
| [Migration plan](docs/MIGRATION_PLAN.md) | How and in what order: phases M0–M7, the Governor roadmap G1–G9, the first PRs, exit criteria and rollback |

Structural metrics (package cycles, long functions, Governor coupling, untyped context fields) are measured by:

```bash
python tools/architecture_metrics.py          # readable report
python tools/architecture_metrics.py --json   # for CI trends
```

---

## Real-Market Readiness Roadmap

See [the real-market readiness roadmap](docs/REAL_MARKET_READINESS_ROADMAP.md) for the verified bug backlog, prioritized engineering work, acceptance checks, and promotion gates. The repository does not yet contain evidence supporting real-money approval.

---

## License

The repository does not include a license file yet; the license decision is tracked as
ENG-07 in the [readiness roadmap](docs/REAL_MARKET_READINESS_ROADMAP.md).
