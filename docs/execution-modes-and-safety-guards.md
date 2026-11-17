# Execution Modes & Safety Guards

Alphora has four execution modes that control how much of the system is "real" at any given time. Understanding the boundaries between them is critical before running in any live environment.

---

## The Four Modes

```
DRY_RUN ──► PAPER_LIVE ──► TESTNET_LIVE ──► REAL_LIVE
(read-only,  (live data,    (Binance Testnet,  (real exchange,
 no orders)   fake orders)   real logic)        real money)
```

### `DRY_RUN`

- **Read-only, not offline.** It connects to Binance Futures: public market data plus
  signed reads of the account, position mode, positions and open orders.
- Therefore it **requires** `BINANCE_FUTURES_API_KEY` and `BINANCE_FUTURES_API_SECRET`;
  without them configuration validation fails before any network call.
- The strategy and the full Governor pipeline run, but every order, cancel, margin or
  leverage change is skipped (orders and cancels are journaled as dry-run events);
  `mutations_enabled` is always `false`, and the Binance client itself refuses mutations.
- `ALPHORA_ENVIRONMENT` (`TESTNET` or `REAL`) selects which endpoint and account are read.
- There is currently no credential-free, network-free mode for the engine; use the
  backtesting engine and the unit tests for fully offline work.

### `PAPER_LIVE`

- Connects to the live Binance Futures WebSocket for real market prices.
- Order execution is **simulated** — no orders are sent to the exchange.
- The strategy runs with real signals and real timing, but fills are synthetic.
- Simulated stop-loss/take-profit are checked every governor cycle against the
  trigger price (mark price by default). Stops fill at the observed price, never
  better than the stop; takes fill at the target.
- Paper fills pay the configured fees, spread, slippage and funding, and paper
  trades count toward the same daily limits and entry spacing as live trading.
- Use this to observe how the strategy behaves on live data before going further.

### `TESTNET_LIVE`

- Full execution against the **Binance Futures Testnet**.
- Real logic, real order placement, but Testnet accounts use play money.
- Requires Testnet API keys (`ALPHORA_ENVIRONMENT=TESTNET`).
- Subject to the CLI safety guard (see below).

### `REAL_LIVE`

- Full execution on the **real Binance Futures exchange**.
- Real logic, real orders, real money.
- Requires production API keys (`ALPHORA_ENVIRONMENT=REAL`).
- Subject to the strictest CLI safety guard.
- Scripted test strategies (`ExecutionProbeStrategy`, `RandomTimedSignalStrategy`)
  are refused.
- New entries require a primary signal timeframe of **5m or slower**; exits are
  never blocked by this rule.

---

## Trading Cadence: Day Trade and Swing Trade Only

Alphora is built for crypto day trading and swing trading. High-frequency
trading (positions held for seconds or milliseconds, order bursts, tick
scalping) is out of scope and blocked by hard limits that configuration can
only make stricter. They live in `src/trading_cadence.py`.

| Guard | Limit | Applies to |
|---|---|---|
| Governor loop interval | at least 1 s (`ALPHORA_LOOP_INTERVAL_SECONDS`) | all modes |
| Spacing between entries on the same symbol | default 300 s, floor 60 s (`ALPHORA_MIN_SECONDS_BETWEEN_ENTRIES`) | paper, testnet, real and backtests |
| Entries per symbol per UTC day | at most 48; `ALPHORA_MAX_DAILY_TRADES=0` means "use 48", never unlimited | paper, testnet, real and backtests |
| Timer of scripted test strategies | at least 60 s | all modes |
| Primary signal timeframe for new entries | 5m or slower | `REAL_LIVE` |

An entry order that was sent but rolled back (for example, invalid protection
after the fill) still counts for the spacing, so a failing entry cannot be
retried every cycle.

---

## CLI Safety Guard

Direct invocation via `python main.py` (or `alphora-engine` / `python -m engine`) is **blocked** for `TESTNET_LIVE` and `REAL_LIVE` unless one of the following is true:

| Condition | How it's set |
|---|---|
| Launched from the Desktop GUI | `ALPHORA_LAUNCHED_BY_GUI=true` (set automatically by the GUI) |
| Explicit developer override | `ALPHORA_ALLOW_CLI_RUN=true` in environment |

This guard exists in `src/engine.py → _enforce_live_entrypoint()` (the repository's `main.py` delegates to it). Its purpose is to prevent accidental live runs from a terminal where the operator may not have verified the config.

If you hit the block:

```
Execucao live direta via `python main.py` bloqueada por seguranca.
Inicie pela interface Desktop (alphora-desktop).
Para desenvolvimento consciente, defina ALPHORA_ALLOW_CLI_RUN=true.
```

Either start the GUI (`cargo run --manifest-path interface/Cargo.toml`) or set `ALPHORA_ALLOW_CLI_RUN=true` in your shell before running.

---

## `mutations_enabled` Flag

`mutations_enabled` is not a setting: `AppConfig.mutations_enabled` is derived from the
execution mode and cannot be configured separately.

| `execution_mode` | `mutations_enabled` | Result |
|---|---|---|
| `DRY_RUN` | `false` | No orders ever sent; account and positions are only read |
| `PAPER_LIVE` | `false` | No orders ever sent; fills are simulated |
| `TESTNET_LIVE` | `true` | Orders sent to Testnet (after the CLI guard and confirmations) |
| `REAL_LIVE` | `true` | Orders sent to the real exchange (also requires `ALPHORA_ALLOW_REAL_TRADING=true`) |

The engine and the operational tools pass this flag to the `BinanceFuturesClient` at construction time, and the client raises `BinanceFuturesMutationBlocked` if a mutation is attempted while it is `false`.

---

## Intelligent Activation Levels

For strategies that use the AI probabilistic gate, a separate `IntelligentActivationLevel` enum controls how the AI signal interacts with the execution pipeline:

| Level | Effect |
|---|---|
| `SHADOW_ONLY` | AI scores computed but never block trades |
| `PAPER_ACTIVE` | AI blocks trades in paper mode only |
| `TESTNET_OBSERVE` | AI scores logged on Testnet, no blocking |
| `TESTNET_REDUCE` | AI can reduce position size on Testnet |
| `TESTNET_BLOCK` | AI can block trades entirely on Testnet |
| `REAL_OBSERVE` | AI scores logged on real exchange, no blocking |
| `REAL_REDUCE` | AI can reduce size on real exchange |
| `REAL_BLOCK` | AI can block trades on real exchange |

The AI gate is managed by `AIGateMixin` in the Governor.

---

## Setting the Mode

Via environment variables or the `.env` file (see [Configuration](../README.md#configuration) for precedence):

```env
ALPHORA_EXECUTION_MODE=PAPER_LIVE
ALPHORA_ENVIRONMENT=TESTNET
```

The engine does not read the mode from any JSON file. The GUI's operation panel stores
its choice in `config/alphora_gui.local.json`, passes it to the engine as these
environment variables, surfaces the settings before any start command, and prevents
starting `REAL_LIVE` without an explicit confirmation step.
